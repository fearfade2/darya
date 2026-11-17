"""Explicit join approvals and persistent, command-enabled approval per group."""
from __future__ import annotations

import asyncio
import json
import re
import time

from telethon import errors, functions, types, utils

from .authority import authorized
from .group_conversation import topic_id as message_topic
from .groups import group_entity


JOIN_ACTIONS = {'pending_requests', 'approve_all_joins', 'approve_listed_joins', 'auto_approve_joins'}


class JoinApprovalRevoked(ValueError):
    """A persistent permission to approve requests is no longer valid."""


def parse_join_command(text):
    low = text.casefold().replace('ё', 'е').strip().rstrip('.!? ')
    low = re.sub(r'\s+', ' ', low)
    low = re.sub(r'^(?:можешь|можешь ли ты|давай)\s+', '', low)
    low = re.sub(r'[, ]+пожалуйста$', '', low)
    low = re.sub(r'\s+(?:тут|здесь|в этой группе|в этом чате)$', '', low)
    if re.fullmatch(r'(?:прими|принять|принимай|одобри|одобрить) (?:все|все ожидающие|все текущие|все новые) заявки', low):
        return {'action': 'approve_all_joins'}
    if re.fullmatch(r'(?:покажи|показать|посмотри|проверь|список) (?:(?:все|ожидающие|новые) )?заявки', low) or low in {'заявки', 'список заявок', 'покажи заявки на вступление'}:
        return {'action': 'pending_requests'}
    if re.fullmatch(r'(?:прими|принять|одобри|одобрить) (?:их|эти заявки|этих участников|заявки из списка)', low):
        return {'action': 'approve_listed_joins'}
    if (re.fullmatch(r'(?:принимай|одобряй) (?:(?:все|новые) )?заявки (?:автоматически|автоматом)', low)
            or re.fullmatch(r'(?:автоматически|автоматом) (?:принимай|одобряй) (?:(?:все|новые) )?заявки', low)
            or re.fullmatch(r'(?:включи|включить|запусти) (?:автоприем|авто ?прием|автоматический прием) заявок', low)
            or low == '/autoapprove on'):
        return {'action': 'auto_approve_joins', 'enabled': True}
    if (re.fullmatch(r'(?:выключи|выключить|отключи|отключить|останови) (?:автоприем|авто ?прием|автоматический прием) заявок', low)
            or re.fullmatch(r'(?:не|больше не|перестань) (?:принимай|принимать|одобряй|одобрять) (?:(?:все|новые) )?заявки(?: (?:автоматически|автоматом))?', low)
            or low == '/autoapprove off'):
        return {'action': 'auto_approve_joins', 'enabled': False}
    return None


