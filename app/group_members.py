"""Public member information bound to the current Telegram group."""
from __future__ import annotations

import asyncio
import copy
import json
import re
import time

from telethon import errors, functions, types, utils

from .authority import authorized
from .group_conversation import topic_id as message_topic
from .groups import group_entity
from .privacy import redact_locations


DELETED_ACTIONS = {'kick_deleted_accounts', 'auto_kick_deleted_accounts'}


class DeletedCleanupRevoked(ValueError):
    """A persistent permission to remove deleted accounts is no longer valid."""


def parse_deleted_command(text):
    low = text.casefold().replace('ё', 'е').strip().rstrip('.!? ')
    low = re.sub(r'\s+', ' ', low)
    low = re.sub(r'^(?:можешь|можешь ли ты|давай)\s+', '', low)
    low = re.sub(r'[, ]+пожалуйста$', '', low)
    low = re.sub(r'\s+(?:тут|здесь|в этой группе|в этом чате|из этой группы|из этого чата|из группы|из чата)$', '', low)
    target = r'(?:все\s+|всех\s+)?(?:удаленн(?:ые|ых)\s+(?:аккаунт\w*|пользовател\w*|участник\w*|юзер\w*)|собач(?:ек|ки))'
    auto = r'(?:автоматически|автоматом|всегда|постоянно)(?:\s+(?:и\s+)?(?:автоматически|автоматом|всегда|постоянно))*'
    if (re.fullmatch(r'(?:выключи|выключить|отключи|отключить|останови|остановить)\s+(?:автоудаление|авто\s*удаление|автоочистку|авто\s*очистку|авточистку|авто\s*чистку|автокик|авто\s*кик|автоматическое\s+удаление|автоматическую\s+(?:очистку|чистку))\s+' + target, low)
            or re.fullmatch(r'(?:не|больше\s+не|перестань)\s+(?:удаляй|удалять|исключай|исключать|кикай|кикать|чисти|чистить|вычищай|вычищать|убирай|убирать)\s+' + target + r'(?:\s+' + auto + r')?', low)
            or low in {'/autodeleted off', '/clean_deleted off'}):
        return {'action': 'auto_kick_deleted_accounts', 'enabled': False}
    if (re.fullmatch(r'(?:удаляй|удалять|исключай|исключать|кикай|кикать|чисти|чистить|вычищай|вычищать|убирай|убирать)\s+' + target + r'\s+' + auto, low)
            or re.fullmatch(auto + r'\s+(?:удаляй|удалять|исключай|исключать|кикай|кикать|чисти|чистить|вычищай|вычищать|убирай|убирать)\s+' + target, low)
            or re.fullmatch(r'(?:включи|включить)\s+(?:автоудаление|авто\s*удаление|автоочистку|авто\s*очистку|авточистку|авто\s*чистку|автокик|авто\s*кик|автоматическое\s+удаление|автоматическую\s+(?:очистку|чистку))\s+' + target, low)
            or low in {'/autodeleted on', '/clean_deleted on'}):
        return {'action': 'auto_kick_deleted_accounts', 'enabled': True}
    if (re.fullmatch(r'(?:удали|удалить|исключи|исключить|кикни|кикнуть|очисти|очистить|почисти|почистить|вычисти|вычистить|убери|убрать)\s+' + target, low)
            or re.fullmatch(r'(?:очисти|почисти)\s+(?:группу|чат)\s+от\s+(?:удаленн\w+\s+(?:аккаунт\w*|пользовател\w*|участник\w*)|собачек)', low)
            or low in {'/kick_deleted', '/clean_deleted'}):
        return {'action': 'kick_deleted_accounts'}
    return None


def parse_member_question(text):
    plain=re.sub(r'^(?:(?:дарья|даша|даш|дашка|дашенька)[,\s]+)+','',text.strip(),flags=re.I)
    plain=plain.casefold().replace('ё','е').rstrip(' .!?')
    if re.fullmatch(r'(?:кто|хто)(?:\s+это)?\s+(?:зашел|зашла|вступил|вступила)(?:\s+(?:в группу|в чат|сюда))?',plain):
        return {'action':'member_info','user':'recent'}
    if plain in {'кто это','хто это','кто этот участник','кто эта участница'}:
        return {'action':'member_info','user':'reply'}
    match=re.fullmatch(r'(?:кто|хто)\s+(?:такой|такая|этот|эта)\s+(.{1,100})',plain)
    if match and match[1] not in {'ты','я'}:
        value=match[1].strip()
        if re.fullmatch(r'@[A-Za-z][A-Za-z0-9_]{3,31}|[1-9][0-9]{0,19}',value):
            return {'action':'member_info','user':value}
        return {'action':'member_info','text':value}
    return None


