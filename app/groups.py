"""Telegram group membership, invoked only through authenticated owner actions."""
from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit

from telethon import errors, functions, types, utils

from .voice import CallUnavailable


NAVIGATION_ACTIONS = {'join_group', 'leave_group', 'join_call'}


def parse_navigation_command(text):
    """Recognize explicit Telegram navigation without a model or link guessing."""
    text = re.sub(r'^(?:(?:дарья|даша|даш|дашка|дашенька|дашуль|дашуля)[,\s]+|пожалуйста[,\s]+)+', '', text.strip(), flags=re.I)
    verbs = r'вступи|вступить|присоединись|зайди|заходи|подключись|выйди|покинь'
    words = (r'туда|сюда|в|из|к|по|этой|этот|эту|ссылке|ссылку|группу|группе|группы|'
             r'чат|чату|чате|звонок|звонку|звонка|созвон|созвону|созвона|'
             r'голосовой|голосовому|голосового|видеочат|видеочату|видеочата|конференцию|конференции')
    match = re.fullmatch(r'(?P<prefix>(?:' + verbs + r')(?:\s+(?:' + words + r'))*)\s*[:,-]?\s+'
                         r'(?P<target>\S+?)(?:\s+(?:и|в режиме)\s+(?P<mode>слушай|говори|разговаривай|слушать|разговаривать|talk|listen))?[.!\s]*', text, re.I)
    if not match:
        return None
    value = match['target'].strip('«»"()').rstrip('.!,')
    if not re.match(r'(?:https://)?(?:t\.me|telegram\.me)/|@[A-Za-z]|-?\d+$|[A-Za-z][A-Za-z0-9_]{3,31}$', value):
        return None
    prefix = match['prefix'].casefold()
    calls = bool(re.search(r'звон|голосов|видеочат|конференц', prefix) or
                 re.match(r'(?:https://)?(?:t\.me|telegram\.me)/call/', value) or '?voicechat=' in value)
    parsed = target(value, calls=calls)
    if prefix.startswith(('выйди', 'покинь')):
        if calls:
            return None
        return {'action': 'leave_group', 'target': value}
    if match['mode'] and not calls:
        return None
    action = 'join_call' if calls or parsed['kind'] == 'conference' else 'join_group'
    result = {'action': action, 'target': value}
    if match['mode']:
        result['mode'] = 'listen' if match['mode'].casefold() in {'слушай', 'слушать', 'listen'} else 'talk'
    return result


def membership_text(result):
    return {'joined': 'Вступила в группу «', 'already_joined': 'Уже состою в группе «',
            'left': 'Вышла из группы «', 'pending': 'Отправила заявку в группу «'}[result['status']] + result['name'] + (
            '». Жду одобрения администратора.' if result['status'] == 'pending' else '».')


def target(value, *, calls=False):
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 500:
        raise CallUnavailable('Укажи @username, ID группы или ссылку Telegram')
    value = value.strip()
    if value.startswith(('t.me/', 'telegram.me/')):
        value = 'https://' + value
    if value.startswith('https://'):
        url = urlsplit(value)
        if url.hostname not in {'t.me', 'telegram.me'} or url.username or url.password or url.port or url.fragment:
            raise CallUnavailable('Нужна ссылка t.me без постороннего адреса')
        path = url.path.strip('/')
        if calls and re.fullmatch(r'call/[A-Za-z0-9_-]{5,256}', path) and not url.query:
            return {'kind': 'conference', 'value': path[5:]}
        if re.fullmatch(r'\+[A-Za-z0-9_-]{5,256}|joinchat/[A-Za-z0-9_-]{5,256}', path) and not url.query:
            return {'kind': 'invite', 'value': path[1:] if path.startswith('+') else path[9:]}
        if re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{3,31}', path):
            query = parse_qs(url.query, keep_blank_values=True)
            if query and (not calls or set(query) != {'voicechat'} or len(query['voicechat']) != 1
                          or not re.fullmatch(r'[A-Za-z0-9_-]{0,256}', query['voicechat'][0])):
                raise CallUnavailable('Нужна ссылка на группу или её голосовой чат')
            return {'kind': 'public', 'value': path, 'invite_hash': query.get('voicechat', [None])[0]}
    if re.fullmatch(r'@[A-Za-z][A-Za-z0-9_]{3,31}|[A-Za-z][A-Za-z0-9_]{3,31}|-?[0-9]{1,20}', value):
        return {'kind': 'public', 'value': int(value) if value.lstrip('-').isdigit() else value.lstrip('@')}
    raise CallUnavailable('Укажи @username, ID или действующую ссылку Telegram')


