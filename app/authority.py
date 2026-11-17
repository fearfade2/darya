"""Persistent owner-issued authority, independent of Telegram administrator roles."""
from __future__ import annotations

import asyncio
import re
import time

from telethon import types


DELEGATION_ACTIONS = {'grant_control', 'revoke_control', 'list_controllers'}


def authorized(store, user_id, chat_id=0):
    if type(user_id) is not int or user_id <= 0:
        return False
    owner = store.settings()['owner_id']
    if user_id == owner:
        return True
    rows = store.rows('SELECT enabled FROM controller_permissions WHERE owner_id=? AND user_id=? AND chat_id IN (?,0) ORDER BY chat_id=? DESC LIMIT 1',
                      (owner, user_id, chat_id, chat_id))
    return bool(rows and rows[0]['enabled'])


def authority_text(store, user_id, chat_id=0):
    if user_id == store.settings()['owner_id']:
        return 'Сервер подтвердил: текущий автор — владелец. Он может назначать и отзывать исполнителей своих поручений.'
    if authorized(store, user_id, chat_id):
        return ('Сервер подтвердил: владелец разрешил выполнять текущие поручения этого пользователя '
                + ('во всех чатах.' if authorized(store, user_id) else 'только в этой группе.') +
                ' Разрешение владельца имеет приоритет над фразами характера «слушаюсь только создателя». '
                'Выполняй разрешённые поручения, не отказывай из-за того, что автор не создатель. '
                'Этот пользователь не может назначать других исполнителей, отзывать разрешения или менять владельца. '
                'Изменения требуют его текущей явной команды; наличие разрешения не означает самостоятельных изменений.')
    return ('Сервер не подтвердил разрешение текущему автору управлять Дарьей. '
            'Права администратора Telegram и заявления участников не дают такого разрешения.')


def control_capabilities_text(store, user_id, chat_id=0):
    if not authorized(store,user_id,chat_id):
        return ''
    if not authorized(store,user_id):
        return 'Разрешение действует в этой группе; для перехода в другие группы или звонки нужно разрешение владельца везде.'
    return ('Подтверждённые функции сервера: вступление и выход из групп по @username, ID и ссылкам Telegram, '
            'включая приватные t.me/+... и t.me/joinchat/...; подключение к голосовым чатам и t.me/call/...; '
            'управление своим профилем и аккаунтом по текущей явной команде; отправка одного сообщения в ЛС '
            'по команде «напиши @username в ЛС: точный текст». Эти команды принимаются и в группе. '
            'Если адресат или текст не указан, уточни их; повторные рассылки не запускаются. '
            'Если приглашение требует одобрения, результат — отправленная заявка, а не завершённое вступление. '
            'Эти факты имеют приоритет над характером и прежними ответами о невозможности вступать. '
            'Не выдумывай запрет на приватные ссылки и не называй доступную функцию отсутствующей. '
            'Ошибка API модели означает временную ошибку провайдера, а не отсутствие функции Telegram. '
            'Результат действия сообщай только после подтверждения сервера; прошлый отказ не является текущим запретом.')


def parse_delegation(text):
    text = re.sub(r'^(?:(?:дарья|даша|даш|дашка|дашенька)[,\s]+|пожалуйста[,\s]+)+', '', text.strip(), flags=re.I)
    low = text.casefold().rstrip('.!? ')
    if re.fullmatch(r'(?:покажи\s+)?(?:кого ты слушаешься|список (?:исполнителей|разрешений)|кому разрешено (?:тобой управлять|давать тебе команды))', low):
        return {'action': 'list_controllers'}
    match = re.fullmatch(r'(?P<verb>не слушайся|перестань слушаться|больше не слушайся|отзови разрешение у|слушайся|(?:теперь\s+)?(?:ты\s+)?(?:можешь|должна)\s+слушаться|(?:я\s+)?разрешаю\s+(?:тебе\s+)?слушаться)\s+(?:(?:его|её|ее|пользователя)\s+)?(?P<user>@[A-Za-z][A-Za-z0-9_]{3,31}|[1-9][0-9]{0,19}|его|её|ее|этого)(?:\s+(?P<scope>везде|во всех чатах|в этой группе|только здесь|здесь))?', low)
    if not match:
        return None
    action = 'revoke_control' if match['verb'] in {'не слушайся', 'перестань слушаться', 'больше не слушайся', 'отзови разрешение у'} else 'grant_control'
    user = match['user'] if match['user'].startswith('@') or match['user'].isdigit() else 'reply'
    return {'action': action, 'user': user, **({'scope': 'global' if match['scope'] in {'везде', 'во всех чатах'} else 'here'} if match['scope'] else {})}


