import asyncio
import hashlib
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from telethon import errors, functions, types

from app.session_guard import SessionGuard, GuardPolicyError
from app.store import Store
from app.telegram import Telegram


def authorization(value, current=False):
    return SimpleNamespace(hash=value, current=current, device_model='Device',
                           ip='secret-ip', country='secret-location')


class Client:
    def __init__(self):
        self.session = SimpleNamespace(auth_key=SimpleNamespace(key=b'test-server-key'))
        self.rows = [authorization(0, True), authorization(11), authorization(22)]
        self.requests = []
        self.get_error = None
        self.reset_error = None
        self.remove = True
        self.result = True
        self.disconnect = AsyncMock()

    async def __call__(self, request):
        self.requests.append(request)
        if isinstance(request, functions.account.GetAuthorizationsRequest):
            if self.get_error:
                raise self.get_error
            return SimpleNamespace(authorizations=list(self.rows))
        if isinstance(request, functions.account.ResetAuthorizationRequest):
            if self.reset_error:
                raise self.reset_error
            if self.remove:
                self.rows = [row for row in self.rows if row.hash != request.hash]
            return self.result
        raise AssertionError(type(request).__name__)

    def resets(self):
        return [request.hash for request in self.requests
                if isinstance(request, functions.account.ResetAuthorizationRequest)]


class SessionGuardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name))
        self.client = Client()
        self.guard = SessionGuard(self.store)
        self.policy = dict(version=1, account_id=1000000002,
            server_key_sha256=hashlib.sha256(b'test-server-key').hexdigest(),
            approved_at=time.time(), trusted=[dict(hash=str(value), current=current)
                for value, current in [(0, True), (11, False), (22, False)]])
        self.write_policy()

    def write_policy(self):
        self.guard.path.write_text(json.dumps(self.policy))

    async def setup_guard(self):
        self.guard.client, self.guard.account_id = self.client, 1000000002
        self.guard.load()

    async def asyncTearDown(self):
        await self.guard.stop()
        self.store.close()
        self.tmp.cleanup()

    async def test_targeted_revocation_preserves_all_three_trusted_sessions(self):
        await self.setup_guard()
        self.client.rows += [authorization(33), authorization(-44)]
        await self.guard.scan()
        self.assertEqual(self.client.resets(), [33, -44])
        self.assertEqual([row.hash for row in self.client.rows], [0, 11, 22])
        self.assertEqual(self.guard.status()['revoked_count'], 2)
        self.assertEqual(self.guard.status()['state'], 'protected')
        self.assertNotIn('secret-location', json.dumps(self.guard.status()))
        self.assertNotIn('secret-ip', json.dumps(self.store.rows('SELECT text FROM events')))

    async def test_restart_uses_original_baseline_and_never_trusts_new_login(self):
        original = self.guard.path.read_bytes()
        self.client.rows.append(authorization(33))
        await self.guard.start(self.client, 1000000002)
        await self.guard.stop()
        self.client.rows.append(authorization(44))
        await self.guard.start(self.client, 1000000002)
        self.guard.next_scan = 0
        await self.guard.scan()
        self.assertEqual(self.client.resets(), [33, 44])
        self.assertEqual(self.guard.path.read_bytes(), original)

    async def test_current_nonzero_authorization_is_never_reset(self):
        await self.setup_guard()
        self.client.rows[0] = authorization(999, True)
        self.client.rows.append(authorization(33))
        await self.guard.scan()
        self.assertEqual(self.client.resets(), [33])
        self.assertTrue(any(row.current and row.hash == 999 for row in self.client.rows))

    async def test_missing_current_session_does_not_trigger_any_reset(self):
        await self.setup_guard()
        self.client.rows = [authorization(33)]
        await self.guard.scan()
        self.assertEqual(self.client.resets(), [])
        self.assertEqual(self.guard.status()['state'], 'retrying')

    async def test_duplicate_current_or_foreign_zero_inventory_is_rejected(self):
        await self.setup_guard()
        for extra in [authorization(999, True), authorization(0)]:
            self.client.rows = [authorization(0, True), authorization(33), extra]
            self.guard.next_scan = 0
            await self.guard.scan()
            self.assertEqual(self.client.resets(), [])

    async def test_missing_policy_after_installation_blocks_without_rebaselining(self):
        await self.setup_guard()
        self.guard.path.unlink()
        with self.assertRaises(GuardPolicyError):
            await self.guard.start(self.client, 1000000002)
        self.assertFalse(self.guard.path.exists())
        self.assertFalse(self.guard.status()['running'])
        self.assertEqual(self.client.requests, [])

    async def test_invalid_policy_fails_before_contacting_telegram(self):
        self.guard.path.write_text('{broken')
        with self.assertRaises(GuardPolicyError):
            await self.guard.start(self.client, 1000000002)
        self.assertEqual(self.client.requests, [])
        self.assertTrue(self.guard.marker.exists())

    async def test_policy_without_current_or_with_foreign_zero_is_rejected(self):
        for rows in [[dict(hash='11', current=False)],
                     [dict(hash='22', current=True), dict(hash='0', current=False)]]:
            self.policy['trusted'] = rows
            self.write_policy()
            with self.assertRaises(GuardPolicyError):
                await self.guard.start(self.client, 1000000002)
        self.assertEqual(self.client.requests, [])

    async def test_changed_account_or_server_key_fails_before_revoking(self):
        with self.assertRaises(GuardPolicyError):
            await self.guard.start(self.client, 123)
        self.client.session.auth_key.key = b'different-secret-key'
        with self.assertRaises(GuardPolicyError):
            await self.guard.start(self.client, 1000000002)
        self.assertEqual(self.client.requests, [])
        audit = json.dumps(self.store.rows('SELECT text FROM events'))
        self.assertNotIn('different-secret-key', audit)

    async def test_changed_identity_during_worker_disconnects_safely(self):
        with patch('app.session_guard.MIN_SCAN_INTERVAL', 0), patch('app.session_guard.INTERVAL', .02):
            await self.guard.start(self.client, 1000000002)
            self.client.session.auth_key.key = b'changed'
            self.client.rows.append(authorization(33))
            self.guard.wake.set()
            await asyncio.wait_for(self.guard.task, .3)
        self.client.disconnect.assert_awaited_once()
        self.assertEqual(self.client.resets(), [])
        self.assertEqual(self.guard.status()['state'], 'blocked')

    async def test_new_authorization_update_triggers_scan_without_waiting_for_periodic_check(self):
        with patch('app.session_guard.MIN_SCAN_INTERVAL', 0):
            await self.guard.start(self.client, 1000000002)
            self.client.rows.append(authorization(33))
            await self.guard.on_authorization(types.UpdateNewAuthorization(33))
            await self.wait_for_reset(33)
        self.assertEqual(self.guard.status()['unknown_count'], 0)

    async def wait_for_reset(self, value):
        async def wait():
            while value not in self.client.resets():
                await asyncio.sleep(.005)
        await asyncio.wait_for(wait(), .4)

    async def test_periodic_fallback_runs_even_when_chatbot_is_disabled(self):
        self.assertFalse(self.store.settings()['enabled'])
        with patch('app.session_guard.INTERVAL', .02), patch('app.session_guard.MIN_SCAN_INTERVAL', 0):
            await self.guard.start(self.client, 1000000002)
            self.client.rows.append(authorization(33))
            await self.wait_for_reset(33)

    async def test_flood_wait_from_scan_is_respected_even_for_raw_updates(self):
        self.client.get_error = errors.FloodWaitError(None, capture=30)
        await self.guard.start(self.client, 1000000002)
        count = len(self.client.requests)
        for _ in range(4):
            await self.guard.on_authorization(types.UpdateNewAuthorization(33))
            await self.guard.scan()
        await asyncio.sleep(.02)
        self.assertEqual(len(self.client.requests), count)
        self.assertEqual(self.guard.status()['state'], 'cooldown')
        self.assertGreater(self.guard.status()['retry_at'], time.time() + 28)

    async def test_fresh_session_restriction_is_reported_and_worker_keeps_running(self):
        self.client.rows.append(authorization(33))
        self.client.reset_error = errors.FreshResetAuthorisationForbiddenError(None)
        await self.guard.start(self.client, 1000000002)
        self.assertEqual(self.guard.status()['state'], 'restricted')
        self.assertEqual(self.guard.status()['revoked_count'], 0)
        self.assertEqual(self.guard.status()['unknown_count'], 1)
        self.assertTrue(self.guard.status()['running'])
        self.assertEqual([row.hash for row in self.client.rows], [0, 11, 22, 33])

    async def test_false_reset_result_is_rechecked_and_never_claimed_success_if_session_remains(self):
        await self.setup_guard()
        self.client.rows.append(authorization(33))
        self.client.result, self.client.remove = False, False
        await self.guard.scan()
        self.assertEqual(self.guard.status()['revoked_count'], 0)
        self.assertEqual(self.guard.status()['unknown_count'], 1)
        self.assertEqual(self.guard.status()['state'], 'retrying')
        self.assertEqual(len(self.client.requests), 3)

    async def test_confirmation_of_removed_session_matters_more_than_rpc_boolean(self):
        await self.setup_guard()
        self.client.rows.append(authorization(33))
        self.client.result = False
        await self.guard.scan()
        self.assertEqual(self.guard.status()['revoked_count'], 1)
        self.assertEqual(self.guard.status()['state'], 'protected')

    async def test_network_failure_does_not_kill_worker_or_log_error_payload(self):
        self.client.get_error = OSError('secret-location test-server-key')
        await self.guard.start(self.client, 1000000002)
        self.assertEqual(self.guard.status()['state'], 'retrying')
        self.assertTrue(self.guard.status()['running'])
        self.client.get_error = None
        self.guard.next_scan = 0
        await self.guard.scan()
        self.assertEqual(self.guard.status()['state'], 'protected')
        audit = json.dumps(self.store.rows('SELECT text FROM events'))
        self.assertNotIn('secret-location', audit)
        self.assertNotIn('test-server-key', audit)

    async def test_drop_cancels_worker_and_preserves_policy(self):
        telegram = Telegram(self.store, None)
        telegram.client = self.client
        telegram.state = 'connected'
        await telegram.session_guard.start(self.client, 1000000002)
        task = telegram.session_guard.task
        await telegram.drop()
        self.assertTrue(task.cancelled())
        self.assertFalse(telegram.session_guard.status()['running'])
        self.assertTrue(telegram.session_guard.path.exists())
        self.client.disconnect.assert_awaited_once()

    async def test_telegram_rejects_new_server_session_instead_of_finishing_login(self):
        telegram = Telegram(self.store, None)
        telegram.client = self.client
        self.client.get_me = AsyncMock(return_value=SimpleNamespace(id=123, first_name='X', last_name=''))
        with self.assertRaises(GuardPolicyError):
            await telegram.authorized()
        self.assertEqual(telegram.state, 'disconnected')
        self.assertIsNone(telegram.client)
        self.assertEqual(self.client.resets(), [])

    async def test_official_and_real_forwarded_security_messages_never_reach_storage_or_ai(self):
        telegram = Telegram(self.store, Mock())
        telegram.schedule_reply_typing = Mock()
        telegram.begin_reply_presence = AsyncMock()
        for sender, forward in [(777000, None), (123, SimpleNamespace(from_id=types.PeerUser(777000)))]:
            event = SimpleNamespace(sender_id=sender, message=SimpleNamespace(fwd_from=forward),
                                    raw_text='Login code 12345', get_sender=AsyncMock())
            await telegram.on_message(event)
            event.get_sender.assert_not_awaited()
        telegram.receive.assert_not_called()
        telegram.schedule_reply_typing.assert_not_called()
        telegram.begin_reply_presence.assert_not_awaited()
        self.assertEqual(self.store.rows('SELECT * FROM messages'), [])
        self.assertEqual(self.store.rows('SELECT * FROM contacts'), [])
        self.assertTrue(telegram.session_guard.wake.is_set())

    async def test_chat_messages_cannot_run_before_guard_validates_connection(self):
        telegram = Telegram(self.store, Mock())
        event = SimpleNamespace(sender_id=123, message=SimpleNamespace(fwd_from=None), get_sender=AsyncMock())
        await telegram.on_message(event)
        event.get_sender.assert_not_awaited()
        telegram.receive.assert_not_called()

