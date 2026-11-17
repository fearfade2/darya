import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from app.runtime import Runtime
from app.store import Store

OWNER = 1000000001


class CustomSettingsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name))
        self.store.upsert_contact(OWNER, 'Владелец')
        self.store.upsert_contact(10, 'Другой')
        self.store.update_settings({'owner_id': OWNER, 'enabled': True, 'natural_enabled': False, 'min_interval': 0, 'reply_idle_seconds':0})
        self.rt = Runtime(self.store)
        self.rt.telegram.ready = lambda: True
        self.rt.telegram.client = AsyncMock()
        self.rt.telegram.send = AsyncMock(return_value=100)
        self.store.message(OWNER, 'user', 'Создай мою настройку', 1, sender_id=OWNER)

    async def asyncTearDown(self):
        await self.rt.stop()
        self.store.close()
        self.tmp.cleanup()

    async def action(self, name, args, contact_id=OWNER):
        return await self.rt.control.action(contact_id, 1, name, args, trusted=True)

    async def test_create_update_list_delete_and_persist(self):
        setting={'name':'Юмор', 'value':3, 'instruction':'Количество юмора по шкале от 0 до 10'}
        created=await self.action('create_custom_setting', setting)
        self.assertEqual(created['setting']['name'],'юмор')
        await self.action('update_custom_setting', {'name':'Юмор','value':7})
        view=self.rt.control.view(self.store.rows('SELECT * FROM messages WHERE telegram_id=1')[0])
        self.assertEqual(view['custom_settings'][0]['value'],7)
        reopened=Store(Path(self.tmp.name))
        self.assertEqual(reopened.custom_settings(), self.store.custom_settings())
        reopened.close()
        await self.action('delete_custom_setting', {'name':'юмор'})
        self.assertEqual((await self.action('list_custom_settings', {}))['custom_settings'],[])

    async def test_custom_rules_affect_model_context_and_update_immediately(self):
        await self.action('create_custom_setting', {'name':'цвет настроения','value':'синий','instruction':'Используй значение как метафору настроения в ответах'})
        prompt=self.rt.model.context(OWNER)[0]['content']
        self.assertIn('цвет настроения',prompt)
        self.assertIn('синий',prompt)
        await self.action('update_custom_setting', {'name':'цвет настроения','value':'зелёный'})
        prompt=self.rt.model.context(OWNER)[0]['content']
        self.assertIn('зелёный',prompt)
        self.assertNotIn('синий',prompt)

    async def test_custom_parameters_cannot_overwrite_builtin_fields_or_change_types(self):
        for name in ('owner_id','typing_enabled','ai_key','secrets'):
            with self.assertRaises(ValueError):
                await self.action('create_custom_setting', {'name':name,'value':True,'instruction':'Применяй к ответам'})
        await self.action('create_custom_setting', {'name':'сарказм','value':True,'instruction':'Добавляй мягкий сарказм, если включено'})
        for value in (1, 'yes', {}, float('nan')):
            with self.assertRaises(ValueError):
                await self.action('update_custom_setting', {'name':'сарказм','value':value})

    async def test_personal_rules_do_not_leak_into_other_contacts(self):
        await self.action('create_custom_setting', {'name':'обращение','value':'капитан',
            'instruction':'Называй владельца выбранным обращением','scope':'owner'})
        self.assertIn('капитан',self.rt.model.context(OWNER)[0]['content'])
        self.assertNotIn('капитан',self.rt.model.context(10)[0]['content'])

    async def test_other_contact_and_forwarded_owner_cannot_create(self):
        self.store.message(10,'user','создай настройку',1,sender_id=10)
        args={'name':'юмор','value':3,'instruction':'Количество юмора в ответах'}
        with self.assertRaises(ValueError):
            await self.action('create_custom_setting',args,10)
        self.store.execute('UPDATE messages SET forwarded=1 WHERE contact_id=?',(OWNER,))
        with self.assertRaises(ValueError):
            await self.action('create_custom_setting',args)
        self.assertEqual(self.store.custom_settings(),[])

    async def test_screenshot_presence_phrases_change_only_reply_online(self):
        for text in ('когда читаешь мое сообщение будь в сети пока не пришлешь свое соо',
                     'нет в сети типо будь и чат читай пока не ответишь'):
            self.assertEqual(self.rt.control.parse([text]),[{'name':'configure_darya','args':{'settings':{'online_during_reply':True}}}])

    async def test_custom_setting_commands_work_without_model_planning(self):
        self.rt.model.plan_owner=AsyncMock(side_effect=ValueError('429'))
        commands=['создай настройку юмор 3 из 10','поставь юмор на 7','создай правило называть меня капитаном','удали настройку юмор']
        replies=[]
        for index,text in enumerate(commands,20):
            self.store.message(OWNER,'user',text,index,sender_id=OWNER)
            plan=self.rt.control.parse([text])
            self.assertTrue(plan)
            replies.append(await self.rt.control.execute_plan(OWNER,index,plan))
        self.rt.model.plan_owner.assert_not_awaited()
        self.assertIn('7',replies[1])
        remaining=self.store.custom_settings()
        self.assertEqual(len(remaining),1)
        self.assertEqual(remaining[0]['scope'],'owner')

    async def test_online_covers_read_generation_and_send_with_typing_disabled(self):
        self.store.update_settings({'online_during_reply':True,'typing_enabled':False,'presence_enabled':False})
        stages=[]
        async def read(*args):
            stages.append(('read',self.rt.telegram.online))
        async def run(*args,**kwargs):
            stages.append(('model',self.rt.telegram.online))
            await self.rt.telegram.periodic_presence(False)
            stages.append(('quiet_scheduler',self.rt.telegram.online))
            return 'Ответ'
        async def send(*args,**kwargs):
            stages.append(('send',self.rt.telegram.online))
            return 100
        self.rt.telegram.read=read
        self.rt.model.run=run
        self.rt.telegram.send=send
        await self.rt.reply(10,'Привет',11)
        self.assertEqual(stages,[('read',True),('model',True),('quiet_scheduler',True),('send',True)])
        self.assertFalse(self.rt.telegram.online)
        self.assertEqual(self.rt.telegram.reply_presence_count,0)

    async def test_parallel_replies_hold_online_until_last_send(self):
        tg=self.rt.telegram
        await tg.begin_reply_presence()
        await tg.begin_reply_presence()
        await tg.end_reply_presence()
        self.assertTrue(tg.online)
        self.assertEqual(tg.reply_presence_count,1)
        tg.last_presence_update=0
        await tg.periodic_presence(False)
        self.assertFalse(tg.client.call_args.args[0].offline)
        await tg.end_reply_presence()
        self.assertFalse(tg.online)

    async def test_model_failure_releases_presence(self):
        self.store.update_settings({'online_during_reply':True})
        self.rt.model.run=AsyncMock(side_effect=ValueError('Нет ответа'))
        self.rt.telegram.read=AsyncMock()
        await self.rt.reply(10,'Привет',11)
        self.assertEqual(self.rt.telegram.reply_presence_count,0)
        self.assertFalse(self.rt.telegram.online)

    async def test_new_message_cancels_generation_and_keeps_online_and_all_context(self):
        self.store.update_settings({'online_during_reply':True,'burst_window':0})
        started=asyncio.Event()
        contexts=[]
        async def run(contact_id,*args,**kwargs):
            contexts.append(self.rt.model.context(contact_id))
            if len(contexts)==1:
                started.set()
                await asyncio.Event().wait()
            return 'Ответ с уточнением'
        self.rt.model.run=run
        self.rt.telegram.read=AsyncMock()
        self.rt.receive(10,'Первая часть',11,sender_id=10)
        await asyncio.wait_for(started.wait(),1)
        self.rt.receive(10,'Уточнение к теме',12,sender_id=10)
        await asyncio.gather(*list(self.rt.work),return_exceptions=True)
        self.assertEqual(len(contexts),2)
        texts=json.dumps(contexts[1],ensure_ascii=False)
        self.assertIn('Первая часть',texts)
        self.assertIn('Уточнение к теме',texts)
        self.rt.telegram.send.assert_awaited_once()
        self.rt.telegram.read.assert_any_await(10,12)
        statuses=[call.args[0].offline for call in self.rt.telegram.client.call_args_list]
        self.assertTrue(statuses[-1])
        self.assertNotIn(True,statuses[:-1])

    async def test_new_message_stops_actual_typing_before_dispatch(self):
        self.store.update_settings({'burst_window':0,'typing_enabled':True,'typing_pause_enabled':False,
                                    'typing_min_seconds':1,'typing_max_seconds':1})
        from app.telegram import Telegram
        self.rt.telegram.send=Telegram.send.__get__(self.rt.telegram,Telegram)
        started=asyncio.Event()
        exits=[]
        @asynccontextmanager
        async def typing():
            try:
                yield
            finally:
                exits.append(True)
        self.rt.telegram.client.action=Mock(side_effect=lambda *args: typing())
        self.rt.telegram.client.send_message=AsyncMock(return_value=SimpleNamespace(id=100))
        self.rt.model.run=AsyncMock(side_effect=['Устаревший ответ','Новый ответ'])
        self.rt.telegram.read=AsyncMock()
        sleeps=0
        async def pause(seconds):
            nonlocal sleeps
            sleeps+=1
            if sleeps==1:
                started.set()
                await asyncio.Event().wait()
        with patch('app.telegram.asyncio.sleep',side_effect=pause):
            self.rt.receive(10,'Первое сообщение',11,sender_id=10)
            await asyncio.wait_for(started.wait(),1)
            self.rt.receive(10,'Нет, другая тема',12,sender_id=10)
            await asyncio.gather(*list(self.rt.work),return_exceptions=True)
        self.rt.telegram.client.send_message.assert_awaited_once()
        self.assertEqual(self.rt.telegram.client.send_message.call_args.args[1],'Новый ответ')
        self.assertEqual(len(exits),2)

    async def test_new_message_during_wire_send_does_not_cancel_or_duplicate_delivery(self):
        self.store.update_settings({'burst_window':0})
        dispatched=asyncio.Event()
        release=asyncio.Event()
        sends=[]
        async def send(contact_id,text,**kwargs):
            kwargs['before_dispatch']()
            sends.append(text)
            if len(sends)==1:
                dispatched.set()
                await release.wait()
            return 100+len(sends)
        self.rt.telegram.send=send
        self.rt.telegram.read=AsyncMock()
        self.rt.model.run=AsyncMock(side_effect=['Первый ответ','Ответ на уточнение'])
        self.rt.receive(10,'Первое сообщение',11,sender_id=10)
        await asyncio.wait_for(dispatched.wait(),1)
        first=self.rt.reply_states[10]['task']
        self.rt.receive(10,'Уточнение',12,sender_id=10)
        self.assertFalse(first.cancelling())
        release.set()
        await asyncio.gather(*list(self.rt.work),return_exceptions=True)
        self.assertEqual(sends,['Первый ответ','Ответ на уточнение'])
        self.assertEqual(len(self.store.rows("SELECT * FROM messages WHERE contact_id=10 AND role='assistant'")),2)

    async def test_multiple_queued_owner_commands_are_merged_without_losing_one(self):
        self.store.update_settings({'burst_window':0})
        dispatched=asyncio.Event()
        release=asyncio.Event()
        queued={12:asyncio.Event(),13:asyncio.Event()}
        original_reply=self.rt.reply
        async def reply(contact_id,*args,**kwargs):
            if kwargs.get('telegram_id') in queued:
                queued[kwargs['telegram_id']].set()
            return await original_reply(contact_id,*args,**kwargs)
        self.rt.reply=reply
        sends=[]
        async def send(contact_id,text,**kwargs):
            kwargs['before_dispatch']()
            sends.append(text)
            if len(sends)==1:
                dispatched.set()
                await release.wait()
            return 100+len(sends)
        self.rt.telegram.send=send
        self.rt.telegram.read=AsyncMock()
        self.rt.model.plan_owner=AsyncMock(return_value=[])
        self.rt.model.run=AsyncMock(return_value='Привет')
        self.rt.receive(OWNER,'Привет',11,sender_id=OWNER)
        await asyncio.wait_for(dispatched.wait(),1)
        self.rt.receive(OWNER,'отключи реакции',12,sender_id=OWNER)
        await asyncio.wait_for(queued[12].wait(),1)
        self.rt.receive(OWNER,'отключи память',13,sender_id=OWNER)
        await asyncio.wait_for(queued[13].wait(),1)
        release.set()
        await asyncio.gather(*list(self.rt.work),return_exceptions=True)
        self.assertFalse(self.store.settings()['reactions_enabled'])
        self.assertFalse(self.store.settings()['memory_enabled'])
        self.assertEqual(len(sends),2)
        self.rt.model.run.assert_awaited_once()
        self.assertEqual(self.rt.pending_batches,{})

    async def test_owner_confirmation_typing_can_stop_without_repeating_completed_command(self):
        self.store.update_settings({'burst_window':0})
        started=asyncio.Event()
        attempts=0
        sent=[]
        async def send(contact_id,text,**kwargs):
            nonlocal attempts
            attempts+=1
            if attempts==1:
                started.set()
                await asyncio.Event().wait()
            kwargs['before_dispatch']()
            sent.append(text)
            return 100
        self.rt.telegram.send=send
        self.rt.telegram.read=AsyncMock()
        self.rt.receive(OWNER,'отвечай быстрее',11,sender_id=OWNER)
        await asyncio.wait_for(started.wait(),1)
        self.rt.receive(OWNER,'отключи память',12,sender_id=OWNER)
        await asyncio.gather(*list(self.rt.work),return_exceptions=True)
        self.assertFalse(self.store.settings()['natural_enabled'])
        self.assertFalse(self.store.settings()['memory_enabled'])
        self.assertEqual(len(sent),1)
        self.assertIn('Память',sent[0])