def joined_users(message):
    action=getattr(message,'action',None)
    if isinstance(action,types.MessageActionChatAddUser):
        return list(dict.fromkeys(action.users))
    if isinstance(action,(types.MessageActionChatJoinedByLink,types.MessageActionChatJoinedByRequest)):
        return [message.sender_id] if isinstance(message.from_id,types.PeerUser) else []
    return []


def normalized_name(value):
    letters=dict(zip('абвгдеёжзийклмнопрстуфхцчшщъыьэюя',
                     ['a','b','v','g','d','e','e','zh','z','i','i','k','l','m','n','o','p','r','s','t','u','f','h','ts','ch','sh','shch','','y','','e','yu','ya']))
    value=''.join(letters.get(char,char) for char in value.casefold())
    return ' '.join(re.findall(r'[a-z0-9]+',value))


async def member_info(runtime,chat,op,guard,*,reply=None,topic_id=0,before_id=None):
    client=runtime.telegram.client
    chat_id=utils.get_peer_id(chat)
    scope=runtime.store.dialog_scope(chat_id,0)
    def check():
        guard()
        if not runtime.store.scope_active(scope):
            raise ValueError('Контекст группы изменился; задай вопрос снова')
    check()
    if reply and (utils.get_peer_id(reply.peer_id)!=chat_id or message_topic(reply)!=(topic_id or 0)):
        raise ValueError('Ответь на сообщение участника в этой группе и теме')
    joined=False
    historical=False
    recipient=op.user
    async with asyncio.timeout(25):
        if recipient=='reply':
            if not reply:
                raise ValueError('Ответь на сообщение участника или сообщение о вступлении, либо укажи @username')
            users=joined_users(reply)
            joined=bool(users)
            if len(users)>1:
                raise ValueError('В сообщении несколько новых участников. Укажи @username нужного человека')
            recipient=users[0] if users else reply.sender_id
            historical=True
        elif recipient=='recent' or op.text:
            messages=await client.get_messages(chat,limit=100,
                **({'max_id':before_id} if before_id else {}),
                **({'reply_to':topic_id} if topic_id else {}))
            check()
            messages=messages if isinstance(messages,(list,tuple)) else []
            if recipient=='recent':
                users=next((users for message in messages if isinstance(message,(types.Message,types.MessageService)) and utils.get_peer_id(message.peer_id)==chat_id
                    and message_topic(message)==(topic_id or 0) and (users:=joined_users(message))),[])
                if len(users)!=1:
                    raise ValueError('Не вижу однозначного сообщения о вступлении. Ответь на него или укажи @username')
                recipient=users[0];joined=True
                historical=True
            else:
                for message in messages:
                    runtime.group_conversation.remember(chat_id,message)
                query=normalized_name(op.text)
                people=runtime.store.group_participants(chat_id)
                matches={person['user_id'] for person in people
                    if any(normalized_name(name)==query for name in person['names']+person['usernames'])}
                if len(matches)!=1:
                    raise ValueError('Не могу однозначно найти участника по этому имени. Ответь на его сообщение или укажи @username')
                recipient=matches.pop()
                historical=True
        if recipient in {None,'recent','reply'} or recipient==777000:
            raise ValueError('Нужен участник этой группы')
        if isinstance(recipient,str) and recipient.isdigit():recipient=int(recipient)
        user=await client.get_entity(recipient)
        check()
        if not isinstance(user,types.User) or user.id==777000:
            raise ValueError('Нужен аккаунт участника этой группы')
        current_member=True
        try:
            permissions=await client.get_permissions(chat,user)
        except errors.UserNotParticipantError:
            current_member=False
            permissions=None
        check()
        participant=getattr(permissions,'participant',None)
        if isinstance(participant,types.ChannelParticipantLeft) or getattr(getattr(participant,'banned_rights',None),'view_messages',False):
            current_member=False
        if not current_member and not historical:
            raise ValueError('Этот аккаунт сейчас не является участником этой группы')
        name=' '.join(value for value in [user.first_name,user.last_name] if value) or str(user.id)
        text=redact_locations(name)+' — '+('аккаунт из истории этой группы' if not current_member else 'бот' if user.bot else 'участник этой группы')+'.'
        if user.username:text+='\n@'+user.username
        text+='\nTelegram ID: '+str(user.id)
        if joined:text+='\nЭто аккаунт из сообщения о вступлении.'
        if not current_member:
            text+='\nСейчас этот аккаунт уже не состоит в группе.'
            return {'text':text,'user_id':user.id,'member_info':True,'current_member':False}
        # Only public biography is returned; no phone, birthday, personal chats,
        # contact-specific status, or account configuration enters group output.
        if user.access_hash is None and not getattr(user,'is_self',False):
            return {'text':text,'user_id':user.id,'member_info':True}
        try:
            full=await asyncio.wait_for(client(functions.users.GetFullUserRequest(utils.get_input_user(user))),5)
            check()
            bio=redact_locations(getattr(getattr(full,'full_user',None),'about','') or '')
            if bio:text+='\nБио: '+bio[:500]
        except (errors.RPCError,TimeoutError):
            check()
        return {'text':text,'user_id':user.id,'member_info':True}


