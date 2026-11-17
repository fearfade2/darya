"""Personal Telegram blocks, bound to the actual author of a fresh conversation turn."""
import asyncio
import time

from telethon import errors, functions, types

from .session_guard import SECURITY_PEER
from .authority import authorized


REASONS = {'insults': 'оскорбления', 'harassment': 'навязчивое давление',
           'threats': 'угрозы', 'boundaries': 'нарушение личных границ'}
WARNING = 'Мне неприятно такое общение. Если продолжишь, я тебя заблокирую.'


class ContactBlocks:
    def __init__(self, runtime):
        self.runtime = runtime
        self.store = runtime.store
        self.telegram = runtime.telegram
        self.lock = asyncio.Lock()

    def request(self, chat_id, message_id):
        if not self.store.settings()['self_protection_enabled']:
            raise ValueError('Самостоятельная блокировка выключена владельцем')
        if not self.store.settings()['enabled'] or not self.store.contact(chat_id)['enabled']:
            raise ValueError('Общение выключено владельцем')
        state = self.runtime.reply_states.get(chat_id)
        if not state or state['telegram_id'] != message_id or state.get('superseded'):
            raise ValueError('Блокировать можно только автора текущей реплики')
        rows = self.store.rows("SELECT * FROM messages WHERE contact_id=? AND telegram_id=? AND role='user'", (chat_id, message_id))
        if not rows:
            raise ValueError('Входящее сообщение не найдено')
        message = rows[0]
        user_id = message['sender_id']
        if (type(user_id) is not int or user_id <= 0 or message['forwarded']
                or user_id in {self.store.settings()['owner_id'], self.telegram.account_id, SECURITY_PEER}
                or authorized(self.store, user_id, chat_id if self.store.contact(chat_id)['kind'] == 'group' else 0)
                or self.store.contact(chat_id)['kind'] == 'private' and chat_id != user_id):
            raise ValueError('Владельца, служебные аккаунты и авторов пересылок блокировать нельзя')
        if not self.store.context_record_active(message) or time.time() - message['created'] > 300:
            raise ValueError('Нужна свежая реплика из текущего диалога')
        latest = self.store.rows("SELECT MAX(id) AS id FROM messages WHERE contact_id=? AND sender_id=? AND role='user'", (chat_id, user_id))[0]['id']
        if latest != message['id']:
            raise ValueError('Собеседник уже написал новое сообщение; оцени его заново')
        return user_id

    async def block(self, chat_id, message_id, reason):
        if reason not in REASONS:
            raise ValueError('Укажи причину блокировки из списка')
        async with self.lock:
            user_id = self.request(chat_id, message_id)
            rows = self.store.rows('SELECT * FROM contact_blocks WHERE user_id=?', (user_id,))
            if rows and (rows[0]['status'] in {'blocked', 'pending', 'uncertain', 'unblocking'}
                         or (rows[0]['source_chat_id'], rows[0]['source_message_id']) == (chat_id, message_id)):
                return {'blocked': rows[0]['status'] == 'blocked', 'status': rows[0]['status'], 'stop_reply': True}
            warnings = self.store.rows('SELECT * FROM contact_warnings WHERE user_id=?', (user_id,))
            source = self.store.rows('SELECT created FROM messages WHERE contact_id=? AND telegram_id=?', (chat_id, message_id))[0]
            if (not warnings or time.time() - warnings[0]['warned_at'] > 86400
                    or source['created'] <= warnings[0]['warned_at']):
                if warnings and (warnings[0]['source_chat_id'], warnings[0]['source_message_id']) == (chat_id, message_id):
                    return {'blocked': False, 'status': 'warned', 'stop_reply': True}
                self.runtime.reply_states[chat_id]['pending_warning'] = {
                    'user_id': user_id, 'source_message_id': message_id, 'reason': reason}
                return {'blocked': False, 'warning_required': True, 'text': WARNING}
            if not self.telegram.ready():
                raise ValueError('Telegram не подключён')
            peer = await self.telegram.client.get_input_entity(user_id)
            if not isinstance(peer, types.InputPeerUser) or peer.user_id != user_id:
                raise ValueError('Блокировать можно только текущего пользователя Telegram')
            self.request(chat_id, message_id)
            now = time.time()
            self.store.execute("INSERT INTO contact_blocks VALUES (?,?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET status=excluded.status,reason=excluded.reason,source_chat_id=excluded.source_chat_id,source_message_id=excluded.source_message_id,created=excluded.created,updated=excluded.updated",
                               (user_id, 'pending', reason, chat_id, message_id, now, now))
            try:
                async with asyncio.timeout(20):
                    result = await self.telegram.client(functions.contacts.BlockRequest(peer))
            except errors.RPCError:
                self.store.execute("UPDATE contact_blocks SET status='failed',updated=? WHERE user_id=?", (time.time(), user_id))
                raise ValueError('Telegram отказал в блокировке; действие не выполнено') from None
            except BaseException as error:
                self.store.execute("UPDATE contact_blocks SET status='uncertain',updated=? WHERE user_id=?", (time.time(), user_id))
                self.store.event(f'Личная блокировка {user_id}: результат Telegram не подтверждён, ответы приостановлены', 'error')
                if not isinstance(error, Exception):
                    raise
                return {'blocked': False, 'status': 'uncertain', 'stop_reply': True}
            status = 'blocked' if result is True else 'failed'
            self.store.execute('UPDATE contact_blocks SET status=?,updated=? WHERE user_id=?', (status, time.time(), user_id))
            if status == 'failed':
                raise ValueError('Telegram не выполнил блокировку')
            self.store.execute("UPDATE tasks SET status='cancelled',error='Собеседник заблокирован' WHERE status='pending' AND (contact_id=? OR context_sender_id=?)", (user_id, user_id))
            self.store.event(f'Дарья заблокировала {user_id}: {REASONS[reason]}')
            return {'blocked': True, 'status': 'blocked', 'stop_reply': True}

    def confirm_warning(self, chat_id, sent_message_id, text):
        state = self.runtime.reply_states.get(chat_id) or {}
        warning = state.get('pending_warning')
        if not warning or text != WARNING:
            return
        self.store.execute('INSERT INTO contact_warnings VALUES (?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET source_chat_id=excluded.source_chat_id,source_message_id=excluded.source_message_id,sent_message_id=excluded.sent_message_id,warned_at=excluded.warned_at,reason=excluded.reason',
            (warning['user_id'], chat_id, warning['source_message_id'], sent_message_id, time.time(), warning['reason']))
        state.pop('pending_warning', None)
        self.store.event(f"Дарья предупредила {warning['user_id']}: {REASONS[warning['reason']]}")

    def listing(self):
        return [{'user_id': row['user_id'], 'status': row['status'],
                 'reason': REASONS.get(row['reason'], row['reason']), 'updated': row['updated']}
                for row in self.store.rows("SELECT * FROM contact_blocks WHERE status IN ('blocked','pending','uncertain','unblocking') ORDER BY updated DESC")]

    async def unblock(self, recipient, guard):
        async with self.lock:
            guard()
            contact = await self.telegram.resolve(recipient)
            user_id = contact['id']
            if user_id <= 0 or contact.get('kind', 'private') != 'private':
                raise ValueError('Укажи пользователя, а не группу')
            peer = await self.telegram.client.get_input_entity(user_id)
            if not isinstance(peer, types.InputPeerUser) or peer.user_id != user_id:
                raise ValueError('Нужен пользователь Telegram')
            guard()
            rows = self.store.rows('SELECT * FROM contact_blocks WHERE user_id=?', (user_id,))
            if rows:
                self.store.execute("UPDATE contact_blocks SET status='unblocking',updated=? WHERE user_id=?", (time.time(), user_id))
            try:
                async with asyncio.timeout(20):
                    confirmed = await self.telegram.client(functions.contacts.UnblockRequest(peer))
                if confirmed is not True:
                    # An already unblocked user can produce BoolFalse. Verify the
                    # current state without repeating the mutation.
                    async with asyncio.timeout(20):
                        full = await self.telegram.client(functions.users.GetFullUserRequest(
                            types.InputUser(peer.user_id, peer.access_hash)))
                    if getattr(full.full_user, 'blocked', None) is not False:
                        raise ValueError('Telegram не подтвердил разблокировку: пользователь всё ещё заблокирован')
            except BaseException:
                if rows:
                    self.store.execute("UPDATE contact_blocks SET status='uncertain',updated=? WHERE user_id=?", (time.time(), user_id))
                raise
            if rows:
                self.store.execute("UPDATE contact_blocks SET status='unblocked',updated=? WHERE user_id=?", (time.time(), user_id))
            self.store.execute('DELETE FROM contact_warnings WHERE user_id=?', (user_id,))
            self.store.event(f'Владелец разблокировал {user_id}')
            return {'unblocked': True, 'user_id': user_id}
