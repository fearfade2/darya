import asyncio
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock

import httpx

from app.ai import Model, ModelUnavailable
from app.runtime import Runtime
from app.security import create_session
from app.store import Store


class ReplyPresenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name))
        self.store.upsert_contact(10,'Друг')
        self.store.upsert_contact(20,'Другой')
        self.store.upsert_contact(1000000001,'Владелец')
        self.store.update_settings({'owner_id':1000000001,'enabled':True,'online_during_reply':True,'natural_enabled':True,
            'read_delay_min':0.04,'read_delay_max':0.04,'reply_delay_min':0,'reply_delay_max':0,
            'burst_window':0,'min_interval':0,'reply_idle_seconds':0.04,'presence_enabled':False,
            'reply_online_immediate':False})
        self.rt = Runtime(self.store)
        self.rt.telegram.ready = lambda: True
        self.rt.telegram.client = AsyncMock()
        self.rt.telegram.read = AsyncMock()
        self.rt.telegram.send = AsyncMock(return_value=100)
        self.rt.model.run = AsyncMock(return_value='Ответ')

    async def asyncTearDown(self):
        await self.rt.stop()
        self.store.close()
        self.tmp.cleanup()

    async def finish(self):
        await asyncio.gather(*list(self.rt.work),return_exceptions=True)

    async def test_first_online_and_read_honor_pause_once_and_offline_waits_after_send(self):
        start = time.monotonic()
        self.rt.receive(10,'Привет',11,sender_id=10)
        await asyncio.sleep(0.01)
        await self.rt.telegram.periodic_presence(True)
        self.assertFalse(self.rt.telegram.online)
        self.rt.telegram.read.assert_not_awaited()
        await self.finish()
        self.assertGreaterEqual(time.monotonic()-start,0.03)
        self.rt.telegram.read.assert_awaited_once_with(10,11)
        self.assertTrue(self.rt.telegram.online)
        await self.rt.telegram.periodic_presence(False)
        self.assertTrue(self.rt.telegram.online)
        await self.rt.telegram.presence_expiry
        self.assertFalse(self.rt.telegram.online)

    async def test_new_message_keeps_online_through_read_delay_and_restarts_idle_clock(self):
        self.rt.receive(10,'Первое',11,sender_id=10)
        await self.finish()
        old_expiry = self.rt.telegram.presence_expiry
        self.store.update_settings({'read_delay_min':0.08,'read_delay_max':0.08})
        self.rt.telegram.send.return_value = 101
        self.rt.receive(10,'Уточнение',12,sender_id=10)
        await asyncio.sleep(0.06)
        self.assertTrue(self.rt.telegram.online)
        self.assertTrue(old_expiry.cancelled())
        self.assertEqual(self.rt.telegram.read.await_count,1)
        await self.finish()
        self.assertTrue(self.rt.telegram.online)
        statuses = [call.args[0].offline for call in self.rt.telegram.client.call_args_list]
        self.assertNotIn(True,statuses)
        await self.rt.telegram.presence_expiry
        self.assertFalse(self.rt.telegram.online)

    async def test_immediate_online_keeps_read_delay_and_post_reply_grace(self):
        self.store.update_settings({'reply_online_immediate':True})
        self.rt.receive(10,'Ку',11,sender_id=10)
        await asyncio.sleep(0.01)
        self.assertTrue(self.rt.telegram.online)
        self.rt.telegram.read.assert_not_awaited()
        self.rt.model.run.assert_not_awaited()
        await self.finish()
        self.rt.telegram.read.assert_awaited_once_with(10,11)
        self.assertTrue(self.rt.telegram.online)
        await self.rt.telegram.presence_expiry
        self.assertFalse(self.rt.telegram.online)

    async def test_new_conversation_reasserts_online_even_if_cached_status_is_stale(self):
        self.rt.telegram.online = True
        self.rt.telegram.last_presence_update = time.time()
        self.store.update_settings({'reply_online_immediate':True})
        self.rt.receive(10,'Ку',11,sender_id=10)
        await self.finish()
        self.assertFalse(self.rt.telegram.client.call_args_list[0].args[0].offline)

    async def test_wire_online_survives_server_expiry_and_grace_then_really_exits(self):
        from unittest.mock import patch
        tg = self.rt.telegram
        wire = {'online':False,'expiry':None}
        expiries = []
        async def expire():
            await asyncio.sleep(0.035)
            wire['online'] = False
        async def request(value):
            wire['online'] = not value.offline
            if wire['expiry']:
                wire['expiry'].cancel()
            if not value.offline:
                wire['expiry'] = asyncio.create_task(expire())
                expiries.append(wire['expiry'])
            return True
        tg.client = AsyncMock(side_effect=request)
        try:
            with patch('app.telegram.REPLY_PRESENCE_REFRESH_SECONDS',0.01):
                await tg.begin_reply_presence()
                for _ in range(5):
                    await asyncio.sleep(0.02)
                    self.assertTrue(wire['online'])
                await tg.end_reply_presence()
                await asyncio.sleep(0.02)
                self.assertTrue(wire['online'])
                await tg.presence_expiry
                self.assertFalse(wire['online'])
        finally:
            await tg.drop()
            for task in expiries:task.cancel()
            await asyncio.gather(*expiries,return_exceptions=True)

    async def test_status_notification_restores_active_online_but_does_not_end_read_pause(self):
        from types import SimpleNamespace
        from telethon import types
        from datetime import datetime,timezone
        tg = self.rt.telegram
        tg.account_id = 1000000002
        offline = SimpleNamespace(user_id=tg.account_id,status=types.UserStatusOffline(datetime.now(timezone.utc)))
        await tg.begin_reply_presence(activate=False)
        await tg.on_status(offline)
        tg.client.assert_not_awaited()
        await tg.presence(60)
        await tg.on_status(SimpleNamespace(user_id=10,status=offline.status))
        self.assertEqual(tg.client.await_count,1)
        await tg.on_status(offline)
        self.assertTrue(tg.online)
        self.assertEqual(tg.client.await_count,2)
        await tg.end_reply_presence()
        await tg.presence_expiry
        count = tg.client.await_count
        await tg.on_status(offline)
        self.assertEqual(tg.client.await_count,count)

    async def test_heartbeat_does_not_activate_reserved_presence_before_delayed_read(self):
        from unittest.mock import patch
        with patch('app.telegram.REPLY_PRESENCE_REFRESH_SECONDS',0.005):
            self.rt.receive(10,'Ку',11,sender_id=10)
            await asyncio.sleep(0.02)
            self.assertFalse(self.rt.telegram.online)
            self.rt.telegram.read.assert_not_awaited()
            await self.finish()
            self.assertTrue(self.rt.telegram.online)

    async def test_drop_stops_online_refresh_and_releases_presence(self):
        tg = self.rt.telegram
        await tg.begin_reply_presence()
        heartbeat = tg.presence_heartbeat
        await asyncio.sleep(0)
        await tg.drop()
        self.assertTrue(heartbeat.cancelled())
        self.assertIsNone(tg.presence_heartbeat)
        self.assertEqual(tg.reply_presence_count,0)

    async def test_delayed_status_recovery_does_not_reenter_online_after_idle_deadline(self):
        from types import SimpleNamespace
        from telethon import types
        from datetime import datetime,timezone
        tg = self.rt.telegram
        tg.account_id = 1000000002
        await tg.begin_reply_presence()
        await tg.end_reply_presence()
        await tg.presence_lock.acquire()
        recovery = asyncio.create_task(tg.on_status(SimpleNamespace(user_id=tg.account_id,
            status=types.UserStatusOffline(datetime.now(timezone.utc)))))
        try:
            await asyncio.sleep(0.05)
        finally:
            tg.presence_lock.release()
        await recovery
        await tg.presence_expiry
        self.assertFalse(tg.online)
        self.assertEqual(tg.client.await_count,1)

    async def test_photo_download_already_has_online_presence(self):
        from types import SimpleNamespace
        from telethon import types
        from unittest.mock import Mock
        from app.telegram import Telegram
        self.store.update_settings({'reply_online_immediate':True})
        receive = Mock()
        tg = Telegram(self.store,receive)
        tg.ready = lambda:True
        tg.client = AsyncMock()
        async def download(*args,**kwargs):
            self.assertTrue(tg.online)
            self.assertEqual(tg.reply_presence_count,1)
            await tg.periodic_presence(False)
            self.assertTrue(tg.online)
            raise OSError('Download failed')
        tg.client.download_media.side_effect = download
        event = SimpleNamespace(raw_text='Фото',is_private=True,is_group=False,id=11,
            message=SimpleNamespace(photo=True,document=None,file=SimpleNamespace(size=100),fwd_from=None),
            get_sender=AsyncMock(return_value=types.User(id=10,first_name='Друг')))
        try:
            await tg.on_message(event)
            receive.assert_called_once()
            self.assertEqual(tg.reply_presence_count,0)
            self.assertTrue(tg.online)
            await tg.presence_expiry
            self.assertFalse(tg.online)
        finally:
            await tg.drop()

    async def test_presence_diagnostics_require_auth_and_report_telegram_status(self):
        from types import SimpleNamespace
        from telethon import types
        from datetime import datetime,timezone
        from app.api import app
        app.state.store,app.state.runtime = self.store,self.rt
        self.rt.telegram.client.get_me.return_value = SimpleNamespace(status=types.UserStatusOnline(datetime.now(timezone.utc)))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8790') as client:
            self.assertEqual((await client.get('/api/telegram/presence')).status_code,401)
            client.cookies.set('alisa_session',create_session(self.store))
            response = await client.get('/api/telegram/presence')
            self.assertEqual(response.status_code,200)
            self.assertTrue(response.json()['server_online'])

    async def test_screenshot_delay_and_idle_commands_use_direct_settings(self):
        for phrase,seconds in [('пауза перед прочтением 0,5 секунд',0.5),
                              ('нет пауза перед прочтением 1 секунда',1),
                              ('пауза перед прочтением и захода в сеть 1,5 секунда',1.5),
                              ('пауза перед прочтением 4 секунды',4)]:
            plan = self.rt.control.parse([phrase])
            expected = {'read_delay_min':seconds,'read_delay_max':seconds}
            if 'и захода' in phrase:
                expected['reply_online_immediate'] = False
            self.assertEqual(plan,[{'name':'configure_darya','args':{'settings':expected}}])
        self.assertEqual(self.rt.control.parse(['входи в сеть сразу']),
            [{'name':'configure_darya','args':{'settings':{'online_during_reply':True,'reply_online_immediate':True}}}])
        self.assertEqual(self.rt.control.parse(['уходи из сети через 4 секунды']),
            [{'name':'configure_darya','args':{'settings':{'reply_idle_seconds':4}}}])

    async def test_transient_failure_retries_latest_reply_and_sends_once(self):
        self.store.update_settings({'read_delay_min':0,'read_delay_max':0})
        sent = asyncio.Event()
        async def send(*args,**kwargs):
            kwargs['before_dispatch']()
            sent.set()
            return 100
        self.rt.telegram.send = AsyncMock(side_effect=send)
        self.rt.model.run.side_effect = [ModelUnavailable('429',retry_after=1),'Готовый ответ']
        self.rt.receive(10,'Привет',11,sender_id=10)
        await asyncio.wait_for(sent.wait(),3)
        await self.finish()
        self.assertEqual(self.rt.model.run.await_count,2)
        self.rt.telegram.send.assert_awaited_once()
        self.assertEqual(self.store.rows('SELECT * FROM pending_replies'),[])
        self.assertTrue(self.rt.model.run.call_args.kwargs['require_text'])

    async def test_restored_answer_is_text_even_if_model_first_reacts(self):
        import json
        self.rt.model.completion = AsyncMock(side_effect=[{'tool_calls':[
            {'id':'1','function':{'name':'react','arguments':json.dumps({'emoji':'❤'})}},
            {'id':'2','function':{'name':'skip_message','arguments':'{}'}}]}, {'content':'Вижу твоё сообщение.'}])
        result = await Model.run(self.rt.model,10,AsyncMock(return_value={'reacted':True}),require_text=True)
        self.assertEqual(result,'Вижу твоё сообщение.')

    async def test_new_input_replaces_sleeping_retry_without_a_stale_send(self):
        self.store.update_settings({'read_delay_min':0,'read_delay_max':0})
        failed = asyncio.Event()
        count = 0
        async def model(*args,**kwargs):
            nonlocal count
            count += 1
            if count == 1:
                failed.set()
                raise ModelUnavailable('429',retry_after=60)
            return 'Ответ на уточнение'
        self.rt.model.run = AsyncMock(side_effect=model)
        self.rt.receive(10,'Первое',11,sender_id=10)
        await asyncio.wait_for(failed.wait(),1)
        await asyncio.sleep(0.01)
        self.rt.receive(10,'Уточнение',12,sender_id=10)
        await self.finish()
        self.rt.telegram.send.assert_awaited_once()
        self.assertEqual(self.rt.telegram.send.call_args.args[1],'Ответ на уточнение')
        self.assertEqual(self.store.rows('SELECT * FROM pending_replies'),[])
        self.assertNotIn(True,[call.args[0].offline for call in self.rt.telegram.client.call_args_list])

    async def test_uncertain_send_is_never_retried(self):
        self.store.message(10,'user','Привет',11,sender_id=10)
        async def send(*args,**kwargs):
            kwargs['before_dispatch']()
            raise OSError('Connection lost')
        self.rt.telegram.send.side_effect = send
        await self.rt.retry_reply(10,11)
        await self.finish()
        row = self.store.rows('SELECT * FROM pending_replies')[0]
        self.assertEqual(row['status'],'uncertain')
        self.rt.telegram.send.assert_awaited_once()
        with self.assertRaises(ValueError):
            await self.rt.retry_reply(10,11)

    async def test_restart_recovers_generation_and_preserves_uncertain_dispatch(self):
        self.store.execute("INSERT INTO pending_replies(contact_id,telegram_id,first_message_id,due,status) VALUES (10,11,1,0,'running')")
        self.store.execute("INSERT INTO pending_replies(contact_id,telegram_id,first_message_id,due,status) VALUES (20,12,2,0,'dispatch')")
        reopened = Store(Path(self.tmp.name))
        try:
            self.assertEqual([row['status'] for row in reopened.rows('SELECT status FROM pending_replies ORDER BY contact_id')],['pending','uncertain'])
        finally:
            reopened.close()

    async def test_pending_reply_is_resumed_at_runtime_start(self):
        self.store.message(10,'user','Привет',11,sender_id=10)
        self.store.execute("INSERT INTO pending_replies(contact_id,telegram_id,first_message_id,due) VALUES (10,11,1,0)")
        self.rt.telegram.start = AsyncMock()
        self.rt.loop = AsyncMock()
        await self.rt.start()
        await self.finish()
        self.rt.telegram.send.assert_awaited_once()
        self.assertEqual(self.store.rows('SELECT * FROM pending_replies'),[])

    async def test_repeated_provider_failures_stop_without_repeated_sends(self):
        self.store.update_settings({'read_delay_min':0,'read_delay_max':0})
        last_failure = asyncio.Event()
        count = 0
        async def model(*args,**kwargs):
            nonlocal count
            count += 1
            if count == 5: last_failure.set()
            raise ModelUnavailable('429',retry_after=1)
        self.rt.model.run.side_effect = model
        self.rt.receive(10,'Привет',11,sender_id=10)
        await asyncio.wait_for(last_failure.wait(),20)
        await self.finish()
        self.assertEqual(self.rt.model.run.await_count,5)
        self.rt.telegram.send.assert_not_awaited()
        self.assertEqual(self.store.rows('SELECT status FROM pending_replies')[0]['status'],'failed')

    async def test_retry_api_auth_and_original_sender_and_completed_reply(self):
        from app.api import app
        app.state.store, app.state.runtime = self.store,self.rt
        self.store.message(10,'user','Привет',11,sender_id=10)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8790',
            headers={'Origin':'http://127.0.0.1:8790'}) as client:
            url = '/api/contacts/10/retry-reply'
            self.assertEqual((await client.post(url,json={'telegram_id':11})).status_code,401)
            client.cookies.set('alisa_session',create_session(self.store))
            self.assertEqual((await client.post(url,json={'telegram_id':9})).status_code,400)
            self.assertEqual((await client.post('/api/contacts/1000000001/retry-reply',json={'telegram_id':11})).status_code,400)
            response = await client.post(url,json={'telegram_id':11})
            self.assertEqual(response.status_code,200)
            await self.finish()
            self.assertEqual((await client.post(url,json={'telegram_id':11})).status_code,400)
