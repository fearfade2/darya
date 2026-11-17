import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch

import numpy as np
from telethon import errors, functions, types
from pytgcalls.types import Device, Direction, Frame, StreamFrames, ChatUpdate

from app.calls import Calls, join_conference
from app.control import OWNER_NAMES
from app.groups import Groups, target
from app.runtime import Runtime
from app.store import Store
from app.voice import CallUnavailable, Utterance

OWNER = 1000000001


class TargetTests(unittest.TestCase):
    def test_telegram_targets_and_voicechat_are_distinct(self):
        self.assertEqual(target('https://t.me/+abcdef')['kind'], 'invite')
        self.assertEqual(target('t.me/joinchat/abcdef')['value'], 'abcdef')
        self.assertEqual(target('@example')['value'], 'example')
        self.assertEqual(target('-100123')['value'], -100123)
        self.assertEqual(target('https://t.me/example?voicechat=abc', calls=True)['invite_hash'], 'abc')
        self.assertEqual(target('https://t.me/call/AbC_123', calls=True)['kind'], 'conference')
        for value in ['https://evil.test/call/abcdef', 'https://t.me/other/123',
                      'https://t.me/+abcdef?pay=true', 'https://t.me/example?start=1',
                      'https://t.me/call/abcdef', 'file:///tmp/secret', 'https://t.me@evil.test/example']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                target(value)

    def test_vad_is_bounded_filters_clicks_and_keeps_speech(self):
        from app.store import DEFAULTS
        settings = DEFAULTS | {'call_silence_seconds': 0.3, 'call_utterance_seconds': 5}
        speech = (np.ones(320) * 5000).astype('<i2').tobytes()
        silence = bytes(640)
        v = Utterance()
        for _ in range(4):
            v.feed(silence, 1, settings)
        v.feed(speech, 1, settings)
        for _ in range(20):
            result = v.feed(silence, 1, settings)
            self.assertIsNone(result)
        for _ in range(30):
            self.assertIsNone(v.feed(speech, 1, settings))
        returned = None
        for _ in range(18):
            result = v.feed(silence, 1, settings)
            returned = result or returned
        self.assertIsNotNone(returned)
        self.assertLess(len(returned), 32000 * 5)
        self.assertEqual(v.speech, 0)
        v = Utterance()
        result = None
        for _ in range(260):
            result = v.feed(speech, 1, settings) or result
        self.assertIsNotNone(result)
        self.assertLessEqual(len(result), 32000 * 5.1)
        self.assertIsNone(v.feed(b'x', 1, settings))
        self.assertIsNone(v.feed(bytes(32000 * 3), 1, settings))


class CallsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name))
        self.store.update_settings({'owner_id': OWNER})
        for id in [OWNER, 10]:
            self.store.upsert_contact(id, str(id))
        self.store.upsert_contact(-100, 'group', kind='group')
        self.store.message(OWNER, 'user', 'Выполни команду', 1, sender_id=OWNER)
        self.rt = Runtime(self.store)
        self.rt.telegram.ready = Mock(return_value=True)
        self.rt.telegram.client = AsyncMock()
        self.rt.telegram.account_id = 1000000002
        self.rt.telegram.rpc = lambda operation: operation()
        self.sdk = NS(play=AsyncMock(), record=AsyncMock(), get_participants=AsyncMock(return_value=[]), mute=AsyncMock(return_value=True),
                      unmute=AsyncMock(return_value=True), leave_call=AsyncMock(),
                      send_frame=AsyncMock(), _binding=NS(stop=AsyncMock(), calls=AsyncMock(return_value={-123:None})), _p2p_configs={})
        self.rt.calls.start = AsyncMock(return_value=self.sdk)
        self.rt.calls.sdk = self.sdk
        self.rt.calls.voice.prepare = AsyncMock()
        self.rt.calls.voice.release = AsyncMock()
        self.rt.calls.voice.transcribe = AsyncMock(return_value='Привет, Даша!')
        self.rt.calls.voice.synthesize = AsyncMock(return_value=bytes(1920))
        self.rt.model.call_reply = AsyncMock(return_value='Привет!')

    async def asyncTearDown(self):
        self.rt.calls.sdk = None
        await self.rt.stop()
        self.store.close()
        self.tmp.cleanup()

    async def test_commands_parse_without_model(self):
        for text, name, args in [
            ('вступи в группу https://t.me/+abcdef', 'join_group', {'target':'https://t.me/+abcdef'}),
            ('выйди из группы @example', 'leave_group', {'target':'@example'}),
            ('подключись к звонку https://t.me/call/abcdef и говори', 'join_call', {'target':'https://t.me/call/abcdef','mode':'talk'}),
            ('выйди из звонка', 'leave_call', {}), ('статус звонка', 'call_status', {}),
            ('отвечай голосом в звонке', 'set_call_mode', {'mode':'talk'}),
            ('выключи микрофон', 'set_call_mode', {'mode':'listen'}),
            ('прими звонок', 'accept_call', {}), ('прими звонок от @example', 'accept_call', {'recipient':'@example'})]:
            with self.subTest(text=text):
                self.assertEqual(self.rt.control.parse([text]), [{'name':name,'args':args}])
        self.assertIsNone(self.rt.control.parse(['Ты умеешь подключаться к звонкам?']))

    async def test_group_and_call_tools_restricted_to_current_owner_message(self):
        names = {'join_group','leave_group','join_call','leave_call','accept_call','call_status','set_call_mode'}
        self.assertTrue(names <= OWNER_NAMES)
        self.store.message(10, 'user', 'Вступи в группу', 2, sender_id=10)
        self.store.message(OWNER, 'user', 'Вступи', 3, sender_id=OWNER, forwarded=True)
        for id, msg, trusted in [(10,2,True), (OWNER,3,True), (OWNER,1,False), (OWNER,999,True)]:
            for name in names:
                with self.subTest(name=name, id=id), self.assertRaises(ValueError):
                    await self.rt.control.action(id,msg,name,{},trusted)
        self.rt.calls.start.assert_not_awaited()

    async def test_membership_reports_pending_and_refuses_paid_invites(self):
        invite = NS(title='test', broadcast=False, subscription_pricing=None, subscription_form_id=None)
        client = self.rt.telegram.client
        client.side_effect = [invite, errors.InviteRequestSentError(None)]
        guard = Mock()
        result = await self.rt.calls.groups.join('https://t.me/+abcdef', guard)
        self.assertEqual(result['status'],'pending')
        guard.assert_called()
        self.assertFalse(self.store.rows('SELECT id FROM contacts WHERE name=\'test\''))
        paid = NS(title='paid',broadcast=False,subscription_pricing=NS(amount=10),subscription_form_id=1)
        client.reset_mock();client.side_effect = None;client.return_value = paid
        with self.assertRaisesRegex(ValueError,'платное'):
            await self.rt.calls.groups.join('https://t.me/+abcdef', Mock())
        self.assertEqual(client.call_count,1)

    async def test_public_group_join_and_leave_use_membership_rpcs(self):
        group = types.Channel(id=123, title='test', photo=types.ChatPhotoEmpty(), date=None,
                              megagroup=True, left=True)
        client = self.rt.telegram.client
        client.get_entity = AsyncMock(return_value=group)
        client.side_effect = None
        client.return_value = NS(chats=[group])
        result = await self.rt.calls.groups.join('@example', Mock())
        self.assertEqual(result['status'], 'joined')
        self.assertIsInstance(client.call_args.args[0], functions.channels.JoinChannelRequest)
        await self.rt.calls.groups.leave('@example', Mock())
        self.assertIsInstance(client.call_args.args[0], functions.channels.LeaveChannelRequest)
        self.assertFalse(self.store.contact(result['group_id'])['enabled'])

    async def test_join_has_no_autostart_records_pcm_and_deduplicates(self):
        group = types.Chat(123,'test',types.ChatPhotoEmpty(),0,None,0)
        self.rt.calls.groups.resolve = AsyncMock(return_value=group)
        result = await self.rt.control.action(OWNER,1,'join_call',{'target':'@example'},trusted=True)
        self.assertTrue(result['connected'])
        self.assertEqual(result['mode'],'listen')
        config = self.sdk.play.call_args.kwargs['config']
        self.assertFalse(config.auto_start)
        stream = self.sdk.record.call_args.args[1]
        self.assertEqual(stream.microphone.parameters.bitrate,16000)
        self.assertEqual(stream.microphone.parameters.channels,1)
        again = await self.rt.control.action(OWNER,1,'join_call',{'target':'@another'},trusted=True)
        self.assertTrue(again['already_completed'])
        self.sdk.play.assert_awaited_once()
        await self.rt.calls.leave()
        self.sdk.leave_call.assert_awaited_once()

    async def test_absent_group_call_is_reported_without_creating_one(self):
        from pytgcalls.exceptions import NoActiveGroupCall
        group = types.Chat(123,'test',types.ChatPhotoEmpty(),0,None,0)
        self.rt.calls.groups.resolve = AsyncMock(return_value=group)
        self.sdk.play.side_effect = NoActiveGroupCall()
        with self.assertRaisesRegex(ValueError,'нет действующего голосового чата'):
            await self.rt.control.action(OWNER,1,'join_call',{'target':'@example'},trusted=True)
        self.assertIsNone(self.rt.calls.active)
        self.sdk.record.assert_not_awaited()

    async def test_administrator_mute_is_checked_when_joining(self):
        self.sdk.get_participants.return_value = [NS(user_id=self.rt.telegram.account_id,muted_by_admin=True)]
        await self.rt.calls.activate(-123,'test','group','talk')
        self.assertTrue(self.rt.calls.status()['admin_muted'])
        self.rt.calls.enqueue(123,bytes(32000))
        await asyncio.sleep(.03)
        self.rt.model.call_reply.assert_not_awaited()
        self.sdk.unmute.assert_not_awaited()

    async def test_positive_conference_id_uses_conference_participant_cache(self):
        getter = AsyncMock(return_value=[NS(user_id=self.rt.telegram.account_id,muted_by_admin=False)])
        self.sdk._app = NS(_bind_client=NS(_cache=NS(get_participant_list=getter)))
        self.sdk._binding.calls.return_value = {123:None}
        await self.rt.calls.activate(123,'conference','conference','talk')
        getter.assert_awaited_once_with(123)
        self.sdk.get_participants.assert_not_awaited()
        self.assertFalse(self.rt.calls.status()['admin_muted'])

    async def test_group_join_rechecks_owner_before_rpc(self):
        group = types.Channel(id=123,title='test',photo=types.ChatPhotoEmpty(),date=None,megagroup=True,left=True)
        async def changed(*args):
            self.store.update_settings({'owner_id':10})
            return group
        self.rt.telegram.client.get_entity = AsyncMock(side_effect=changed)
        with self.assertRaises(ValueError):
            await self.rt.control.action(OWNER,1,'join_group',{'target':'@example'},trusted=True)
        self.rt.telegram.client.assert_not_called()

    async def test_uncertain_join_not_automatically_repeated(self):
        self.rt.calls.groups.join = AsyncMock(side_effect=ConnectionError())
        with self.assertRaisesRegex(ValueError,'не подтвердил'):
            await self.rt.control.action(OWNER,1,'join_group',{'target':'@example'},trusted=True)
        with self.assertRaisesRegex(ValueError,'уже запускалось'):
            await self.rt.control.action(OWNER,1,'join_group',{'target':'@another'},trusted=True)
        self.rt.calls.groups.join.assert_awaited_once()

    async def test_accept_never_dials_when_incoming_call_disappeared(self):
        self.rt.calls.pending[OWNER] = time.monotonic()
        with self.assertRaisesRegex(ValueError,'завершился'):
            await self.rt.calls.accept(None,'talk',Mock())
        self.sdk.play.assert_not_awaited()
        self.sdk._p2p_configs[OWNER] = NS(outgoing=False)
        self.rt.calls.pending.clear()
        with self.assertRaisesRegex(ValueError,'нет входящего'):
            await self.rt.calls.accept(None,'talk',Mock())
        self.sdk.play.assert_not_awaited()

    async def test_remote_incoming_call_does_not_autoanswer(self):
        await self.rt.calls.on_update(self.sdk,ChatUpdate(10,ChatUpdate.Status.INCOMING_CALL))
        self.assertIn(10,self.rt.calls.pending)
        self.sdk.play.assert_not_awaited()
        await self.rt.calls.on_update(self.sdk,ChatUpdate(10,ChatUpdate.Status.DISCARDED_CALL))
        self.assertNotIn(10,self.rt.calls.pending)

    async def test_listen_does_not_generate_or_send_and_forgets_on_leave(self):
        await self.rt.calls.activate(-123,'test','group','listen')
        self.rt.calls.enqueue(123,bytes(32000))
        for _ in range(20):
            if self.rt.calls.heard:break
            await asyncio.sleep(.01)
        self.assertEqual(self.rt.calls.heard,1)
        self.rt.model.call_reply.assert_not_awaited()
        self.sdk.unmute.assert_not_awaited()
        await self.rt.calls.leave()
        self.assertFalse(self.rt.calls.history)
        self.rt.calls.voice.release.assert_awaited()
        self.assertFalse(self.rt.calls.speakers)
        self.assertIsNone(self.rt.calls.worker)
        self.assertTrue(self.rt.calls.queue.empty())

    async def test_talk_uses_pcm_pacing_and_can_be_muted_mid_sentence(self):
        await self.rt.calls.activate(-123,'test','group','talk')
        self.rt.calls.enqueue(1,bytes(32000))
        for _ in range(30):
            if self.rt.calls.spoken:break
            await asyncio.sleep(.01)
        self.assertEqual(self.rt.calls.spoken,1)
        self.sdk.send_frame.assert_awaited_once()
        self.assertEqual(self.sdk.send_frame.call_args.args[1],Device.MICROPHONE)
        task = asyncio.create_task(self.rt.calls.speak(bytes(96000)))
        await asyncio.sleep(.03)
        await self.rt.calls.mode('listen')
        self.assertFalse(await task)
        self.assertLess(self.sdk.send_frame.await_count,10)

    async def test_outgoing_video_and_wrong_call_frames_ignored(self):
        await self.rt.calls.activate(-123,'test','group','listen')
        frame = Frame(10,bytes(640),Frame.Info())
        for event in [StreamFrames(-123,Direction.OUTGOING,Device.MICROPHONE,[frame]),
                      StreamFrames(-123,Direction.INCOMING,Device.CAMERA,[frame]),
                      StreamFrames(-124,Direction.INCOMING,Device.MICROPHONE,[frame])]:
            await self.rt.calls.on_update(self.sdk,event)
        self.assertFalse(self.rt.calls.speakers)

    async def test_watch_auto_leaves_disabled_calls_and_expired_call(self):
        await self.rt.calls.activate(-123,'test','group','listen')
        self.store.update_settings({'calls_enabled':False})
        for _ in range(20):
            if not self.rt.calls.active:break
            await asyncio.sleep(.02)
        self.assertIsNone(self.rt.calls.active)
        self.sdk.leave_call.assert_awaited_once()

    async def test_mode_setting_applies_and_unrelated_settings_preserved(self):
        before = self.store.settings()
        await self.rt.calls.activate(-123,'test','group','talk')
        await self.rt.control.action(OWNER,1,'configure_darya',{'settings':{'call_mode':'listen'}},trusted=True)
        self.assertEqual(self.rt.calls.active['mode'],'listen')
        saved = self.store.settings()
        for key in ['persona','reply_idle_seconds','typing_start_delay','hourly_limit','daily_contact_limit','gifts_enabled']:
            self.assertEqual(saved[key],before[key])

    async def test_conference_link_joins_existing_chain_without_creating_new_call(self):
        cache = Mock()
        invitation = types.InputGroupCallSlug('abcdef')
        chain = types.UpdateGroupCallChainBlocks(invitation, 0, [b'chain'], 0)
        adapter = NS(_cache=cache, _invoke=AsyncMock(return_value=NS(updates=[chain])))
        sdk = NS(_app=NS(_bind_client=adapter),
                 _binding=NS(create_p2p_call=AsyncMock(),stop=AsyncMock()), _connect_call=AsyncMock())
        call = types.GroupCall(9000,123,1,0,1,conference=True)
        client = AsyncMock(return_value=NS(call=call))
        guard = Mock()
        id = await join_conference(sdk,client,'abcdef',self.rt.calls.media(),guard)
        self.assertEqual(id,9000)
        self.assertIsInstance(client.call_args.args[0],functions.phone.GetGroupCallRequest)
        self.assertIsInstance(client.call_args.args[0].call,types.InputGroupCallSlug)
        cache.set_cache.assert_called_once()
        cached = cache.set_cache.call_args.args[1]
        self.assertIsInstance(cached, types.InputGroupCallSlug)
        self.assertEqual(cached.slug, 'abcdef')
        request = adapter._invoke.call_args.args[0]
        self.assertIsInstance(request, functions.phone.GetGroupCallChainBlocksRequest)
        self.assertEqual((request.call.slug, request.sub_chain_id, request.offset, request.limit),
                         ('abcdef', 0, -1, 1))
        args = sdk._connect_call.call_args.args
        self.assertEqual(args[-1],b'chain')
        self.assertTrue(args[2].conference)
        guard.assert_called_once()
        adapter._invoke.return_value = NS(updates=[])
        sdk._connect_call.reset_mock();sdk._binding.create_p2p_call.reset_mock()
        with patch('app.calls.CONFERENCE_CHAIN_TIMEOUT', .01), self.assertRaisesRegex(ValueError,'не готовы'):
            await join_conference(sdk,client,'abcdef',self.rt.calls.media(),Mock())
        sdk._connect_call.assert_not_awaited()
        sdk._binding.create_p2p_call.assert_not_awaited()

    async def test_sdk_uses_existing_telegram_client_and_cleans_up_only_own_handlers(self):
        from telethon import TelegramClient, events
        from telethon.sessions import MemorySession
        client = TelegramClient(MemorySession(), 1, 'abc')
        async def callback(event):
            pass
        client.add_event_handler(callback, events.NewMessage())
        self.rt.telegram.client = client
        self.rt.calls.sdk = None
        del self.rt.calls.start
        with patch('pytgcalls.PyTgCalls.start', new=AsyncMock()):
            sdk = await self.rt.calls.start()
        self.assertIs(sdk.mtproto_client, client)
        self.assertEqual(len(client.list_event_handlers()), 2)
        await self.rt.calls.close()
        self.assertEqual([f for f, _ in client.list_event_handlers()], [callback])
        self.assertIsNone(self.rt.calls.sdk)
        self.assertFalse(sdk._app._bind_client._callbacks)
        await client.disconnect()

    async def test_native_disconnect_does_not_report_connected(self):
        await self.rt.calls.activate(-123,'test','group','listen')
        self.sdk._binding.calls.return_value = {}
        for _ in range(20):
            if not self.rt.calls.active: break
            await asyncio.sleep(.02)
        self.assertFalse(self.rt.calls.status()['connected'])
        self.sdk.leave_call.assert_not_awaited()

    async def test_call_model_context_contains_no_owner_history_or_tools(self):
        from app.ai import Model
        await self.rt.model.close()
        self.rt.model = Model(self.store)
        self.store.message(OWNER,'user','секрет владельца',20,sender_id=OWNER)
        self.store.execute('INSERT INTO memories(contact_id,key,value,created) VALUES (?,?,?,?)',
                           (OWNER,'приватный факт','только владелец',time.time()))
        self.rt.model.completion = AsyncMock(return_value={'content':'Привет!','reasoning_content':'private'})
        result = await self.rt.model.call_reply([{'role':'user','content':'Привет!'}])
        self.assertEqual(result,'Привет!')
        args = self.rt.model.completion.call_args
        self.assertFalse(args.kwargs['with_tools'])
        self.assertFalse(args.kwargs['owner'])
        context = str(args.args[0])
        self.assertNotIn('секрет владельца',context)
        self.assertNotIn('только владелец',context)
        self.assertNotIn('owner_settings',context)

    def ready_for_initiative(self):
        self.rt.calls.last_activity = time.monotonic() - 1000
        self.rt.calls.last_voice_end = self.rt.calls.last_activity
        self.rt.calls.next_initiative = 0

    async def test_worker_greets_without_incoming_audio_and_waits_after_speaking(self):
        await self.rt.calls.activate(-123, 'test', 'group', 'talk')
        await self.rt.calls.initiate()
        self.rt.model.call_reply.assert_not_awaited()
        self.ready_for_initiative()
        for _ in range(40):
            if self.rt.calls.spoken:
                break
            await asyncio.sleep(.02)
        self.assertEqual(self.rt.calls.spoken, 1)
        self.assertEqual(self.rt.calls.heard, 0)
        self.rt.model.call_reply.assert_awaited_once_with([], initiative='greeting')
        self.sdk.send_frame.assert_awaited_once()
        await self.rt.calls.initiate()
        self.assertEqual(self.rt.calls.spoken, 1)

    async def test_initiative_stops_after_two_unanswered_attempts_until_human_reply(self):
        await self.rt.calls.activate(-123, 'test', 'group', 'talk')
        for _ in range(4):
            self.ready_for_initiative()
            await self.rt.calls.initiate()
        self.assertEqual(self.rt.calls.spoken, 2)
        self.assertEqual(self.rt.model.call_reply.call_args.kwargs['initiative'], 'pause')
        self.rt.calls.enqueue(7, bytes(32000))
        for _ in range(30):
            if self.rt.calls.spoken == 3:
                break
            await asyncio.sleep(.01)
        self.assertEqual(self.rt.calls.initiative_attempts, 0)
        self.ready_for_initiative()
        await self.rt.calls.initiate()
        self.assertEqual(self.rt.calls.spoken, 4)
        await self.rt.calls.leave()
        self.assertFalse(self.rt.calls.own_replies)

    async def test_initiative_respects_listen_admin_mute_settings_and_live_speech(self):
        await self.rt.calls.activate(-123, 'test', 'group', 'listen')
        self.ready_for_initiative()
        await self.rt.calls.initiate()
        await self.rt.calls.mode('talk')
        self.ready_for_initiative()
        self.rt.calls.active['admin_muted'] = True
        await self.rt.calls.initiate()
        self.rt.calls.active['admin_muted'] = False
        self.store.update_settings({'call_initiative_enabled': False})
        await self.rt.calls.initiate()
        self.store.update_settings({'call_initiative_enabled': True, 'calls_enabled': False})
        await self.rt.calls.initiate()
        self.store.update_settings({'calls_enabled': True})
        self.rt.calls.speakers[1] = Utterance(chunks=[bytes(640)], last_frame=time.monotonic())
        await self.rt.calls.initiate()
        self.rt.calls.speakers.clear()
        self.rt.calls.enqueue(1, bytes(640))
        await self.rt.calls.initiate()
        self.rt.model.call_reply.assert_not_awaited()
        self.sdk.unmute.assert_not_awaited()

    async def test_human_speech_during_generation_discards_initiative(self):
        await self.rt.calls.activate(-123, 'test', 'group', 'talk')
        async def incoming(*args, **kwargs):
            frame = Frame(10, (np.ones(640)*5000).astype('<i2').tobytes(), Frame.Info())
            await self.rt.calls.on_update(self.sdk, StreamFrames(-123, Direction.INCOMING, Device.MICROPHONE, [frame]))
            return 'Привет!'
        self.rt.model.call_reply.side_effect = incoming
        self.ready_for_initiative()
        await self.rt.calls.initiate()
        self.rt.calls.voice.synthesize.assert_not_awaited()
        self.sdk.unmute.assert_not_awaited()

    async def test_mode_change_during_synthesis_discards_initiative(self):
        await self.rt.calls.activate(-123, 'test', 'group', 'talk')
        async def mute(*args):
            await self.rt.calls.mode('listen')
            return bytes(1920)
        self.rt.calls.voice.synthesize.side_effect = mute
        self.ready_for_initiative()
        await self.rt.calls.initiate()
        self.sdk.unmute.assert_not_awaited()
        self.assertFalse(self.rt.calls.initiating)

    async def test_leave_cancels_pending_greeting(self):
        await self.rt.calls.activate(-123, 'test', 'group', 'talk')
        started = asyncio.Event()
        async def slow(*args, **kwargs):
            started.set()
            await asyncio.Event().wait()
        self.rt.model.call_reply.side_effect = slow
        self.ready_for_initiative()
        await asyncio.wait_for(started.wait(), 2)
        await self.rt.calls.leave()
        self.assertFalse(self.rt.calls.initiating)
        self.sdk.unmute.assert_not_awaited()

    async def test_failed_initiatives_have_cooldown_and_bounded_retries(self):
        await self.rt.calls.activate(-123, 'test', 'group', 'talk')
        self.rt.model.call_reply.side_effect = RuntimeError('unavailable')
        for _ in range(2):
            self.ready_for_initiative()
            with self.assertRaises(RuntimeError):
                await self.rt.calls.initiate()
            self.assertFalse(self.rt.calls.initiative_ready())
            self.assertFalse(self.rt.calls.initiating)
        self.ready_for_initiative()
        await self.rt.calls.initiate()
        self.assertEqual(self.rt.model.call_reply.await_count, 2)

    async def test_initiative_uses_only_voice_character_and_current_call_context(self):
        from app.ai import Model
        await self.rt.model.close()
        model = Model(self.store)
        self.store.update_settings({'persona': 'TEXT_ONLY_SECRET', 'voice_persona': 'VOICE_CHARACTER'})
        self.store.message(OWNER, 'user', 'OWNER_PRIVATE_SECRET', 99, sender_id=OWNER)
        model.completion = AsyncMock(return_value={'content': 'Привет!'})
        try:
            for kind, instruction in [('greeting', 'начинаешь разговор первой'), ('pause', 'В звонке пауза')]:
                await model.call_reply([], initiative=kind)
                args = model.completion.call_args
                context = str(args.args[0])
                self.assertIn('VOICE_CHARACTER', context)
                self.assertIn(instruction, args.args[0][-2]['content'])
                self.assertEqual(args.args[0][-1]['role'], 'user')
                self.assertIn('не реплика участника', args.args[0][-1]['content'])
                self.assertNotIn('OWNER_PRIVATE_SECRET', context)
                self.assertNotIn('TEXT_ONLY_SECRET', context)
                self.assertFalse(args.kwargs['with_tools'])
                self.assertFalse(args.kwargs['owner'])
        finally:
            await model.close()

    async def test_frequent_mode_continues_after_two_replies_but_waits_for_quiet(self):
        self.store.update_settings({'call_initiative_seconds': 5, 'call_initiative_max_unanswered': 0})
        await self.rt.calls.activate(-123, 'test', 'group', 'talk')
        for _ in range(5):
            self.ready_for_initiative()
            await self.rt.calls.initiate()
            self.assertFalse(self.rt.calls.initiative_ready())
        self.assertEqual(self.rt.calls.spoken, 5)
        self.assertEqual(self.rt.calls.heard, 0)
        self.rt.calls.next_initiative = 0
        self.rt.calls.last_activity = time.monotonic() - 4
        self.assertFalse(self.rt.calls.initiative_ready())
        self.rt.calls.last_activity -= 2
        self.assertTrue(self.rt.calls.initiative_ready())
        await self.rt.calls.mode('listen')
        self.ready_for_initiative()
        await self.rt.calls.initiate()
        self.assertEqual(self.rt.calls.spoken, 5)

    async def test_unanswered_limit_can_be_changed_during_call(self):
        await self.rt.calls.activate(-123, 'test', 'group', 'talk')
        for _ in range(2):
            self.ready_for_initiative()
            await self.rt.calls.initiate()
        self.ready_for_initiative()
        self.assertFalse(self.rt.calls.initiative_ready())
        self.store.update_settings({'call_initiative_max_unanswered': 0})
        self.assertTrue(self.rt.calls.initiative_ready())
        await self.rt.calls.initiate()
        self.store.update_settings({'call_initiative_max_unanswered': 3})
        self.ready_for_initiative()
        self.assertFalse(self.rt.calls.initiative_ready())

    async def test_frequent_mode_backs_off_on_provider_errors(self):
        self.store.update_settings({'call_initiative_seconds': 5, 'call_initiative_max_unanswered': 0})
        await self.rt.calls.activate(-123, 'test', 'group', 'talk')
        self.rt.model.call_reply.side_effect = RuntimeError('unavailable')
        self.ready_for_initiative()
        with self.assertRaises(RuntimeError):
            await self.rt.calls.initiate()
        self.assertGreater(self.rt.calls.next_initiative - time.monotonic(), 29)
        await self.rt.calls.initiate()
        self.rt.model.call_reply.assert_awaited_once()

    async def test_talk_over_mode_speaks_despite_incoming_audio_and_queue(self):
        self.store.update_settings({'call_wait_for_silence': False, 'call_interrupt_on_speech': False,
                                    'call_initiative_seconds': 5, 'call_initiative_max_unanswered': 0})
        await self.rt.calls.activate(-123, 'test', 'group', 'talk')
        self.rt.calls.worker.cancel()
        await asyncio.gather(self.rt.calls.worker, return_exceptions=True)
        self.ready_for_initiative()
        async def incoming(*args, **kwargs):
            frame = Frame(10, (np.ones(640)*5000).astype('<i2').tobytes(), Frame.Info())
            await self.rt.calls.on_update(self.sdk, StreamFrames(-123, Direction.INCOMING, Device.MICROPHONE, [frame]))
            self.rt.calls.enqueue(20, bytes(32000))
            return 'А у меня ещё мысль!'
        self.rt.model.call_reply.side_effect = incoming
        self.rt.calls.voice.synthesize.return_value = bytes(1920 * 3)
        self.sdk.send_frame.side_effect = incoming
        await self.rt.calls.initiate()
        self.assertEqual(self.rt.calls.spoken, 1)
        self.assertEqual(self.sdk.send_frame.await_count, 3)
        self.assertFalse(self.rt.calls.interrupt.is_set())
        self.assertFalse(self.rt.calls.initiative_ready())
        self.rt.calls.next_initiative = 0
        self.rt.calls.last_voice_end -= 6
        self.assertTrue(self.rt.calls.initiative_ready())
        self.rt.calls.active['admin_muted'] = True
        self.assertFalse(self.rt.calls.initiative_ready())

    async def test_due_talk_over_initiative_is_not_starved_by_incoming_queue(self):
        self.store.update_settings({'call_wait_for_silence': False, 'call_interrupt_on_speech': False})
        await self.rt.calls.activate(-123, 'test', 'group', 'talk')
        self.ready_for_initiative()
        self.rt.calls.enqueue(20, bytes(32000))
        started = asyncio.Event()
        async def generate(*args, **kwargs):
            self.assertEqual(kwargs, {'initiative': 'greeting'})
            self.rt.calls.voice.transcribe.assert_not_awaited()
            started.set()
            await asyncio.Event().wait()
        self.rt.model.call_reply.side_effect = generate
        await asyncio.wait_for(started.wait(), 1)
        await self.rt.calls.leave()