class GroupDeletedAccounts:
    SCAN_INTERVAL = 300
    PAGE_LIMIT = 200
    MAX_PAGES = 25
    MAX_KICKS = 50

    def __init__(self, admin):
        self.admin, self.runtime, self.store = admin, admin.runtime, admin.store
        self.task = None

    def authority_version(self, actor, chat_id):
        if actor == self.store.settings()['owner_id']:
            return 'owner'
        return json.dumps(self.store.rows('SELECT chat_id,enabled,updated FROM controller_permissions WHERE owner_id=? AND user_id=? AND chat_id IN (?,0) ORDER BY chat_id',
            (self.store.settings()['owner_id'], actor, chat_id)), sort_keys=True)

    def policy(self, chat_id):
        rows = self.store.rows('SELECT * FROM group_deleted_automation WHERE chat_id=? AND enabled=1', (chat_id,))
        row = rows[0] if rows else None
        if (not row or row['owner_id'] != self.store.settings()['owner_id']
                or row['generation'] != self.store.dialog_scope(chat_id, 0)[2]
                or not authorized(self.store, row['actor_id'], chat_id)
                or self.authority_version(row['actor_id'], chat_id) != row['authority_version']):
            return None
        return row

    async def kick_deleted_user(self, chat, user, participant, guard):
        client = self.admin.telegram.client
        async def send(request):
            guard()
            result = await asyncio.wait_for(client(request), 30)
            if result is False:
                raise ValueError('Telegram не подтвердил исключение удалённого аккаунта')
            return result
        if isinstance(chat, types.Channel):
            previous = copy.copy(getattr(participant, 'banned_rights', None)) or types.ChatBannedRights(None)
            banned = copy.copy(previous)
            banned.view_messages = True
            banned.until_date = None
            await send(functions.channels.EditBannedRequest(chat, user, banned))
            await send(functions.channels.EditBannedRequest(chat, user, previous))
        else:
            await send(functions.messages.DeleteChatUserRequest(chat.id, user))

    async def scan_and_kick(self, chat, guard, *, on_before_first_kick=None):
        client = self.admin.telegram.client
        self_id = self.admin.telegram.account_id
        owner_id = self.store.settings()['owner_id']
        removed = []
        seen = set()
        if isinstance(chat, types.Channel):
            offset = 0
            for _ in range(self.MAX_PAGES):
                guard()
                page = await asyncio.wait_for(client(functions.channels.GetParticipantsRequest(
                    chat, types.ChannelParticipantsRecent(), offset, self.PAGE_LIMIT, hash=0)), 30)
                guard()
                participants = list(getattr(page, 'participants', None) or [])
                users = {u.id: u for u in (getattr(page, 'users', None) or []) if isinstance(u, types.User)}
                for p in participants:
                    peer = getattr(p, 'peer', None)
                    uid = getattr(p, 'user_id', None) or (peer.user_id if isinstance(peer, types.PeerUser) else None)
                    if not uid or uid in seen or uid in {self_id, owner_id, 777000}:
                        continue
                    seen.add(uid)
                    if isinstance(p, (types.ChannelParticipantCreator, types.ChannelParticipantAdmin, types.ChannelParticipantLeft)) or getattr(p, 'left', False):
                        continue
                    if getattr(getattr(p, 'banned_rights', None), 'view_messages', False):
                        continue
                    user = users.get(uid)
                    if not user or not getattr(user, 'deleted', False):
                        continue
                    if on_before_first_kick and not removed:
                        on_before_first_kick()
                    await self.kick_deleted_user(chat, user, p, guard)
                    removed.append(uid)
                    if len(removed) >= self.MAX_KICKS:
                        return removed
                if len(participants) < self.PAGE_LIMIT:
                    break
                offset += len(participants)
        else:
            guard()
            users = await asyncio.wait_for(client.get_participants(chat, limit=self.PAGE_LIMIT), 30)
            guard()
            for user in users or []:
                if not isinstance(user, types.User) or user.id in seen or user.id in {self_id, owner_id, 777000}:
                    continue
                seen.add(user.id)
                if not getattr(user, 'deleted', False):
                    continue
                try:
                    perms = await asyncio.wait_for(self.admin.permissions(chat, user), 30)
                except errors.UserNotParticipantError:
                    continue
                if perms and (perms.is_creator or perms.is_admin):
                    continue
                if on_before_first_kick and not removed:
                    on_before_first_kick()
                await self.kick_deleted_user(chat, user, getattr(perms, 'participant', None), guard)
                removed.append(user.id)
                if len(removed) >= self.MAX_KICKS:
                    break
        return removed

    async def execute(self, chat, op, guard, *, actor_id=None):
        chat_id = utils.get_peer_id(chat)
        actor = actor_id if actor_id is not None else self.store.settings()['owner_id']
        generation = self.store.dialog_scope(chat_id, 0)[2]
        guard()
        if op.action == 'auto_kick_deleted_accounts':
            if op.enabled and (not self.store.settings()['enabled'] or not self.store.settings()['group_enabled']
                               or not self.store.contact(chat_id)['enabled'] or self.store.dialog_blocked(chat_id)):
                raise ValueError('Группа отключена; сначала включи ее в панели')
            self.store.execute('''INSERT INTO group_deleted_automation
                (chat_id,enabled,owner_id,actor_id,generation,authority_version,revision,next_check,status,updated)
                VALUES (?,?,?,?,?,?,1,0,'idle',?) ON CONFLICT(chat_id) DO UPDATE SET
                enabled=excluded.enabled,owner_id=excluded.owner_id,actor_id=excluded.actor_id,
                generation=excluded.generation,authority_version=excluded.authority_version,
                revision=group_deleted_automation.revision+1,next_check=0,status='idle',updated=excluded.updated''',
                (chat_id, int(op.enabled), self.store.settings()['owner_id'], actor, generation, self.authority_version(actor, chat_id), time.time()))
            if op.enabled:
                self.tick()
            return {'text': 'Автоудаление удалённых аккаунтов включено в этой группе: проверяю участников и исключаю удалённые аккаунты автоматически.' if op.enabled
                    else 'Автоудаление удалённых аккаунтов в этой группе выключено.',
                    'enabled': bool(op.enabled)}
        if op.action == 'kick_deleted_accounts':
            removed = await self.scan_and_kick(chat, guard)
            return {'text': f'Исключила удалённые аккаунты из этой группы: {len(removed)}.' if removed
                    else 'Удалённых аккаунтов среди проверенных участников этой группы не найдено.',
                    'removed': len(removed), 'removed_ids': removed}
        raise ValueError('Неизвестная команда очистки удалённых аккаунтов')

    def mode_guard(self, mode):
        settings = self.store.settings()
        rows = self.store.rows('SELECT * FROM group_deleted_automation WHERE chat_id=?', (mode['chat_id'],))
        if (not rows or not rows[0]['enabled'] or rows[0]['revision'] != mode['revision']
                or mode['owner_id'] != settings['owner_id'] or not authorized(self.store, mode['actor_id'], mode['chat_id'])
                or self.authority_version(mode['actor_id'], mode['chat_id']) != mode['authority_version']
                or self.store.dialog_scope(mode['chat_id'], 0)[2] != mode['generation']
                or not settings['enabled'] or not settings['group_enabled']
                or not self.store.contact(mode['chat_id'])['enabled'] or self.store.dialog_blocked(mode['chat_id'])):
            raise DeletedCleanupRevoked('Разрешение на автоматическое удаление удалённых аккаунтов отозвано')
        if not self.admin.telegram.ready():
            raise ConnectionError('Telegram временно отключён')

    def tick(self):
        if self.task and not self.task.done() or not self.admin.telegram.ready():
            return
        if self.store.rows('SELECT 1 FROM group_deleted_automation WHERE enabled=1 AND next_check<=? LIMIT 1', (time.time(),)):
            self.task = self.runtime.spawn(self.poll())

    async def poll(self):
        for mode in self.store.rows('SELECT * FROM group_deleted_automation WHERE enabled=1 AND next_check<=? ORDER BY next_check LIMIT 20', (time.time(),)):
            dispatched = False
            phase = 'проверка разрешения'
            def guard():
                self.mode_guard(mode)
            def mark_sending():
                nonlocal dispatched, phase
                self.store.execute("UPDATE group_deleted_automation SET status='sending',updated=? WHERE chat_id=? AND revision=?",
                    (time.time(), mode['chat_id'], mode['revision']))
                dispatched = True
                phase = 'исключение удалённых аккаунтов'
            try:
                guard()
                if max(self.admin.cooldown_until, self.runtime.flood_until) > time.time():
                    continue
                async with self.admin.lock:
                    guard()
                    phase = 'получение группы'
                    chat = group_entity(await asyncio.wait_for(self.admin.telegram.client.get_entity(mode['chat_id']), 30))
                    if utils.get_peer_id(chat) != mode['chat_id']:
                        raise DeletedCleanupRevoked('Полученная группа не совпадает с настроенной')
                    phase = 'проверка прав администратора'
                    perms = await asyncio.wait_for(self.admin.permissions(chat, self.admin.telegram.account_id), 30)
                    if not self.admin.has_right(perms, 'ban_users'):
                        raise DeletedCleanupRevoked('У меня нет права администратора исключать участников')
                    guard()
                    phase = 'проверка участников'
                    removed = await self.scan_and_kick(chat, guard, on_before_first_kick=mark_sending)
                    if removed:
                        self.store.event(f'Автоматически исключены удалённые аккаунты ({len(removed)}) в группе ' + str(mode['chat_id']))
                    self.store.execute("UPDATE group_deleted_automation SET status='idle',next_check=?,updated=? WHERE chat_id=? AND revision=?",
                        (time.time() + self.SCAN_INTERVAL, time.time(), mode['chat_id'], mode['revision']))
            except BaseException as error:
                retry_after = 120
                if isinstance(error, errors.FloodWaitError):
                    self.admin.cooldown_until = max(self.admin.cooldown_until, time.time() + error.seconds)
                    retry_after = max(retry_after, error.seconds)
                uncertain = dispatched and not isinstance(error, errors.FloodWaitError)
                stop = uncertain or isinstance(error, (DeletedCleanupRevoked, errors.ChatAdminRequiredError, errors.ChannelPrivateError))
                self.store.execute('UPDATE group_deleted_automation SET enabled=?,status=?,next_check=?,updated=? WHERE chat_id=? AND revision=?',
                    (0 if stop else 1, 'uncertain' if uncertain else 'failed', time.time() + retry_after, time.time(), mode['chat_id'], mode['revision']))
                detail = ': ' + str(error) if isinstance(error, DeletedCleanupRevoked) else ''
                self.store.event('Автоудаление удалённых аккаунтов ' + ('остановлено' if stop else 'отложено') +
                                 ' в группе ' + str(mode['chat_id']) + ': ' + phase + ', ' + type(error).__name__ + detail, 'error')
                if isinstance(error, asyncio.CancelledError):
                    raise
