import asyncio
import datetime
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from telethon import errors, types

from app.store import Store
from app.telegram import Telegram


class TelegramQRTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name))
        self.store.update_secrets({'telegram_api_id':100001,'telegram_api_hash':'a'*32})
        self.telegram = Telegram(self.store,None)
        self.client = AsyncMock()
        self.client.is_user_authorized.return_value = False
        self.client.get_me.return_value = SimpleNamespace(id=10,first_name='Дарья',last_name='')
        self.client.send_code_request.return_value = types.auth.SentCode(types.auth.SentCodeTypeApp(5),'private-code-hash')
        self.scan = asyncio.Event()
        self.qr = SimpleNamespace(url='tg://login?token=FAKE_TOKEN_ONLY',expires=datetime.datetime.now(datetime.timezone.utc)+datetime.timedelta(seconds=30),wait=AsyncMock(side_effect=self.scan.wait))
        self.client.qr_login.return_value = self.qr
        async def connect():
            self.telegram.client = self.client
        self.telegram.connect = AsyncMock(side_effect=connect)

    async def asyncTearDown(self):
        await self.telegram.drop()
        self.store.close()
        self.tmp.cleanup()

    async def test_qr_is_rendered_locally_and_token_is_absent_from_metadata_and_logs(self):
        result = await self.telegram.request_qr(100001,'')
        self.assertEqual(result['state'],'qr')
        self.assertIn('<svg',self.telegram.qr_svg)
        self.assertTrue(self.telegram.qr_waiter and not self.telegram.qr_waiter.done())
        output=json.dumps(result)+json.dumps(self.store.rows('SELECT text FROM events'))
        for secret in ['FAKE_TOKEN_ONLY','a'*32]:
            self.assertNotIn(secret,output)

    async def test_confirmed_qr_finishes_login_and_removes_scannable_token(self):
        await self.telegram.request_qr(100001,'')
        task=self.telegram.qr_waiter
        self.scan.set()
        await task
        self.assertEqual(self.telegram.state,'connected')
        self.assertEqual(self.telegram.account_id,10)
        self.assertEqual(self.telegram.qr_svg,'')
        self.assertEqual(self.telegram.qr_info(),{})

    async def test_qr_confirmation_can_continue_with_two_factor_password(self):
        self.qr.wait.side_effect=errors.SessionPasswordNeededError(None)
        await self.telegram.request_qr(100001,'')
        await self.telegram.qr_waiter
        self.assertEqual(self.telegram.state,'password')
        self.assertEqual(self.telegram.qr_svg,'')
        self.assertEqual(self.telegram.qr_info(),{})

    async def test_expired_qr_can_no_longer_be_displayed(self):
        self.qr.wait.side_effect=asyncio.TimeoutError
        await self.telegram.request_qr(100001,'')
        await self.telegram.qr_waiter
        self.assertEqual(self.telegram.qr_svg,'')
        self.assertIn('истёк',self.telegram.qr_info()['error'])

    async def test_switching_to_code_cancels_the_old_qr_waiter(self):
        await self.telegram.request_qr(100001,'')
        task=self.telegram.qr_waiter
        await self.telegram.request_code(100001,'','+70000001234')
        self.assertTrue(task.cancelled())
        self.assertEqual(self.telegram.state,'code')
        self.assertEqual(self.telegram.qr_svg,'')
        self.scan.set()
        await asyncio.sleep(0)
        self.assertEqual(self.telegram.state,'code')

    async def test_cancel_login_stops_local_waiter_without_deleting_credentials(self):
        await self.telegram.request_qr(100001,'')
        task=self.telegram.qr_waiter
        await self.telegram.cancel_login()
        self.assertTrue(task.cancelled())
        self.assertEqual(self.telegram.state,'disconnected')
        self.assertEqual(self.telegram.qr_svg,'')
        self.assertEqual(self.store.secrets()['telegram_api_id'],100001)

    async def test_recreated_qr_has_a_new_image_version_even_with_same_expiration(self):
        first=await self.telegram.request_qr(100001,'')
        second=await self.telegram.request_qr(100001,'')
        self.assertGreater(second['qr']['version'],first['qr']['version'])

    async def test_start_does_not_interrupt_waiting_for_a_scan(self):
        self.telegram.state='qr'
        await self.telegram.start()
        self.telegram.connect.assert_not_awaited()
