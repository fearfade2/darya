"""Revoke newly created Telegram authorizations against an owner-approved baseline."""
from __future__ import annotations

import asyncio
import hashlib
import json
import time

from telethon import errors, functions, types

INTERVAL = 15
MIN_SCAN_INTERVAL = 1
RPC_TIMEOUT = 20
SECURITY_PEER = 777000


def security_message(event):
    forward = getattr(getattr(event, 'message', None), 'fwd_from', None)
    origin = getattr(forward, 'from_id', None)
    return (getattr(event, 'sender_id', None) == SECURITY_PEER or
            isinstance(origin, types.PeerUser) and origin.user_id == SECURITY_PEER)


class GuardPolicyError(ValueError):
    pass


class SessionGuard:
    def __init__(self, store):
        self.store = store
        self.path = store.directory / 'telegram-session-trust.json'
        self.marker = store.directory / 'telegram-session-guard.required'
        self.client = None
        self.account_id = None
        self.policy = None
        self.task = None
        self.wake = asyncio.Event()
        self.lock = asyncio.Lock()
        self.next_scan = 0
        self.identity_validated = False
        self.info = dict(state='unconfigured', trusted_count=0, active_count=0,
                         unknown_count=0, revoked_count=0, last_check_at=0,
                         retry_at=0, error='')

    def status(self):
        return self.info | {'running': bool(self.task and not self.task.done())}

    def blocked(self, message):
        self.info.update(state='blocked', error=message)
        self.store.event('Защита сессий Telegram: ' + message, 'error')
        raise GuardPolicyError(message)

    def load(self):
        if not self.path.exists():
            if self.marker.exists():
                self.blocked('Список доверенных сессий отсутствует. Нужно подтверждение владельца')
            self.policy = None
            return
        # A later missing policy must never silently switch protection off.
        if not self.marker.exists():
            with self.marker.open('x') as stream:
                self.marker.chmod(0o600)
                stream.write('1\n')
        try:
            policy = json.loads(self.path.read_text())
            assert policy['version'] == 1
            assert type(policy['account_id']) is int and policy['account_id'] > 0
            digest = policy['server_key_sha256']
            assert isinstance(digest, str) and len(digest) == 64
            int(digest, 16)
            rows = policy['trusted']
            assert isinstance(rows, list) and rows
            assert all(type(row['current']) is bool and isinstance(row['hash'], str) for row in rows)
            hashes = [int(row['hash']) for row in rows]
            assert len(set(hashes)) == len(rows)
            assert all(-(2**63) <= value < 2**63 for value in hashes)
            assert sum(row['current'] for row in rows) == 1
            assert all(row['current'] or int(row['hash']) != 0 for row in rows)
        except (OSError, ValueError, KeyError, TypeError, AssertionError):
            self.blocked('Список доверенных сессий повреждён. Нужно подтверждение владельца')
        self.policy = policy
        self.info['trusted_count'] = len(rows)

    def validate_identity(self):
        key = getattr(getattr(self.client.session, 'auth_key', None), 'key', None)
        if (self.account_id != self.policy['account_id'] or not isinstance(key, bytes) or
                hashlib.sha256(key).hexdigest() != self.policy['server_key_sha256']):
            self.blocked('Аккаунт или серверная сессия изменились. Нужно подтверждение владельца')

    async def start(self, client, account_id):
        if self.task and not self.task.done():
            return
        self.client, self.account_id = client, account_id
        self.load()
        if self.policy is None:
            return
        self.validate_identity()
        self.identity_validated = True
        await self.scan()
        self.task = asyncio.create_task(self.run(), name='telegram-session-guard')

    async def stop(self):
        task, self.task = self.task, None
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.client = None
        self.identity_validated = False
        if self.info['state'] not in {'blocked', 'unconfigured'}:
            self.info['state'] = 'stopped'

    async def on_authorization(self, update):
        # Never adopt update.hash or include the reported location in memory/logs.
        self.wake.set()

    async def call(self, request):
        return await asyncio.wait_for(self.client(request), RPC_TIMEOUT)

    async def scan(self):
        async with self.lock:
            if time.time() < self.next_scan:
                return
            self.validate_identity()
            try:
                result = await self.call(functions.account.GetAuthorizationsRequest())
                rows = result.authorizations
                if sum(bool(row.current) for row in rows) != 1 or any(not row.current and row.hash == 0 for row in rows):
                    raise ValueError('unsafe authorization inventory')
                trusted = {int(row['hash']) for row in self.policy['trusted'] if not row['current']}
                unknown = [row for row in rows if not row.current and row.hash not in trusted]
                self.info.update(active_count=len(rows), unknown_count=len(unknown),
                                 last_check_at=time.time())
                for row in unknown:
                    # Explicitly target only a foreign, nonzero, unapproved authorization.
                    if row.current or not row.hash or row.hash in trusted:
                        continue
                    self.validate_identity()
                    await self.call(functions.account.ResetAuthorizationRequest(hash=row.hash))
                if unknown:
                    after = await self.call(functions.account.GetAuthorizationsRequest())
                    remaining = {row.hash for row in after.authorizations}
                    revoked = sum(row.hash not in remaining for row in unknown)
                    self.info['revoked_count'] += revoked
                    self.info['active_count'] = len(after.authorizations)
                    self.info['unknown_count'] = sum(not row.current and row.hash not in trusted
                                                     for row in after.authorizations)
                    if revoked:
                        self.store.event(f'Защита Telegram завершила посторонние сессии: {revoked}')
                    if self.info['unknown_count']:
                        raise ValueError('authorization still present')
                self.info.update(state='protected', error='', retry_at=0)
                self.next_scan = time.time() + MIN_SCAN_INTERVAL
            except GuardPolicyError:
                raise
            except errors.FloodWaitError as error:
                self.retry('Telegram ограничил частоту проверки сессий', max(1, error.seconds), 'cooldown')
            except errors.FreshResetAuthorisationForbiddenError:
                self.retry('Telegram запрещает завершение чужих сессий в первые 24 часа после входа сервера', 60, 'restricted')
            except Exception as error:
                # RPC errors can contain account metadata; retain only the class name.
                self.retry('Проверка сессий не завершена: ' + type(error).__name__, INTERVAL, 'retrying')

    def retry(self, message, seconds, state):
        if message != self.info['error']:
            self.store.event('Защита сессий Telegram: ' + message, 'error')
        self.next_scan = time.time() + seconds
        self.info.update(state=state, error=message, retry_at=self.next_scan)

    async def run(self):
        while True:
            timeout = max(0.05, self.next_scan - time.time()) if self.next_scan > time.time() + INTERVAL else INTERVAL
            try:
                await asyncio.wait_for(self.wake.wait(), timeout)
            except asyncio.TimeoutError:
                pass
            self.wake.clear()
            remaining = self.next_scan - time.time()
            if remaining > 0:
                await asyncio.sleep(remaining)
            try:
                await self.scan()
            except GuardPolicyError:
                await self.client.disconnect()
                return