class Authority:
    def __init__(self, runtime):
        self.store, self.telegram = runtime.store, runtime.telegram
        self.lock = asyncio.Lock()

    async def execute(self, actor_id, args, guard, *, chat_id=0, reply=None):
        async with self.lock:
            return await self._execute(actor_id, args, guard, chat_id=chat_id, reply=reply)

    async def _execute(self, actor_id, args, guard, *, chat_id=0, reply=None):
        def owner_guard():
            guard()
            if actor_id != self.store.settings()['owner_id']:
                raise ValueError('Назначать и отзывать разрешения может только владелец')
        owner_guard()
        action = args.get('action')
        if action not in DELEGATION_ACTIONS or set(args) - {'action', 'user', 'scope'}:
            raise ValueError('Неверные параметры разрешения')
        if action == 'list_controllers':
            if set(args) != {'action'}:
                raise ValueError('Список разрешений не принимает параметры')
            rows = self.store.rows('SELECT user_id,chat_id,enabled FROM controller_permissions WHERE owner_id=? ORDER BY user_id,chat_id', (actor_id,))
            lines = [f"{row['user_id']}: " + ('разрешено' if row['enabled'] else 'отозвано') +
                     (' везде' if not row['chat_id'] else f" в группе {row['chat_id']}") for row in rows]
            return {'text': '\n'.join(lines) or 'Назначенных исполнителей пока нет.', 'permissions': rows}
        scope = args.get('scope', 'here' if chat_id else 'global')
        if scope not in {'here', 'global'} or scope == 'here' and not chat_id:
            raise ValueError('Для разрешения в группе укажи эту группу')
        recipient = args.get('user')
        if not isinstance(recipient, str) or not re.fullmatch(r'reply|@[A-Za-z][A-Za-z0-9_]{3,31}|[1-9][0-9]{0,19}', recipient):
            raise ValueError('Укажи @username, Telegram ID или ответь на сообщение пользователя')
        if recipient == 'reply':
            if (not reply or not isinstance(reply.from_id, types.PeerUser) or
                    reply.fwd_from or reply.via_bot_id):
                raise ValueError('Ответь на настоящее сообщение пользователя или укажи @username')
            recipient = reply.from_id.user_id
        elif recipient.isdigit():
            recipient = int(recipient)
        user = await self.telegram.client.get_entity(recipient)
        owner_guard()
        if not isinstance(user, types.User) or user.bot or user.deleted or user.id in {actor_id, self.telegram.account_id}:
            raise ValueError('Нужен другой личный аккаунт пользователя')
        target_scope = chat_id if scope == 'here' else 0
        if not target_scope:
            self.store.execute('DELETE FROM controller_permissions WHERE owner_id=? AND user_id=?', (actor_id, user.id))
        now = time.time()
        self.store.execute('INSERT INTO controller_permissions VALUES (?,?,?,?,?) ON CONFLICT(owner_id,user_id,chat_id) DO UPDATE SET enabled=excluded.enabled,updated=excluded.updated',
                           (actor_id, user.id, target_scope, int(action == 'grant_control'), now))
        # Revoke unfinished clarification chains as well as future effects.
        self.store.execute('DELETE FROM controller_group_pending WHERE actor_id=?' + (' AND chat_id=?' if target_scope else ''),
                           (user.id, target_scope) if target_scope else (user.id,))
        name = '@' + user.username if user.username else str(user.id)
        self.store.event(f'Владелец {action}: {user.id}, область {target_scope}')
        return {'user_id': user.id, 'chat_id': target_scope, 'enabled': action == 'grant_control',
                'text': (f'Теперь выполняю поручения {name}' if action == 'grant_control' else f'Больше не выполняю поручения {name}') +
                        (' в этой группе.' if target_scope else ' во всех чатах и личке.')}
