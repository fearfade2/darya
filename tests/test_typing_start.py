import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
import tempfile
import time
import unittest
from unittest.mock import AsyncMock,Mock

from telethon import functions,types
from app.runtime import Runtime
from app.store import Store


class TypingStartTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.store=Store(Path(self.tmp.name))
        self.store.upsert_contact(10,'Друг')
        self.store.upsert_contact(1000000001,'Владелец')
        self.store.update_settings(dict(owner_id=1000000001,enabled=True,natural_enabled=True,read_delay_min=.01,read_delay_max=.01,
            reply_delay_min=0,reply_delay_max=0,burst_window=0,min_interval=0,typing_enabled=True,
            typing_during_generation=True,typing_start_delay=.03,typing_min_seconds=0,typing_max_seconds=3,
            typing_pause_enabled=False,online_during_reply=True,reply_online_immediate=True,reply_idle_seconds=0))
        self.rt=Runtime(self.store)
        self.rt.telegram.ready=lambda:True
        self.rt.telegram.client=AsyncMock()
        self.rt.telegram.client.send_message.return_value=SimpleNamespace(id=100)
        @asynccontextmanager
        async def action(*args,**kwargs):yield
        self.rt.telegram.client.action=Mock(side_effect=action)
        self.rt.telegram.read=AsyncMock()
        self.gate=asyncio.Event()
        async def model(*args,**kwargs):
            await self.gate.wait()
            return 'Готовый ответ. '*80
        self.rt.model.run=AsyncMock(side_effect=model)
        self.rt.model.plan_owner=AsyncMock(return_value=[])
        self.requests=[]
        async def request(value):
            if isinstance(value,functions.messages.SetTypingRequest):
                self.requests.append((time.monotonic(),value.action))
            return True
        self.rt.telegram.client.side_effect=request

    async def asyncTearDown(self):
        await self.rt.stop()
        self.store.close()
        self.tmp.cleanup()

    async def finish(self):
        await asyncio.wait_for(asyncio.gather(*list(self.rt.work),return_exceptions=True),1)

    async def test_typing_starts_on_timer_while_model_waits_and_ready_answer_sends_without_length_delay(self):
        start=time.monotonic()
        self.rt.receive(10,'Привет',11,sender_id=10)
        await asyncio.sleep(.02)
        self.assertEqual(self.requests,[])
        await asyncio.sleep(.025)
        self.assertIsInstance(self.requests[0][1],types.SendMessageTypingAction)
        self.assertGreaterEqual(self.requests[0][0]-start,.025)
        self.rt.telegram.client.send_message.assert_not_awaited()
        self.rt.telegram.read.assert_awaited_once_with(10,11)
        ready=time.monotonic()
        self.gate.set()
        await self.finish()
        self.assertLess(time.monotonic()-ready,.1)
        self.rt.telegram.client.send_message.assert_awaited_once()
        self.rt.telegram.client.action.assert_not_called()
        self.assertIsInstance(self.requests[-1][1],types.SendMessageCancelAction)
        self.assertEqual(self.rt.telegram.reply_typing,{})

    async def test_new_message_cancels_old_typing_then_restarts_for_latest_reply(self):
        self.rt.receive(10,'Первое',11,sender_id=10)
        await asyncio.sleep(.045)
        self.assertIsInstance(self.requests[0][1],types.SendMessageTypingAction)
        self.rt.receive(10,'Уточнение',12,sender_id=10)
        await asyncio.sleep(.02)
        self.assertIsInstance(self.requests[-1][1],types.SendMessageCancelAction)
        await asyncio.sleep(.025)
        self.assertIsInstance(self.requests[-1][1],types.SendMessageTypingAction)
        self.assertEqual(self.rt.telegram.last_typing_start['telegram_id'],12)
        self.gate.set()
        await self.finish()
        self.rt.telegram.client.send_message.assert_awaited_once()
        self.assertEqual(self.rt.telegram.reply_typing,{})

    async def test_disabled_contact_and_duplicate_input_do_not_create_new_typing(self):
        self.store.execute('UPDATE contacts SET enabled=0 WHERE id=10')
        self.assertFalse(self.rt.receive(10,'Привет',11,sender_id=10))
        self.assertEqual(self.rt.telegram.reply_typing,{})
        self.store.execute('UPDATE contacts SET enabled=1 WHERE id=10')
        self.assertTrue(self.rt.receive(10,'Привет',11,sender_id=10))
        original=self.rt.telegram.reply_typing[10]
        self.assertFalse(self.rt.receive(10,'Привет',11,sender_id=10))
        self.assertIs(self.rt.telegram.reply_typing[10],original)
        self.gate.set()
        await self.finish()

    async def test_owner_greeting_skips_command_planner_and_typing_timer_command_is_direct(self):
        self.assertEqual(self.rt.control.parse(['начинай печатать через 1,5 секунды']),[
            {'name':'configure_darya','args':{'settings':{'typing_enabled':True,'typing_during_generation':True,'typing_start_delay':1.5}}}])
        self.gate.set()
        self.rt.receive(1000000001,'Ку',11,sender_id=1000000001)
        await self.finish()
        self.rt.model.plan_owner.assert_not_awaited()
        self.rt.model.run.assert_awaited_once()

    async def test_typing_timer_already_runs_during_photo_download(self):
        photo_gate=asyncio.Event()
        async def download(*args,**kwargs):
            await photo_gate.wait()
            raise OSError('Temporary download failure')
        self.rt.telegram.client.download_media.side_effect=download
        event=SimpleNamespace(raw_text='Фото',is_private=True,is_group=False,id=11,
            message=SimpleNamespace(photo=True,document=None,file=SimpleNamespace(size=100),fwd_from=None),
            get_sender=AsyncMock(return_value=types.User(id=10,first_name='Друг')))
        task=asyncio.create_task(self.rt.telegram.on_message(event))
        try:
            await asyncio.sleep(.045)
            self.assertIsInstance(self.requests[0][1],types.SendMessageTypingAction)
            self.assertFalse(task.done())
            photo_gate.set()
            await task
            self.gate.set()
            await self.finish()
            self.assertEqual(self.rt.telegram.reply_typing,{})
        finally:
            task.cancel()
            await asyncio.gather(task,return_exceptions=True)
