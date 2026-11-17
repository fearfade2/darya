import asyncio
import json
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch

from telethon import types

from app.runtime import Runtime
from app.store import Store
from app.voice_messages import format_request, reply_format, spoken_text, voice_note

OWNER = 1000000001


class FormatTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name))
        self.store.update_settings({'owner_id': OWNER})
        for id in [10,20,OWNER]:self.store.upsert_contact(id,str(id))
        self.store.upsert_contact(-100,'group',kind='group')

    def tearDown(self):
        self.store.close();self.tmp.cleanup()

    def messages(self, text, sender=10, forwarded=False):
        return [{'text':text,'sender_id':sender,'forwarded':forwarded}]

    def test_direct_requests_and_call_commands_are_different(self):
        self.assertEqual(format_request('общайся только голосовыми'),('voice',True))
        self.assertEqual(format_request('Даша, пиши мне только голосовыми сообщениями'),('voice',True))
        self.assertEqual(format_request('отвечай текстом'),('text',True))
        self.assertEqual(format_request('не надо голосовых'),('text',True))
        self.assertEqual(format_request('ответь голосовым'),('voice',False))
        self.assertEqual(format_request('расскажи историю голосом'),('voice',False))
        for text in ['отвечай голосом в звонке','он сказал «ответь голосом»','не ответь голосом','ты умеешь голосовыми?']:
            self.assertIsNone(format_request(text),text)

    def test_voice_shorthand_and_reverse_word_order(self):
        for text in ['Гс отправь', 'голосовое запиши', 'мне гс скинь пожалуйста',
                     'гс', 'Можно гс?', 'а гс?', 'гс, пожалуйста']:
            self.assertEqual(format_request(text), ('voice', False), text)
        self.assertEqual(format_request('голосовыми отвечай'), ('voice', True))
        self.assertEqual(format_request('текстом ответь'), ('text', False))
        self.assertEqual(format_request('текстом теперь отвечай'), ('text', True))

    def test_reverse_requests_do_not_match_quotes_negations_or_longer_words(self):
        for text in ['не гс отправь', 'он сказал гс отправь',
                     '«гс отправь»', '"гс"', 'гс отправь в звонке', 'пришли гссылку',
                     'гс отправься', 'ты умеешь отправлять гс?']:
            self.assertIsNone(format_request(text), text)

    def test_negative_voice_requests_switch_persistent_mode_off(self):
        for text in ['не отправляй гс', 'гс не отправляй', 'не присылай мне голосовые', 'не отвечай голосом']:
            self.store.set_reply_mode(10,10,'voice')
            self.assertEqual(reply_format(self.store,10,self.messages(text)),'text',text)
            self.assertEqual(self.store.reply_mode(10,10),'text',text)

    def test_voice_preference_does_not_swallow_other_owner_commands(self):
        from app.control import OwnerControl
        control=OwnerControl(NS(store=self.store))
        for text in ['отключи реакции и ответь голосовым','ответь голосовым, а ещё поменяй имя на Даша',
                     'не отправляй гс и включи реакции']:
            self.assertIsNone(control.parse([text]),text)
            self.assertIsNotNone(format_request(text),text)
        self.assertEqual(control.parse(['гс отправь']),[])

    def test_shorthand_does_not_persist_or_accept_forwarded_requests(self):
        self.assertEqual(reply_format(self.store, 10, self.messages('гс отправь')), 'voice')
        self.assertEqual(reply_format(self.store, 10, self.messages('привет')), 'text')
        self.assertEqual(reply_format(self.store, 10, self.messages('гс отправь', forwarded=True)), 'text')
        self.assertEqual(reply_format(self.store, 10, self.messages('гс отправь', sender=20)), 'text')

    def test_mode_is_per_person_and_survives_restart(self):
        self.assertEqual(reply_format(self.store,10,self.messages('общайся только голосовыми')),'voice')
        self.assertEqual(reply_format(self.store,20,self.messages('привет',20)),'text')
        directory=self.store.directory;self.store.close();self.store=Store(directory)
        self.assertEqual(reply_format(self.store,10,self.messages('как дела')),'voice')
        self.assertEqual(reply_format(self.store,10,self.messages('отвечай текстом')),'text')
        self.assertEqual(reply_format(self.store,10,self.messages('как дела')),'text')

    def test_one_off_does_not_change_persistent_mode(self):
        self.assertEqual(reply_format(self.store,10,self.messages('ответь голосовым')),'voice')
        self.assertEqual(reply_format(self.store,10,self.messages('привет')),'text')
        self.store.set_reply_mode(10,10,'voice')
        self.assertEqual(reply_format(self.store,10,self.messages('ответь текстом')),'text')
        self.assertEqual(self.store.reply_mode(10,10),'voice')

    def test_forwarded_and_wrong_sender_cannot_change_preferences(self):
        self.assertEqual(reply_format(self.store,10,self.messages('общайся только голосовыми',forwarded=True)),'text')
        self.assertEqual(reply_format(self.store,10,self.messages('общайся только голосовыми',sender=20)),'text')
        self.assertFalse(self.store.rows('SELECT * FROM reply_modes'))

    def test_group_mode_changes_only_replies_to_that_participant(self):
        self.assertEqual(reply_format(self.store,-100,self.messages('общайся только голосовыми',10)),'voice')
        self.assertEqual(reply_format(self.store,-100,self.messages('привет',20)),'text')
        self.assertEqual(reply_format(self.store,-100,self.messages('привет',10)),'voice')
        self.assertEqual(reply_format(self.store,10,self.messages('привет',10)),'text')

    def test_latest_preference_wins_inside_burst(self):
        messages=self.messages('общайся только голосовыми')+self.messages('нет, отвечай текстом')
        self.assertEqual(reply_format(self.store,10,messages),'text')
        self.assertEqual(self.store.reply_mode(10,10),'text')

    def test_owner_can_disable_feature_without_others_changing_global_settings(self):
        self.store.update_settings({'voice_messages_enabled':False})
        before=self.store.settings()
        self.assertEqual(reply_format(self.store,10,self.messages('общайся только голосовыми')),'text')
        self.assertEqual(before,self.store.settings())
        self.assertEqual(self.store.reply_mode(10,10),'voice')

    def test_speech_trims_at_sentence_or_word_and_removes_markup(self):
        self.assertEqual(spoken_text('**Привет** https://example.com',100),'Привет')
        text='Первое предложение достаточно длинное. Второе предложение тоже долгое.'
        self.assertEqual(spoken_text(text,50),'Первое предложение достаточно длинное.')


class VoiceReplyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=Store(Path(self.tmp.name))
        for id in [10,20,OWNER]:self.store.upsert_contact(id,str(id))
        self.store.update_settings({'owner_id':OWNER,'enabled':True,'natural_enabled':False,'min_interval':0,'typing_enabled':False,'online_during_reply':False})
        self.rt=Runtime(self.store)
        self.rt.telegram.ready=Mock(return_value=True)
        self.rt.telegram.send=AsyncMock(return_value=1000)
        self.rt.telegram.send_voice=AsyncMock(return_value=1001)
        self.rt.telegram.read=AsyncMock();self.rt.telegram.presence=AsyncMock()
        self.rt.model.run=AsyncMock(return_value='Привет! Как дела?')
        self.rt.model.plan_owner=AsyncMock(return_value=[])
        self.rt.calls.voice.synthesize=AsyncMock(return_value=bytes(9600))
        self.rt.calls.voice.release=AsyncMock()
        self.encoding_paths=[]
        @asynccontextmanager
        async def fake_note(*args,**kwargs):
            with tempfile.TemporaryDirectory() as directory:
                path=Path(directory)/'voice.ogg';path.write_bytes(b'OggS');self.encoding_paths.append(path)
                yield path,1
        self.render_patch=patch('app.runtime.voice_note',fake_note);self.render_patch.start()

    async def asyncTearDown(self):
        self.render_patch.stop();await self.rt.stop();self.store.close();self.tmp.cleanup()

    def incoming(self,id,text,msg):
        self.store.message(id,'user',text,msg,sender_id=id)

    async def test_requested_reply_is_voice_only_and_next_reply_is_text(self):
        self.incoming(10,'ответь голосовым',1)
        await self.rt.reply(10,telegram_id=1,prepared=True)
        self.rt.telegram.send_voice.assert_awaited_once()
        self.rt.telegram.send.assert_not_awaited()
        self.assertTrue(self.rt.model.run.call_args.kwargs['voice_reply'])
        rows=self.store.history(10)
        self.assertEqual(rows[-1]['kind'],'voice');self.assertEqual(rows[-1]['text'],'Привет! Как дела?')
        self.assertFalse(self.encoding_paths[-1].exists())
        self.rt.calls.voice.release.assert_awaited()
        self.incoming(10,'привет',2)
        await self.rt.reply(10,telegram_id=2,prepared=True)
        self.rt.telegram.send.assert_awaited_once()

    async def test_screenshot_request_sends_voice_to_its_actual_sender(self):
        self.incoming(10, 'Гс отправь', 619)
        await self.rt.reply(10, telegram_id=619, prepared=True)
        self.rt.telegram.send_voice.assert_awaited_once()
        self.assertEqual(self.rt.telegram.send_voice.call_args.args[0], 10)
        self.assertEqual(self.rt.telegram.send_voice.call_args.kwargs['reply_to'], 619)
        self.assertTrue(self.rt.model.run.call_args.kwargs['voice_reply'])
        self.rt.telegram.send.assert_not_awaited()
        self.assertEqual(self.store.history(10)[-1]['reply_to_telegram_id'], 619)
        self.assertEqual(self.rt.control.parse(['Гс отправь']), [])

    async def test_persistent_voice_and_switch_back_do_not_affect_another_user(self):
        for id,text,msg in [(10,'общайся только голосовыми',1),(10,'как дела',2),(20,'привет',3),(10,'отвечай текстом',4),(10,'привет',5)]:
            self.incoming(id,text,msg);await self.rt.reply(id,telegram_id=msg,prepared=True)
        self.assertEqual(self.rt.telegram.send_voice.await_count,2)
        self.assertEqual(self.rt.telegram.send.await_count,3)
        self.assertEqual(self.store.reply_mode(10,10),'text')
        self.assertEqual(self.store.reply_mode(20,20),'text')

    async def test_owner_preference_is_not_an_administrative_call_command(self):
        self.incoming(OWNER,'общайся только голосовыми',1)
        self.assertEqual(self.rt.control.parse(['общайся только голосовыми']),[])
        await self.rt.reply(OWNER,telegram_id=1,prepared=True)
        self.rt.model.plan_owner.assert_not_awaited()
        self.rt.telegram.send_voice.assert_awaited_once()
        self.assertEqual(self.store.settings()['call_mode'],'listen')

    async def test_proactive_messages_respect_private_voice_preference(self):
        self.store.set_reply_mode(10,10,'voice')
        self.store.execute('UPDATE contacts SET proactive=1 WHERE id=10')
        self.store.update_settings({'quiet_start':0,'quiet_end':0})
        await self.rt.reply(10,proactive=True,prepared=True)
        self.rt.telegram.send_voice.assert_awaited_once()
        self.rt.telegram.send.assert_not_awaited()

    async def test_mode_is_saved_even_when_generation_temporarily_fails(self):
        from app.ai import ModelUnavailable
        self.incoming(10,'общайся только голосовыми',1)
        self.rt.model.run.side_effect=ModelUnavailable('test')
        await self.rt.reply(10,telegram_id=1,prepared=True)
        self.assertEqual(self.store.reply_mode(10,10),'voice')
        self.rt.telegram.send_voice.assert_not_awaited()
        self.assertEqual(self.store.rows('SELECT status FROM pending_replies')[0]['status'],'pending')

    async def test_superseded_reply_during_upload_is_cancelled_before_dispatch(self):
        self.incoming(10,'ответь голосовым',1)
        async def cancel(*args,**kwargs):
            self.rt.reply_states[10]['superseded']=True
            kwargs['before_dispatch']()
            self.fail('must be cancelled')
        self.rt.telegram.send_voice.side_effect=cancel
        with self.assertRaises(asyncio.CancelledError):
            await self.rt.reply(10,telegram_id=1,prepared=True)
        self.assertEqual(len([row for row in self.store.history(10) if row['role']=='assistant']),0)
        self.assertFalse(self.encoding_paths[-1].exists())

    async def test_tts_failure_is_retried_before_any_telegram_dispatch(self):
        from app.voice_messages import VoiceRenderUnavailable
        self.incoming(10,'ответь голосовым',1)
        @asynccontextmanager
        async def unavailable(*args, **kwargs):
            raise VoiceRenderUnavailable('temporary')
            yield
        with patch('app.runtime.voice_note', unavailable):
            await self.rt.reply(10,telegram_id=1,prepared=True)
        self.rt.telegram.send_voice.assert_not_awaited()
        self.assertEqual(self.store.rows('SELECT status FROM pending_replies')[0]['status'],'pending')

    async def test_uncertain_delivery_is_not_resubmitted_by_durable_retry_queue(self):
        self.incoming(10,'ответь голосовым',1)
        self.store.execute('INSERT INTO pending_replies(contact_id,telegram_id,first_message_id,due,attempts,status,error) VALUES (?,?,?,?,?,?,?)',
            (10,1,1,0,0,'running',''))
        async def lost(*args,**kwargs):
            kwargs['before_dispatch']()
            raise ConnectionError('delivery unknown')
        self.rt.telegram.send_voice.side_effect=lost
        await self.rt.reply(10,telegram_id=1,prepared=True)
        self.assertEqual(self.store.rows('SELECT status FROM pending_replies')[0]['status'],'uncertain')
        self.assertFalse(self.encoding_paths[-1].exists())

    async def test_native_send_uses_voice_flag_and_marks_dispatch_after_upload(self):
        from app.telegram import Telegram
        tg=Telegram(self.store,Mock());tg.ready=Mock(return_value=True)
        ordering=[]
        @asynccontextmanager
        async def action(*args):yield
        async def upload(*args):ordering.append('upload');return NS(name='voice.ogg')
        async def send(*args,**kwargs):ordering.append('send');return NS(id=123)
        tg.client=NS(action=action,upload_file=AsyncMock(side_effect=upload),send_file=AsyncMock(side_effect=send))
        result=await tg.send_voice(10,Path('dummy.ogg'),2,reply_to=1,before_dispatch=lambda:ordering.append('dispatch'))
        self.assertEqual(result,123);self.assertEqual(ordering,['upload','dispatch','send'])
        kwargs=tg.client.send_file.call_args.kwargs
        self.assertTrue(kwargs['voice_note']);self.assertTrue(kwargs['attributes'][0].voice)
        self.assertEqual(kwargs['reply_to'],1)

    async def test_mute_permissions_rechecked_after_upload(self):
        from app.telegram import Telegram
        tg=Telegram(self.store,Mock());tg.ready=Mock(return_value=True)
        @asynccontextmanager
        async def action(*args):yield
        async def upload(*args):
            self.store.execute('UPDATE contacts SET enabled=0 WHERE id=10');return NS(name='voice.ogg')
        tg.client=NS(action=action,upload_file=AsyncMock(side_effect=upload),send_file=AsyncMock())
        guard=Mock()
        with self.assertRaisesRegex(ValueError,'Отправка отключена'):
            await tg.send_voice(10,Path('dummy.ogg'),2,before_dispatch=guard)
        guard.assert_not_called();tg.client.send_file.assert_not_awaited()

    async def test_idle_tts_release_does_not_unload_an_active_call(self):
        self.rt.calls.active={'chat_id':-123}
        await self.rt.calls.release_idle_voice()
        self.rt.calls.voice.release.assert_not_awaited()
        self.rt.calls.active=None
        await self.rt.calls.release_idle_voice()
        self.rt.calls.voice.release.assert_awaited_once()


class EncodingTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_opus_encoding_and_automatic_file_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            engine=NS(synthesize=AsyncMock(return_value=bytes(96000)))
            async with voice_note(engine,Path(directory),'Привет') as (path,duration):
                self.assertEqual(path.read_bytes()[:4],b'OggS');self.assertEqual(duration,1)
                process=await asyncio.create_subprocess_exec('ffprobe','-v','error','-show_entries','stream=codec_name','-of','json',str(path),stdout=asyncio.subprocess.PIPE)
                output,_=await process.communicate()
                self.assertEqual(json.loads(output)['streams'][0]['codec_name'],'opus')
                source=path
            self.assertFalse(source.exists())
            self.assertFalse(list((Path(directory)/'outgoing-voice').iterdir()))