def group_entity(entity):
    if not (isinstance(entity, types.Chat) or isinstance(entity, types.Channel) and entity.megagroup):
        raise CallUnavailable('Это не группа Telegram')
    return entity


class Groups:
    def __init__(self, telegram):
        self.telegram = telegram
        self.store = telegram.store

    def contact(self, entity):
        group_entity(entity)
        return self.store.upsert_contact(utils.get_peer_id(entity), entity.title,
                                         getattr(entity, 'username', '') or '', 'group')

    async def resolve(self, parsed):
        tg = self.telegram
        if not tg.ready():
            raise CallUnavailable('Сначала подключи Telegram')
        if parsed['kind'] == 'invite':
            invite = await tg.rpc(lambda: tg.client(functions.messages.CheckChatInviteRequest(parsed['value'])))
            if not isinstance(invite, types.ChatInviteAlready):
                raise CallUnavailable('Сначала попроси вступить в группу по этой ссылке')
            return group_entity(invite.chat)
        if parsed['kind'] != 'public':
            raise CallUnavailable('Нужна ссылка на группу')
        entity = await tg.rpc(lambda: tg.client.get_entity(parsed['value']))
        return group_entity(entity)

    async def join(self, value, guard):
        parsed = target(value)
        tg = self.telegram
        guard()
        if not tg.ready():
            raise CallUnavailable('Сначала подключи Telegram')
        if parsed['kind'] == 'invite':
            invite = await tg.rpc(lambda: tg.client(functions.messages.CheckChatInviteRequest(parsed['value'])))
            if isinstance(invite, types.ChatInviteAlready):
                guard()
                contact = self.contact(invite.chat)
                return {'status': 'already_joined', 'group_id': contact['id'], 'name': contact['name']}
            if getattr(invite, 'broadcast', False):
                raise CallUnavailable('Это канал, нужна группа')
            if getattr(invite, 'subscription_pricing', None) or getattr(invite, 'subscription_form_id', None):
                raise CallUnavailable('Вступление платное; оплату через эту команду не выполняю')
            guard()
            try:
                result = await tg.rpc(lambda: tg.client(functions.messages.ImportChatInviteRequest(parsed['value'])))
            except errors.InviteRequestSentError:
                return {'status': 'pending', 'name': invite.title}
            entity = next((chat for chat in getattr(result, 'chats', []) if isinstance(chat, types.Chat)
                           or isinstance(chat, types.Channel) and chat.megagroup), None)
            if entity is None:
                # Telegram may acknowledge the join without a chats vector.
                confirmed = await tg.rpc(lambda: tg.client(functions.messages.CheckChatInviteRequest(parsed['value'])))
                if isinstance(confirmed, types.ChatInviteAlready):
                    entity = confirmed.chat
            if entity is None:
                raise CallUnavailable('Telegram не подтвердил вступление в группу')
        else:
            entity = await self.resolve(parsed)
            if not getattr(entity, 'left', False):
                guard()
                contact = self.contact(entity)
                return {'status': 'already_joined', 'group_id': contact['id'], 'name': contact['name']}
            if isinstance(entity, types.Chat):
                raise CallUnavailable('Для возвращения в эту группу нужна пригласительная ссылка')
            guard()
            try:
                result = await tg.rpc(lambda: tg.client(functions.channels.JoinChannelRequest(entity)))
            except errors.InviteRequestSentError:
                return {'status': 'pending', 'name': entity.title}
            except errors.UserAlreadyParticipantError:
                result = None
            if result:
                entity = next((chat for chat in getattr(result, 'chats', []) if getattr(chat, 'id', None) == entity.id), entity)
        contact = self.contact(entity)
        self.store.execute('UPDATE contacts SET enabled=1 WHERE id=?', (contact['id'],))
        return {'status': 'joined', 'group_id': contact['id'], 'name': contact['name']}

    async def leave(self, value, guard):
        entity = await self.resolve(target(value))
        tg = self.telegram
        guard()
        if isinstance(entity, types.Channel):
            await tg.rpc(lambda: tg.client(functions.channels.LeaveChannelRequest(entity)))
        else:
            await tg.rpc(lambda: tg.client(functions.messages.DeleteChatUserRequest(entity.id, types.InputUserSelf())))
        contact = self.contact(entity)
        self.store.execute('UPDATE contacts SET enabled=0,proactive=0 WHERE id=?', (contact['id'],))
        return {'status': 'left', 'group_id': contact['id'], 'name': contact['name']}