class GroupJoinRequests:
    def __init__(self, admin):
        self.admin, self.runtime, self.store = admin, admin.runtime, admin.store
        self.task = None

    def authority_version(self, actor, chat_id):
        if actor == self.store.settings()['owner_id']:
            return 'owner'
        return json.dumps(self.store.rows('SELECT chat_id,enabled,updated FROM controller_permissions WHERE owner_id=? AND user_id=? AND chat_id IN (?,0) ORDER BY chat_id',
            (self.store.settings()['owner_id'], actor, chat_id)), sort_keys=True)

    async def pending_page(self, chat, *, offset_date=None, offset_user=None):
        return await self.admin.telegram.client(functions.messages.GetChatInviteImportersRequest(
            chat, offset_date, offset_user or types.InputUserEmpty(), 50, requested=True))

    @staticmethod
    def request_date(importer):
        date = getattr(importer, 'date', None)
        return date.timestamp() if date else None

    async def execute(self, chat, op, guard, *, actor_id=None, topic_id=0, source_id=None, reply=None):
        chat_id = utils.get_peer_id(chat)
        actor = actor_id if actor_id is not None else self.store.settings()['owner_id']
        generation = self.store.dialog_scope(chat_id, 0)[2]
        guard()
        if op.action == 'auto_approve_joins':
            if op.enabled and (not self.store.settings()['enabled'] or not self.store.settings()['group_enabled']
                               or not self.store.contact(chat_id)['enabled'] or self.store.dialog_blocked(chat_id)):
                raise ValueError('Группа отключена; сначала включи ее в панели')
            self.store.execute('''INSERT INTO group_join_automation
                (chat_id,enabled,owner_id,actor_id,generation,authority_version,revision,next_check,status,updated)
                VALUES (?,?,?,?,?,?,1,0,'idle',?) ON CONFLICT(chat_id) DO UPDATE SET
                enabled=excluded.enabled,owner_id=excluded.owner_id,actor_id=excluded.actor_id,
                generation=excluded.generation,authority_version=excluded.authority_version,
                revision=group_join_automation.revision+1,next_check=0,status='idle',updated=excluded.updated''',
                (chat_id,int(op.enabled),self.store.settings()['owner_id'],actor,generation,self.authority_version(actor,chat_id),time.time()))
            return {'text': 'Автоприём заявок включён в этой группе: принимаю ожидающие и новые заявки автоматически.' if op.enabled
                    else 'Автоприём заявок в этой группе выключен.'}
        if op.action == 'pending_requests':
            page = await self.pending_page(chat)
            guard()
            requests = {str(i.user_id): self.request_date(i) for i in page.importers}
            users = [u for u in page.users if str(u.id) in requests]
            # Cache exactly the displayed set, scoped to the real command author.
            requests = {str(u.id): requests[str(u.id)] for u in users}
            text = 'Заявки (до 50):\n' + '\n'.join(self.admin.user_label(u) for u in users) if users else 'Ожидающих заявок нет.'
            if source_id is not None:
                self.store.execute('''INSERT OR REPLACE INTO group_join_lists
                    (chat_id,topic_id,actor_id,owner_id,generation,source_id,requesters,text,created)
                    VALUES (?,?,?,?,?,?,?,?,?)''',
                    (chat_id,topic_id or 0,actor,self.store.settings()['owner_id'],generation,source_id,json.dumps(requests),text,time.time()))
            return {'text': text, 'join_requesters': requests}
        if op.action == 'approve_all_joins':
            guard()
            result = await self.admin.telegram.client(functions.messages.HideAllChatJoinRequestsRequest(chat,approved=True))
            if result is False:
                raise ValueError('Telegram не подтвердил приём заявок')
            return {'text': 'Приняла все ожидающие заявки в этой группе.'}
        if op.action == 'approve_listed_joins':
            listed = self.list_context(chat_id, topic_id, actor, generation, reply)
            if listed is None:
                raise ValueError('Не вижу списка заявок для этого поручения. Напиши «покажи заявки», затем «прими их» или «прими все заявки».')
            # Re-read actual requests; never approve a newcomer absent from the displayed list.
            remaining = set(listed)
            accepted = 0
            offset_date, offset_user = None, None
            seen_offsets = set()
            for _ in range(100):
                guard()
                page = await self.pending_page(chat,offset_date=offset_date,offset_user=offset_user)
                guard()
                users = {str(u.id): u for u in page.users}
                for importer in page.importers:
                    key = str(importer.user_id)
                    if key not in remaining or key not in users:
                        continue
                    remaining.remove(key)
                    if listed[key] is not None and listed[key] != self.request_date(importer):
                        continue
                    guard()
                    result = await self.admin.telegram.client(functions.messages.HideChatJoinRequestRequest(
                        chat, utils.get_input_user(users[key]), approved=True))
                    if result is False:
                        raise ValueError('Telegram не подтвердил приём заявки')
                    accepted += 1
                if not remaining or len(page.importers) < 50:
                    break
                last = page.importers[-1]
                marker = (last.user_id, self.request_date(last))
                if marker in seen_offsets or str(last.user_id) not in users:
                    break
                seen_offsets.add(marker)
                offset_date, offset_user = last.date, utils.get_input_user(users[str(last.user_id)])
            return {'text': f'Приняла заявки из показанного списка: {accepted}.' if accepted else 'В показанном списке больше нет ожидающих заявок.',
                    'accepted': accepted}
        raise ValueError('Неизвестная команда заявок')

    def list_context(self, chat_id, topic_id, actor, generation, reply):
        params = (chat_id,topic_id or 0,actor,self.store.settings()['owner_id'],generation,time.time()-1800)
        suffix, source_id = '', None
        if reply is not None:
            # Only an authentic recorded outgoing list in the same topic is a list reference.
            if (reply.sender_id != self.admin.telegram.account_id or reply.fwd_from or reply.via_bot_id
                    or message_topic(reply) != (topic_id or 0) or utils.get_peer_id(reply.peer_id) != chat_id):
                return None
            rows = self.store.rows('SELECT * FROM group_messages WHERE chat_id=? AND telegram_id=? AND generation=? AND outgoing=1 AND topic_id=?',
                (chat_id,reply.id,generation,topic_id or 0))
            if not rows or rows[0]['text'] != (getattr(reply,'message','') or ''):
                return None
            source_id = rows[0]['reply_to']
            suffix = ' AND source_id=? AND response_id=?'
        rows = self.store.rows('''SELECT * FROM group_join_lists WHERE chat_id=? AND topic_id=? AND actor_id=? AND owner_id=?
            AND generation=? AND created>? AND response_id IS NOT NULL''' + suffix + ' ORDER BY created DESC LIMIT 1',
            params + ((source_id,reply.id) if suffix else ()))
        if rows:
            return json.loads(rows[0]['requesters'])
        if source_id is None:
            return None
        # Compatibility with lists sent before this feature. Verify server action and author,
        # rather than interpreting IDs supplied by a user or the model.
        original = self.store.rows('SELECT * FROM group_messages WHERE chat_id=? AND telegram_id=? AND generation=? AND topic_id=? AND sender_id=? AND outgoing=0 AND forwarded=0 AND created>?',
            (chat_id,source_id,generation,topic_id or 0,actor,time.time()-1800))
        actions = self.store.rows("SELECT result FROM group_admin_actions WHERE chat_id=? AND message_id=? AND status='sent'", (chat_id,source_id))
        if not original:
            return None
        text = getattr(reply,'message','') or ''
        for action in actions:
            try:
                if json.loads(action['result']).get('text') != text or not text.startswith('Заявки (до 50):\n'):
                    continue
                lines = text.splitlines()[1:]
                ids = [re.fullmatch(r'.+ · ([1-9][0-9]{0,19})(?: · @[A-Za-z][A-Za-z0-9_]{3,31})?', line) for line in lines]
                if lines and len(lines) <= 50 and all(ids):
                    return {match[1]: None for match in ids}
            except (ValueError,AttributeError):
                continue
        return None

    def mode_guard(self, mode):
        settings = self.store.settings()
        rows = self.store.rows('SELECT * FROM group_join_automation WHERE chat_id=?', (mode['chat_id'],))
        if (not rows or not rows[0]['enabled'] or rows[0]['revision'] != mode['revision']
                or mode['owner_id'] != settings['owner_id'] or not authorized(self.store,mode['actor_id'],mode['chat_id'])
                or self.authority_version(mode['actor_id'],mode['chat_id']) != mode['authority_version']
                or self.store.dialog_scope(mode['chat_id'],0)[2] != mode['generation']
                or not settings['enabled'] or not settings['group_enabled']
                or not self.store.contact(mode['chat_id'])['enabled'] or self.store.dialog_blocked(mode['chat_id'])):
            raise JoinApprovalRevoked('Разрешение на автоматический приём заявок отозвано')
        if not self.admin.telegram.ready():
            raise ConnectionError('Telegram временно отключён')

    def tick(self):
        if self.task and not self.task.done() or not self.admin.telegram.ready():
            return
        if self.store.rows('SELECT 1 FROM group_join_automation WHERE enabled=1 AND next_check<=? LIMIT 1', (time.time(),)):
            self.task = self.runtime.spawn(self.poll())

    async def poll(self):
        for mode in self.store.rows('SELECT * FROM group_join_automation WHERE enabled=1 AND next_check<=? ORDER BY next_check LIMIT 20', (time.time(),)):
            dispatched = False
            phase = 'проверка разрешения'
            def guard():
                self.mode_guard(mode)
            try:
                guard()
                if max(self.admin.cooldown_until,self.runtime.flood_until) > time.time():
                    continue
                async with self.admin.lock:
                    guard()
                    phase = 'получение группы'
                    # Keep FloodWaitError intact: the interactive RPC wrapper turns it
                    # into ValueError and previously disabled this persistent mode.
                    chat = group_entity(await asyncio.wait_for(self.admin.telegram.client.get_entity(mode['chat_id']),30))
                    if utils.get_peer_id(chat) != mode['chat_id']:
                        raise JoinApprovalRevoked('Полученная группа не совпадает с настроенной')
                    phase = 'проверка прав администратора'
                    perms = await asyncio.wait_for(self.admin.permissions(chat,self.admin.telegram.account_id),30)
                    if not self.admin.has_right(perms,'invite_users'):
                        raise JoinApprovalRevoked('У меня нет права администратора принимать заявки')
                    guard()
                    phase = 'чтение заявок'
                    page = await asyncio.wait_for(self.pending_page(chat),30)
                    guard()
                    if page.importers:
                        self.store.execute("UPDATE group_join_automation SET status='sending',updated=? WHERE chat_id=? AND revision=?", (time.time(),mode['chat_id'],mode['revision']))
                        dispatched = True
                        phase = 'приём заявок'
                        result = await asyncio.wait_for(self.admin.telegram.client(functions.messages.HideAllChatJoinRequestsRequest(chat,approved=True)),30)
                        if result is False:
                            raise ValueError('Telegram не подтвердил приём заявок')
                        self.store.event('Автоматически приняты ожидающие заявки в группе '+str(mode['chat_id']))
                    self.store.execute("UPDATE group_join_automation SET status='idle',next_check=?,updated=? WHERE chat_id=? AND revision=?", (time.time()+20,time.time(),mode['chat_id'],mode['revision']))
            except BaseException as error:
                retry_after = 60
                if isinstance(error,errors.FloodWaitError):
                    self.admin.cooldown_until = max(self.admin.cooldown_until,time.time()+error.seconds)
                    retry_after = max(retry_after,error.seconds)
                # A lost acknowledgement must never turn into an automatic repeat.
                uncertain = dispatched and not isinstance(error,errors.FloodWaitError)
                stop = uncertain or isinstance(error,(JoinApprovalRevoked,errors.ChatAdminRequiredError,errors.ChannelPrivateError))
                self.store.execute('UPDATE group_join_automation SET enabled=?,status=?,next_check=?,updated=? WHERE chat_id=? AND revision=?',
                    (0 if stop else 1,'uncertain' if uncertain else 'failed',time.time()+retry_after,time.time(),mode['chat_id'],mode['revision']))
                detail = ': '+str(error) if isinstance(error,JoinApprovalRevoked) else ''
                self.store.event('Автоприём заявок '+('остановлен' if stop else 'отложен')+' в группе '+str(mode['chat_id'])+': '+phase+', '+type(error).__name__+detail, 'error')
                if isinstance(error,asyncio.CancelledError):
                    raise
