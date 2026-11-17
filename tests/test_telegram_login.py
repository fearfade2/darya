import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from telethon import errors, functions, types

from app.store import Store
from app.telegram import Telegram


class TelegramLoginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name))
        self.telegram = Telegram(self.store, None)
        self.telegram.phone = '+70000001234'
        self.telegram.client = AsyncMock()
        self.telegram.client.is_user_authorized.return_value = False
        self.telegram.connect = AsyncMock()
        self.telegram.drop = AsyncMock()

    async def asyncTearDown(self):
        self.store.close()
        self.tmp.cleanup()

    async def test_request_reports_delivery_without_exposing_login_secrets(self):
        self.telegram.client.send_code_request.return_value = types.auth.SentCode(types.auth.SentCodeTypeApp(5), 'private-login-hash', timeout=90)
        result = await self.telegram.request_code(100001, 'a'*32, self.telegram.phone)
        self.assertEqual(result['login']['channel'], 'telegram')
        self.assertEqual(result['login']['phone_hint'], '+••••1234')
        self.assertEqual(result['login']['code_length'], 5)
        self.assertGreater(result['login']['resend_at'], time.time()+85)
        audit = json.dumps(self.store.rows('SELECT text FROM events'))
        for value in [self.telegram.phone, 'private-login-hash', 'a'*32]:
            self.assertNotIn(value, json.dumps(result)+audit)
        self.assertIn('SentCodeTypeApp', audit)

    async def test_resend_uses_current_request_hash_and_updates_delivery(self):
        await self.telegram.accept_code(types.auth.SentCode(types.auth.SentCodeTypeApp(5), 'first-hash'))
        self.telegram.code_delivery['resend_at'] = 0
        self.telegram.client.return_value = types.auth.SentCode(types.auth.SentCodeTypeSms(5), 'second-hash', timeout=60)
        result = await self.telegram.resend_code()
        request = self.telegram.client.call_args.args[0]
        self.assertIsInstance(request, functions.auth.ResendCodeRequest)
        self.assertEqual(request.phone_code_hash, 'first-hash')
        self.assertEqual(request.phone_number, '70000001234')
        self.assertEqual(self.telegram.phone_hash, 'second-hash')
        self.assertEqual(result['login']['channel'], 'sms')
        self.telegram.client.send_code_request.assert_not_awaited()
        self.telegram.drop.assert_not_awaited()

    async def test_resend_cooldown_does_not_contact_telegram(self):
        await self.telegram.accept_code(types.auth.SentCode(types.auth.SentCodeTypeApp(5), 'hash', timeout=120))
        with self.assertRaisesRegex(ValueError, 'через'):
            await self.telegram.resend_code()
        self.telegram.client.assert_not_awaited()
        self.telegram.connect.assert_not_awaited()

    async def test_email_delivery_is_not_claimed_to_be_a_telegram_message(self):
        sent = types.auth.SentCode(types.auth.SentCodeTypeEmailCode('full@example.com', 6), 'hash')
        result = await self.telegram.accept_code(sent)
        self.assertEqual(result['login']['channel'], 'email')
        self.assertEqual(result['login']['email_hint'], '***@example.com')
        self.assertNotIn('full@example.com', json.dumps(result))

    async def test_missing_hash_cannot_leave_a_stale_code_form(self):
        self.telegram.state = 'code'
        self.telegram.phone_hash = 'old-hash'
        with self.assertRaisesRegex(ValueError, 'данные'):
            await self.telegram.accept_code(types.auth.SentCode(types.auth.SentCodeTypeApp(5), ''))
        self.assertEqual(self.telegram.state, 'disconnected')
        self.assertEqual(self.telegram.phone_hash, '')
        self.assertEqual(self.telegram.login_info(), {})

    async def test_unavailable_resend_keeps_existing_code_and_records_reason(self):
        await self.telegram.accept_code(types.auth.SentCode(types.auth.SentCodeTypeApp(5), 'hash'))
        self.telegram.code_delivery['resend_at'] = 0
        self.telegram.client.side_effect = errors.SendCodeUnavailableError(None)
        with self.assertRaisesRegex(ValueError, 'не разрешает'):
            await self.telegram.resend_code()
        self.assertEqual(self.telegram.phone_hash, 'hash')
        self.assertEqual(self.telegram.state, 'code')
        self.assertTrue(any('не разрешает' in row['text'] for row in self.store.rows('SELECT text FROM events')))

    async def test_failed_first_request_is_recorded_without_secrets(self):
        self.telegram.client.send_code_request.side_effect = errors.PhoneNumberBannedError(None)
        with self.assertRaisesRegex(ValueError, 'заблокировал'):
            await self.telegram.request_code(100001, 'a'*32, self.telegram.phone)
        self.assertEqual(self.telegram.state, 'disconnected')
        self.assertTrue(any('заблокировал' in row['text'] for row in self.store.rows('SELECT text FROM events')))

    async def test_saved_api_hash_can_be_reused_for_its_api_id(self):
        self.store.update_secrets({'telegram_api_id':100001, 'telegram_api_hash':'b'*32})
        self.telegram.client.send_code_request.return_value = types.auth.SentCode(types.auth.SentCodeTypeApp(5), 'hash')
        result = await self.telegram.request_code(100001, '', self.telegram.phone)
        self.assertEqual(result['state'], 'code')
        self.assertEqual(self.store.secrets()['telegram_api_hash'], 'b'*32)
        self.assertNotIn('b'*32, json.dumps(result))

    async def test_saved_api_hash_cannot_be_reused_for_another_api_id(self):
        self.store.update_secrets({'telegram_api_id':100001, 'telegram_api_hash':'b'*32})
        with self.assertRaisesRegex(ValueError, 'API Hash'):
            await self.telegram.request_code(100002, '', self.telegram.phone)
        self.telegram.connect.assert_not_awaited()
        self.telegram.drop.assert_not_awaited()
        self.assertEqual(self.store.secrets()['telegram_api_id'], 100001)

    async def test_authorized_session_does_not_request_another_code(self):
        self.telegram.client.is_user_authorized.return_value=True
        self.telegram.client.get_me.return_value=SimpleNamespace(id=10,first_name='Дарья',last_name='')
        result=await self.telegram.request_code(100001,'a'*32,self.telegram.phone)
        self.assertEqual(result['state'],'connected')
        self.telegram.client.send_code_request.assert_not_awaited()
