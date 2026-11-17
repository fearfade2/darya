import asyncio
from datetime import datetime,timedelta,timezone
import json
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock,Mock

from telethon import functions,types

from app.dialogs import current_dialog
from app.telegram_actions import TelegramAction
from app.voice import CallUnavailable
from telegram_fixtures import TelegramFixture,document,message,OWNER,SELF,GROUP,NOW


class ExpandedActionsTests(TelegramFixture):
    async def test_invalid_action_and_unrelated_fields_are_rejected_before_any_rpc(self):
        for args in ({'action':'delete_everything'},{'action':'profile','text':'change bio'},
                     {'action':'call','filename':'secret.session'},{'action':'poll','text':'?','options':['same','same']}):
            with self.assertRaises(ValueError):await self.execute(args)
        self.client.assert_not_awaited()

    async def test_secret_service_chat_cannot_be_read_or_used_as_a_destination(self):
        for action in ('profile','history','collect_media','send_text','stories'):
            args={'action':action,'recipient':'777000'}
            if action=='send_text':args['text']='test'
            with self.assertRaises(ValueError):await self.execute(args)
        self.client.get_entity.assert_not_awaited();self.rt.telegram.send.assert_not_awaited()

    async def test_search_discovers_real_people_and_channels_but_not_login_service(self):
        self.client.return_value=NS(users=[self.users[10],self.users[777000]],chats=[self.channel])
        result=await self.execute({'action':'search','query':'аниме'})
        self.assertEqual(len(result['users']),1);self.assertIn(10,self.actions.discovered);self.assertIn(GROUP,self.actions.discovered)
        self.assertNotIn(777000,self.actions.discovered)
        self.assertIsInstance(self.client.call_args.args[0],functions.contacts.SearchRequest)

    async def test_public_profile_reads_actual_fields_and_available_photo_description(self):
        self.client.return_value=NS(full_user=NS(about='Люблю музыку',blocked=False,birthday=None))
        self.client.get_profile_photos=AsyncMock(return_value=[])
        result=await self.execute({'action':'profile','recipient':'10'})
        self.assertEqual(result['id'],10);self.assertEqual(result['bio'],'Люблю музыку')

    async def test_collect_media_reads_selected_chat_and_ignores_ordinary_attachments(self):
        regular=document(124);regular.attributes=[]
        self.client.get_messages.return_value=[message(doc=document()),message(doc=document(125,'gif')),message(doc=regular)]
        result=await self.execute({'action':'collect_media','recipient':'10'})
        self.assertEqual(result['collected'],2)
        self.assertEqual({r['kind'] for r in result['items']},{'sticker','gif'})

    async def test_sends_owner_message_and_stores_confirmed_telegram_id(self):
        result=await self.execute({'action':'send_text','recipient':'10','text':'Привет'})
        self.assertEqual(result['telegram_id'],500)
        self.assertEqual(self.store.history(10)[-1]['text'],'Привет')
        self.assertEqual(self.store.history(10)[-1]['telegram_id'],500)

    async def test_owner_can_send_text_while_automatic_replies_are_paused(self):
        self.store.update_settings({'enabled':False})
        self.assertTrue((await self.execute({'action':'send_text','recipient':'10','text':'Привет'}))['sent'])

    async def test_disabled_destination_blocks_text_media_and_polls(self):
        self.store.execute('UPDATE contacts SET enabled=0 WHERE id=10')
        for args in ({'action':'send_text','recipient':'10','text':'Привет'},
                     {'action':'poll','recipient':'10','text':'Как дела?','options':['Хорошо','Нормально']}):
            with self.assertRaises(ValueError):await self.execute(args)
        self.client.send_file.assert_not_awaited();self.rt.telegram.send.assert_not_awaited()

    async def test_poll_uses_real_telegram_poll_and_answer_bytes(self):
        result=await self.execute({'action':'poll','recipient':'10','text':'Музыка?','options':['Джаз','Рок'],'multiple':True})
        self.assertTrue(result['sent'])
        media=self.client.send_file.call_args.args[1]
        self.assertIsInstance(media,types.InputMediaPoll)
        self.assertEqual(media.poll.question.text,'Музыка?')
        self.assertEqual([a.option for a in media.poll.answers],[b'\x00',b'\x01'])
        self.assertTrue(media.poll.multiple_choice)

    async def test_quiz_requires_a_valid_correct_answer(self):
        for args in ({'action':'poll','text':'?','options':['A','B'],'quiz':True},
            {'action':'poll','text':'?','options':['A','B'],'quiz':True,'correct_option':3}):
            with self.assertRaises(ValueError):await self.execute(args)
        await self.execute({'action':'poll','recipient':'10','text':'?','options':['A','B'],'quiz':True,'correct_option':1})
        self.assertEqual(self.client.send_file.call_args.args[1].correct_answers,[1])

    async def test_vote_uses_actual_poll_options_and_rejects_missing_or_multiple_choice(self):
        poll=types.Poll(1,types.TextWithEntities('?',[]),[types.PollAnswer(types.TextWithEntities('A',[]),b'A'),types.PollAnswer(types.TextWithEntities('B',[]),b'B')],hash=0)
        m=message();m.media=types.MessageMediaPoll(poll,types.PollResults(total_voters=0))
        self.client.get_messages.return_value=m
        await self.execute({'action':'vote_poll','recipient':'10','message_id':500,'option_indices':[1]})
        self.assertEqual(self.client.call_args.args[0].options,[b'B'])
        for options in ([2],[0,1]):
            with self.assertRaises(ValueError):await self.execute({'action':'vote_poll','recipient':'10','message_id':500,'option_indices':options})

    async def test_cannot_close_another_persons_poll(self):
        poll=types.Poll(1,types.TextWithEntities('?',[]),[types.PollAnswer(types.TextWithEntities('A',[]),b'A')],hash=0)
        m=message();m.media=types.MessageMediaPoll(poll,types.PollResults())
        self.client.get_messages.return_value=m
        with self.assertRaises(ValueError):await self.execute({'action':'close_poll','recipient':'10','message_id':500})
        self.client.edit_message.assert_not_awaited()

    async def test_reaction_can_be_added_and_removed_on_a_real_message(self):
        self.client.get_messages.return_value=message()
        await self.execute({'action':'reaction','recipient':'10','message_id':500,'emoji':'❤️'})
        self.assertEqual(self.client.call_args.args[0].reaction[0].emoticon,'❤️')
        await self.execute({'action':'reaction','recipient':'10','message_id':500,'remove':True})
        self.assertEqual(self.client.call_args.args[0].reaction,[])

    async def test_draft_is_saved_without_sending_a_message(self):
        await self.execute({'action':'draft','recipient':'10','text':'Черновик','message_id':500})
        request=self.client.call_args.args[0]
        self.assertIsInstance(request,functions.messages.SaveDraftRequest);self.assertEqual(request.message,'Черновик')
        self.rt.telegram.send.assert_not_awaited();self.assertEqual(self.store.history(10),[])

    async def test_native_scheduled_message_is_not_recorded_as_already_delivered(self):
        when=(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat()
        result=await self.execute({'action':'schedule','recipient':'10','text':'Позже','when':when})
        self.assertTrue(result['scheduled']);self.assertEqual(self.store.history(10),[])
        self.assertIn('schedule',self.client.send_message.call_args.kwargs)

    async def test_autonomous_scheduled_message_uses_server_queue_and_does_not_send_immediately(self):
        when=(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat()
        token=current_dialog.set(self.store.dialog_scope(10))
        try:result=await self.execute({'action':'schedule','text':'Позже','when':when},autonomous=True,focus=10)
        finally:current_dialog.reset(token)
        self.assertTrue(result['scheduled']);self.assertEqual(self.store.rows('SELECT text,status FROM tasks')[0],{'text':'Позже','status':'pending'})
        self.client.send_message.assert_not_awaited();self.assertEqual(self.store.history(10),[])

    async def test_scheduled_message_management_rejects_unknown_ids(self):
        self.client.return_value=NS(messages=[message(id=55)])
        with self.assertRaises(ValueError):await self.execute({'action':'cancel_scheduled','recipient':'10','message_ids':[999]})
        self.assertTrue(all(not isinstance(call.args[0],functions.messages.DeleteScheduledMessagesRequest) for call in self.client.call_args_list))

    async def test_folder_update_preserves_existing_peers_and_options(self):
        existing=types.DialogFilter(2,types.TextWithEntities('Старое',[]),[],[types.InputPeerUser(20,1)],[],groups=True)
        self.client.side_effect=lambda request:NS(filters=[existing]) if isinstance(request,functions.messages.GetDialogFiltersRequest) else True
        await self.execute({'action':'folder','folder_id':2,'title':'Друзья'})
        saved=self.client.call_args.args[0].filter
        self.assertEqual(saved.title.text,'Друзья');self.assertEqual(saved.include_peers[0].user_id,20);self.assertTrue(saved.groups)

    async def test_folder_rejects_special_ids_and_unknown_flags(self):
        for args in ({'action':'delete_folder','folder_id':0}, {'action':'folder','folder_id':2,'title':'Test','folder_options':{'take_sessions':True}}):
            with self.assertRaises(ValueError):await self.execute(args)
        self.client.assert_not_awaited()

    async def test_new_channel_uses_telegram_create_channel_and_reports_actual_id(self):
        self.client.return_value=NS(chats=[self.channel])
        result=await self.execute({'action':'create_channel','title':'Музыка','description':'Любимые треки'})
        self.assertTrue(result['created']);self.assertEqual(result['chat_id'],GROUP)
        request=self.client.call_args.args[0];self.assertIsInstance(request,functions.channels.CreateChannelRequest);self.assertTrue(request.broadcast)

    async def test_join_and_leave_channel_use_real_telegram_requests(self):
        await self.execute({'action':'join','recipient':'@group_test'})
        self.assertIsInstance(self.client.call_args.args[0],functions.channels.JoinChannelRequest)
        await self.execute({'action':'leave','recipient':'@group_test'})
        self.assertIsInstance(self.client.call_args.args[0],functions.channels.LeaveChannelRequest)

    async def test_stories_read_and_react_only_through_telegram(self):
        self.client.side_effect=lambda request:NS(stories=NS(stories=[])) if isinstance(request,functions.stories.GetPeerStoriesRequest) else True
        self.assertEqual((await self.execute({'action':'stories','recipient':'10'}))['stories'],[])
        await self.execute({'action':'story_reaction','recipient':'10','story_id':1,'emoji':'❤️'})
        self.assertIsInstance(self.client.call_args.args[0],functions.stories.SendReactionRequest)

    async def test_story_publication_uses_owner_media_and_contacts_audience(self):
        from PIL import Image
        root=self.store.directory/'media';root.mkdir(exist_ok=True);Image.new('RGB',(10,10)).save(root/'story.jpg')
        self.client.upload_file.return_value=types.InputFile(1,1,'photo.jpg','hash')
        self.client.return_value=NS(updates=[NS(story=NS(id=12))])
        result=await self.execute({'action':'publish_story','filename':'story.jpg','text':'Уютный вечер'})
        request=self.client.call_args.args[0]
        self.assertIsInstance(request,functions.stories.SendStoryRequest)
        self.assertIsInstance(request.privacy_rules[0],types.InputPrivacyValueAllowContacts)
        self.assertEqual(result['story_ids'],[12])

    async def test_private_artifacts_symlinks_and_external_paths_cannot_be_published(self):
        root=self.store.directory/'media';root.mkdir(exist_ok=True)
        (root/'private.jpg').write_bytes(b'image')
        self.store.execute('INSERT INTO jobs(contact_id,task,created,updated,artifact) VALUES (10,?,0,0,?)',('private','private.jpg'))
        (root/'link.jpg').symlink_to(root/'private.jpg')
        for filename in ('private.jpg','link.jpg','../secrets.json','/tmp/private.jpg'):
            with self.assertRaises(ValueError):await self.execute({'action':'publish_story','filename':filename})
        self.client.upload_file.assert_not_awaited()

    async def test_account_changes_reuse_existing_validation_and_recording(self):
        await self.execute({'action':'dialog','recipient':'10','operation':{'action':'archive','enabled':True}})
        self.assertIsInstance(self.client.call_args.args[0],functions.folders.EditPeerFoldersRequest)
        await self.execute({'action':'profile_settings','operation':{'action':'birthday','day':1,'month':5}})
        self.assertIsInstance(self.client.call_args.args[0],functions.account.UpdateBirthdayRequest)

    async def test_group_actions_do_not_allow_granting_control(self):
        self.rt.group_admin.execute=AsyncMock(return_value={'changed':True})
        with self.assertRaises(ValueError):await self.execute({'action':'group','recipient':str(GROUP),'operation':{'action':'grant_control','user':'20'}})
        self.rt.group_admin.execute.assert_not_awaited()
        await self.execute({'action':'group','recipient':str(GROUP),'operation':{'action':'title','text':'Новое название'}})
        self.rt.group_admin.execute.assert_awaited_once()

    async def test_delegated_user_cannot_enable_or_widen_global_autonomy(self):
        self.store.execute('INSERT INTO controller_permissions VALUES (?,?,0,1,0)',(OWNER,20))
        self.store.message(20,'user','включи самостоятельность',1,sender_id=20)
        for values in ({'telegram_autonomy_enabled':True},{'telegram_autonomy_instructions':'звони всем'},{'telegram_auto_answer_calls':True}):
            with self.assertRaises(ValueError):await self.rt.control.action(20,1,'configure_darya',{'settings':values},trusted=True)
        self.assertFalse(self.store.settings()['telegram_autonomy_enabled'])

    async def test_call_and_autonomy_commands_parse_without_provider(self):
        self.assertEqual(self.rt.control.parse(['позвони @olga_test']),[{'name':'call_user','args':{'recipient':'@olga_test'}}])
        self.assertEqual(self.rt.control.parse(['включи самостоятельность в Telegram']),[{'name':'configure_darya','args':{'settings':{'telegram_autonomy_enabled':True}}}])
        self.assertEqual(self.rt.control.parse(['выключи самостоятельность']),[{'name':'configure_darya','args':{'settings':{'telegram_autonomy_enabled':False}}}])


class OutgoingCallTests(TelegramFixture):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.sdk=NS(play=AsyncMock(),_p2p_configs={})
        self.rt.calls.start=AsyncMock(return_value=self.sdk)
        self.rt.calls.voice.prepare=AsyncMock()
        self.rt.calls.media=Mock(return_value='audio')
        async def activate(chat_id,name,kind,mode):self.rt.calls.active={'chat_id':chat_id,'name':name,'kind':kind,'mode':mode}
        self.rt.calls.activate=AsyncMock(side_effect=activate)
        self.rt.calls.status=Mock(return_value={'connected':True,'name':'Оля','mode':'talk'})
        self.rt.calls.cleanup_native=AsyncMock()

    async def test_originates_actual_private_call_without_pending_incoming(self):
        result=await self.rt.calls.dial('10','talk',lambda:None)
        self.assertTrue(result['connected']);self.assertEqual(self.sdk.play.call_args.args[0],10)
        self.assertTrue(self.rt.calls.active['outgoing'])

    async def test_does_not_replace_an_existing_call_or_accept_an_incoming_by_accident(self):
        self.rt.calls.active={'chat_id':20}
        with self.assertRaises(CallUnavailable):await self.rt.calls.dial('10','talk',lambda:None)
        self.rt.calls.active=None;self.rt.calls.pending[10]=0
        with self.assertRaises(CallUnavailable):await self.rt.calls.dial('10','talk',lambda:None)
        self.sdk.play.assert_not_awaited()

    async def test_per_contact_call_switch_bots_self_and_login_service_are_rejected(self):
        self.store.execute('UPDATE contacts SET calls_allowed=0 WHERE id=10')
        with self.assertRaises(CallUnavailable):await self.rt.calls.dial('10','talk',lambda:None)
        self.store.execute('UPDATE contacts SET calls_allowed=1 WHERE id=10');self.users[10].bot=True
        for target in ('10',str(SELF)):
            with self.assertRaises(CallUnavailable):await self.rt.calls.dial(target,'talk',lambda:None)
        with self.assertRaises(ValueError):await self.rt.calls.dial('777000','talk',lambda:None)
        self.sdk.play.assert_not_awaited()

    async def test_revoked_permission_during_voice_preparation_stops_before_dial(self):
        async def prepare():self.store.execute('UPDATE contacts SET calls_allowed=0 WHERE id=10')
        self.rt.calls.voice.prepare.side_effect=prepare
        with self.assertRaises(CallUnavailable):await self.rt.calls.dial('10','talk',lambda:None)
        self.sdk.play.assert_not_awaited()

    async def test_failed_dial_cleans_up_without_a_second_attempt(self):
        self.sdk.play.side_effect=ConnectionError('lost acknowledgment')
        with self.assertRaises(ConnectionError):await self.rt.calls.dial('10','talk',lambda:None)
        self.sdk.play.assert_awaited_once();self.rt.calls.cleanup_native.assert_awaited_once_with(10)
        self.assertIsNone(self.rt.calls.active)
