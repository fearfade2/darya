import asyncio
import json
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

from telethon import errors, functions, types

from app.group_admin import AdminOperation, OPERATIONS, ADMIN_RIGHTS, parse_command
from app.ai import ModelUnavailable
from app.runtime import Runtime
from app.store import Store

OWNER=1000000001
SELF=1000000002
CHAT=-1000000000123


class GroupAdminTests(unittest.IsolatedAsyncioTestCase):
    async def test_group_reader_drawing_request_returns_to_conversation(self):
        await self.grant_group_reader()
        self.assertFalse(await self.handle(self.event('Даша нарисуй кота',user=20,id=339)))
        self.rt.model.completion.assert_not_awaited()
        self.client.send_message.assert_not_awaited()

    async def test_group_reader_semantic_drawing_does_not_require_global_authority(self):
        await self.grant_group_reader()
        self.rt.model.completion.return_value={'content':json.dumps({'actions':[{'name':'generate_image','args':{'prompt':'кот'}}]})}
        self.assertFalse(await self.handle(self.event('хочу новую картинку с котом, сделаешь?',user=20,id=340)))
        self.client.send_message.assert_not_awaited()

    async def grant_group_reader(self):
        await self.rt.authority.execute(OWNER,{'action':'grant_control','user':'20','scope':'here'},lambda:None,chat_id=CHAT)

    def joined(self,users=None):
        return types.MessageService(89,types.PeerChannel(123),datetime.now(timezone.utc),
            action=types.MessageActionChatAddUser(users) if users is not None else types.MessageActionChatJoinedByLink(20),
            from_id=types.PeerUser(20 if users is not None else 10))

    async def test_group_reader_identifies_reply_to_join_without_account_authority(self):
        await self.grant_group_reader()
        event=self.event('Даша кто это',user=20,id=310)
        event.get_reply_message.return_value=self.joined(users=[10])
        self.client.get_entity.side_effect=lambda value:types.User(int(value),access_hash=123,first_name='Giti',last_name='Ghaemi',username='giti_test',phone='PRIVATE_PHONE')
        self.rt.account.execute=AsyncMock()
        self.assertTrue(await self.handle(event))
        response=self.client.send_message.call_args.args[1]
        self.assertIn('Giti Ghaemi',response)
        self.assertIn('Telegram ID: 10',response)
        self.assertNotIn('PRIVATE_PHONE',response)
        self.assertNotIn('разрешение действует',response)
        self.rt.model.completion.assert_not_awaited()
        self.rt.account.execute.assert_not_awaited()
        self.assertFalse(__import__('app.authority',fromlist=['authorized']).authorized(self.store,20))

    async def test_group_reader_finds_recent_join_instead_of_last_speaker(self):
        await self.grant_group_reader()
        event=self.event('Даша кто это зашел',user=20,id=311,reply_user=None)
        self.client.get_messages.side_effect=None
        self.client.get_messages.return_value=[self.message(91,20,'hello'),self.joined()]
        self.assertTrue(await self.handle(event))
        self.assertIn('Telegram ID: 10',self.client.send_message.call_args.args[1])
        self.assertEqual(self.client.get_messages.call_args.kwargs['max_id'],311)
        self.rt.model.completion.assert_not_awaited()

    async def test_group_reader_matches_cyrillic_name_to_known_member(self):
        await self.grant_group_reader()
        self.store.group_message(CHAT,88,'hello',sender_id=10,sender_name='Giti Ghaemi')
        self.client.get_messages.side_effect=None
        self.client.get_messages.return_value=[]
        self.assertTrue(await self.handle(self.event('Кто такой гити гхаеми',user=20,id=312,reply_user=None)))
        self.assertIn('Telegram ID: 10',self.client.send_message.call_args.args[1])
        self.rt.model.completion.assert_not_awaited()

    async def test_group_reader_public_profile_still_rejects_nonmember(self):
        await self.grant_group_reader()
        self.client.get_permissions.side_effect=errors.UserNotParticipantError(None)
        self.assertTrue(await self.handle(self.event('покажи профиль @outsider',user=20,id=313,reply_user=None)))
        self.assertIn('не является участником',self.client.send_message.call_args.args[1])
        self.assertFalse(any(isinstance(call.args[0],functions.users.GetFullUserRequest) for call in self.client.await_args_list))

    async def test_group_reader_identifies_former_member_from_join_event(self):
        await self.grant_group_reader()
        event=self.event('Даша кто это',user=20,id=330)
        event.get_reply_message.return_value=self.joined(users=[10])
        self.client.get_permissions.side_effect=errors.UserNotParticipantError(None)
        self.assertTrue(await self.handle(event))
        response=self.client.send_message.call_args.args[1]
        self.assertIn('Telegram ID: 10',response)
        self.assertIn('уже не состоит в группе',response)
        self.assertFalse(any(isinstance(call.args[0],functions.users.GetFullUserRequest) for call in self.client.await_args_list))

    async def test_group_reader_identifies_former_member_by_name_from_group_history(self):
        await self.grant_group_reader()
        self.store.group_message(CHAT,88,'hello',sender_id=10,sender_name='Giti Ghaemi')
        self.client.get_messages.side_effect=None
        self.client.get_messages.return_value=[]
        self.client.get_permissions.side_effect=errors.UserNotParticipantError(None)
        self.assertTrue(await self.handle(self.event('Кто такой гити гхаеми',user=20,id=331,reply_user=None)))
        response=self.client.send_message.call_args.args[1]
        self.assertIn('Telegram ID: 10',response)
        self.assertIn('уже не состоит в группе',response)
        self.assertFalse(any(isinstance(call.args[0],functions.users.GetFullUserRequest) for call in self.client.await_args_list))

    async def test_group_reader_permission_revoked_during_lookup_prevents_profile_read(self):
        await self.grant_group_reader()
        async def revoke(value):
            self.store.execute('UPDATE controller_permissions SET enabled=0 WHERE user_id=20')
            return types.User(10,access_hash=123,first_name='Giti')
        self.client.get_entity.side_effect=revoke
        self.assertTrue(await self.handle(self.event('Даша кто это',user=20,id=314)))
        self.assertFalse(any(isinstance(call.args[0],functions.users.GetFullUserRequest) for call in self.client.await_args_list))
        self.client.send_message.assert_not_awaited()

    async def test_group_reader_semantic_profile_request_uses_member_reader(self):
        await self.grant_group_reader()
        self.rt.model.completion.return_value={'content':json.dumps({'actions':[{'name':'get_telegram_profile','args':{'recipient':'10'}}]})}
        self.rt.account.execute=AsyncMock()
        self.assertTrue(await self.handle(self.event('расскажи об этом участнике',user=20,id=315)))
        self.assertIn('Telegram ID: 10',self.client.send_message.call_args.args[1])
        self.rt.account.execute.assert_not_awaited()

    async def test_group_reader_rejects_join_reply_from_another_group(self):
        await self.grant_group_reader()
        event=self.event('Даша кто это',user=20,id=316)
        reply=self.joined();reply.peer_id=types.PeerChannel(999)
        event.get_reply_message.return_value=reply
        self.client.reset_mock()
        self.assertTrue(await self.handle(event))
        self.assertIn('в этой группе',self.client.send_message.call_args.args[1])
        self.client.get_entity.assert_not_awaited()

    async def test_reactions_do_not_continue_an_administrative_clarification(self):
        await self.grant_group_reader()
        self.store.execute('INSERT INTO controller_group_pending VALUES (?,?,?,?,?,?,?,?,?)',
            (CHAT,0,20,OWNER,0,300,None,'замуть его',time.time()))
        for index,text in enumerate(['Даша ди нахуй','Чт бля','то плохое']):
            self.assertFalse(await self.handle(self.event(text,user=20,id=320+index,reply_user=None)))
        self.rt.model.completion.assert_not_awaited()
        self.client.send_message.assert_not_awaited()

    async def test_chat_reactions_do_not_become_failed_commands_during_provider_outage(self):
        self.rt.model.completion.side_effect=ModelUnavailable('API модели вернул код 503')
        for index,text in enumerate(['о','вхахах пиздец','ахахаха','ты кто','Даша, кто ты?']):
            self.assertFalse(await self.handle(self.event(text,id=300+index,reply_user=None)))
        self.rt.model.completion.assert_not_awaited()
        self.client.send_message.assert_not_awaited()
        self.assertEqual(self.store.rows('SELECT * FROM group_admin_actions'),[])

    async def test_ordinary_gif_command_stays_in_current_forum_topic(self):
        self.rt.model.completion.return_value={'content':json.dumps({'actions':[{'name':'send_gif','args':{'query':'кролик'}}]})}
        self.rt.telegram_media.select=AsyncMock(return_value='gif:123')
        self.rt.telegram_media.send=AsyncMock(return_value={'sent':True})
        event=self.event('пришли сюда анимацию с кроликом',reply_user=None)
        event.message.reply_to=types.MessageReplyHeader(reply_to_msg_id=38,reply_to_top_id=38,forum_topic=True)
        self.assertTrue(await self.handle(event))
        self.rt.telegram_media.send.assert_awaited_once()
        self.assertEqual(self.rt.telegram_media.send.call_args.kwargs['reply_to'],38)
        self.assertEqual(self.rt.telegram_media.send.call_args.args[0],CHAT)

    async def test_manual_gif_stays_in_current_forum_topic(self):
        self.rt.model.completion.return_value={'content':json.dumps({'actions':[{'name':'telegram_action','args':{'action':'send_gif','query':'кролик'}}]})}
        self.client.get_entity=AsyncMock(return_value=self.chat)
        self.rt.telegram_media.select=AsyncMock(return_value='gif:123')
        self.rt.telegram_media.send=AsyncMock(return_value={'sent':True})
        event=self.event('отправь сюда гифку с кроликом',reply_user=None)
        event.message.reply_to=types.MessageReplyHeader(reply_to_msg_id=38,reply_to_top_id=38,forum_topic=True)
        self.assertTrue(await self.handle(event))
        self.rt.telegram_media.send.assert_awaited_once()
        self.assertEqual(self.rt.telegram_media.send.call_args.kwargs['reply_to'],38)
        self.assertEqual(self.rt.telegram_media.send.call_args.args[0],CHAT)

    async def test_real_photo_from_group_command_reaches_avatar_executor(self):
        from test_owner_control import photo_bytes
        self.rt.model.completion.return_value={'content':'{"actions":[{"name":"set_profile_photo","args":{}}]}'}
        self.tg.set_profile_photo=AsyncMock()
        self.client.download_media.return_value=photo_bytes()
        event=self.event('поставь эту фотографию на свой аватар',reply_user=None)
        event.message.media=types.MessageMediaPhoto(photo=types.Photo(123,456,b'ref',datetime.now(timezone.utc),[],1))
        self.assertTrue(await self.handle(event))
        self.tg.set_profile_photo.assert_awaited_once()
        self.assertEqual(self.store.rows("SELECT status FROM owner_actions WHERE action='set_profile_photo'")[0]['status'],'sent')

    async def test_group_only_delegate_cannot_change_global_setting_even_in_mixed_plan(self):
        await self.rt.authority.execute(OWNER,{'action':'grant_control','user':'10','scope':'here'},lambda:None,chat_id=CHAT)
        self.rt.model.completion.return_value={'content':json.dumps({'actions':[
            {'action':'mute','user':'reply','minutes':3},
            {'name':'configure_darya','args':{'settings':{'typing_cps':60}}}]})}
        before=self.store.settings()['typing_cps'];self.client.reset_mock()
        self.assertTrue(await self.handle(self.event('замуть его и поменяй скорость печати',user=10,reply_user=20)))
        self.assertEqual(self.store.settings()['typing_cps'],before)
        self.assertFalse(any(isinstance(c.args[0],functions.channels.EditBannedRequest) for c in self.client.call_args_list))
        data=json.loads(self.rt.model.completion.call_args.args[0][1]['content'])
        self.assertEqual(data['state']['settings'],{})

    async def test_flexible_global_setting_works_from_group_and_duplicate_never_repeats(self):
        self.rt.model.completion.return_value={'content':json.dumps({'actions':[{'name':'configure_darya','args':{'settings':{'typing_cps':75}}}]})}
        event=self.event('давай увеличим скорость печати до 75 символов в секунду',reply_user=None)
        self.assertTrue(await self.handle(event))
        self.assertEqual(self.store.settings()['typing_cps'],75)
        self.assertTrue(await self.handle(event))
        self.rt.model.completion.assert_awaited_once()
        self.assertIsNone(__import__('app.control',fromlist=['group_request']).group_request.get())
        with self.assertRaises(ValueError):await self.rt.control.action(CHAT,event.id,'owner_settings',{},trusted=True)

    async def test_settings_and_moderation_execute_as_one_validated_plan(self):
        self.rt.model.completion.return_value={'content':json.dumps({'actions':[
            {'name':'configure_darya','args':{'settings':{'typing_cps':60}}},
            {'name':'manage_group','args':{'group':str(CHAT),'operation':{'action':'mute','user':'reply','minutes':3}}}]})}
        self.assertTrue(await self.handle(self.event('печатай быстрее и замуть его на три минуты')))
        self.assertEqual(self.store.settings()['typing_cps'],60)
        calls=[c.args[0] for c in self.client.call_args_list if isinstance(c.args[0],functions.channels.EditBannedRequest)]
        self.assertEqual(len(calls),1);self.assertEqual(calls[0].participant.id,10)

    async def test_group_publication_setting_selects_current_group(self):
        self.rt.model.completion.return_value={'content':json.dumps({'actions':[{'name':'configure_publications','args':{'settings':{'daily_limit':500}}}]})}
        self.rt.publications.configure=AsyncMock(return_value={'text':'Изменено'})
        self.assertTrue(await self.handle(self.event('подними потолок постов здесь до 500',reply_user=None)))
        self.assertEqual(self.rt.publications.configure.call_args.args[0],{'destination':str(CHAT),'settings':{'daily_limit':500}})

    async def test_invalid_later_group_task_prevents_earlier_global_change(self):
        self.rt.model.completion.return_value={'content':json.dumps({'actions':[
            {'name':'configure_darya','args':{'settings':{'typing_cps':60}}},
            {'name':'schedule_message','args':{'text':'x','when':'invalid'}}]})}
        before=self.store.settings()['typing_cps']
        self.assertTrue(await self.handle(self.event('поменяй скорость и напомни позже')))
        self.assertEqual(self.store.settings()['typing_cps'],before)
        self.assertEqual(self.store.rows('SELECT * FROM tasks'),[])

    async def test_group_reminder_retains_author_and_forum_topic(self):
        self.store.execute('UPDATE contacts SET proactive=1 WHERE id=?',(CHAT,))
        self.rt.model.completion.return_value={'content':json.dumps({'actions':[{'name':'schedule_message','args':{'text':'Проверка','when':datetime.fromtimestamp(time.time()+3600,timezone.utc).isoformat()}}]})}
        event=self.event('напомни об этом в этой ветке вечером',reply_user=None)
        event.message.reply_to=types.MessageReplyHeader(reply_to_msg_id=38,reply_to_top_id=38,forum_topic=True)
        self.assertTrue(await self.handle(event))
        task=self.store.rows('SELECT topic_id,context_sender_id FROM tasks')[0]
        self.assertEqual(task,{'topic_id':38,'context_sender_id':OWNER})

    async def test_current_topic_publication_ban_runs_without_model_or_admin_permissions(self):
        event=self.event('дура сюда ничего не публикуй',reply_user=None)
        event.message.reply_to=types.MessageReplyHeader(reply_to_msg_id=38,reply_to_top_id=38,forum_topic=True)
        self.rt.publications.configure_topic=AsyncMock(return_value={'text':'Публикации в этой теме запрещены.'})
        self.rt.model.completion.side_effect=AssertionError('Clear topic ban must not need a model')
        self.assertTrue(await self.handle(event))
        self.rt.publications.configure_topic.assert_awaited_once()
        call=self.rt.publications.configure_topic.call_args
        self.assertEqual(call.args[:3],(str(CHAT),38,False))
        self.assertEqual(call.kwargs['actor_id'],OWNER)
        self.client.get_permissions.assert_not_awaited()
        self.assertTrue(await self.handle(event))
        self.rt.publications.configure_topic.assert_awaited_once()

    async def test_topic_publication_parser_does_not_execute_quotes_or_mute_conversation(self):
        for text in ['Даша, не публикуй здесь','в эту тему не публикуй','сюда ничего не пости']:
            self.assertEqual(parse_command(text)[0].action,'topic_publications')
            self.assertFalse(parse_command(text)[0].enabled)
        self.assertTrue(parse_command('снова публикуй сюда')[0].enabled)
        self.assertIsNone(parse_command('мне сказали «сюда ничего не публикуй»'))
        self.assertIsNone(parse_command('не разговаривай здесь'))

    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=Store(Path(self.tmp.name))
        self.store.update_settings({'owner_id':OWNER});self.rt=Runtime(self.store)
        self.tg=self.rt.telegram;self.tg.ready=Mock(return_value=True);self.tg.account_id=SELF
        self.client=AsyncMock();self.tg.client=self.client;self.admin=self.rt.group_admin
        self.chat=types.Channel(123,'Test',types.ChatPhotoEmpty(),datetime.now(timezone.utc),megagroup=True)
        self.store.upsert_contact(CHAT,'Test',kind='group')
        self.me=NS(is_admin=True,is_creator=False,is_chat=False,participant=NS(admin_rights=types.ChatAdminRights(**{r:True for r in ADMIN_RIGHTS})))
        self.member=NS(is_admin=False,is_creator=False,is_chat=False,participant=NS(banned_rights=None))
        self.client.get_permissions.side_effect=lambda chat,user: self.me if user==SELF else self.member
        self.client.get_entity.side_effect=self.entity
        self.client.side_effect=self.rpc
        self.client.get_messages.side_effect=lambda chat,ids: self.message(ids,10,'original')
        self.client.download_media.return_value=b'photo';self.client.upload_file.return_value=types.InputFile(1,1,'photo.jpg','a')
        self.admin.groups.resolve=AsyncMock(return_value=self.chat)
        self.rt.model.completion=AsyncMock(return_value={'content':'{"actions":[]}'})

    async def asyncTearDown(self):
        self.tg.client=None;await self.rt.stop();self.store.close();self.tmp.cleanup()

    async def entity(self,value):
        if isinstance(value,types.Channel):return value
        if isinstance(value,types.User):return value
        return types.User(int(value) if isinstance(value,int) or str(value).isdigit() else 10,first_name='User')

    async def rpc(self,request):
        if isinstance(request,(functions.channels.GetFullChannelRequest,functions.messages.GetFullChatRequest)):
            return NS(full_chat=NS(call=types.InputGroupCall(999,123)))
        if isinstance(request,functions.messages.ExportChatInviteRequest):return NS(link='https://t.me/+fixture')
        if isinstance(request,functions.messages.GetExportedChatInvitesRequest):return NS(invites=[NS(link='https://t.me/+fixture',revoked=False)])
        return NS(offset=0)

    def message(self,id,user,text):
        return types.Message(id,types.PeerChannel(123),datetime.now(timezone.utc),text,from_id=types.PeerUser(user))

    def event(self,text,user=OWNER,id=100,reply_user=10):
        message=self.message(id,user,text)
        reply=self.message(90,reply_user,'target') if reply_user else None
        return NS(id=id,chat_id=CHAT,raw_text=text,message=message,is_private=False,is_group=True,
                  is_reply=bool(reply),mentioned=True,get_chat=AsyncMock(return_value=self.chat),
                  get_reply_message=AsyncMock(return_value=reply),get_sender=AsyncMock(return_value=types.User(user,first_name='Owner')))

    async def handle(self,event):return await self.admin.handle(event,await event.get_sender())

    async def execute(self,action,**kwargs):
        return await self.admin.execute(self.chat,AdminOperation(action=action,**kwargs),Mock(),reply=self.message(90,10,'target'),topic_id=8)

    async def test_owner_reply_ban_and_duplicate_never_repeats(self):
        event=self.event('Даша, забань этого')
        self.assertTrue(await self.handle(event));self.assertTrue(await self.handle(event))
        requests=[c.args[0] for c in self.client.await_args_list]
        self.assertEqual(len(requests),1);self.assertIsInstance(requests[0],functions.channels.EditBannedRequest)
        self.assertTrue(requests[0].banned_rights.view_messages)
        self.client.send_message.assert_awaited_once()
        self.assertEqual(self.store.rows('SELECT status FROM group_admin_actions')[0]['status'],'sent')

    async def test_alias_alone_is_conversation_without_pending_command(self):
        self.assertFalse(await self.handle(self.event('даша',reply_user=None)))
        self.rt.model.completion.assert_not_awaited()
        self.client.send_message.assert_not_awaited()
        self.assertIsNone(self.admin.pending(CHAT,0))

    async def test_explicit_moderation_bypasses_old_pending_during_model_outage(self):
        for index,(text,reply_user) in enumerate([('размуть',10),('замуть @example на 5 минут',None)]):
            self.store.execute('INSERT OR REPLACE INTO group_admin_pending VALUES (?,?,?,?,?,?,?,?)',
                (CHAT,0,OWNER,self.store.dialog_scope(CHAT,0)[2],80,None,'верни его',time.time()))
            self.rt.model.completion.side_effect=ModelUnavailable('API модели вернул код 503')
            event=self.event(text,id=100+index,reply_user=reply_user)
            await self.handle(event);await self.handle(event)
            self.rt.model.completion.assert_not_awaited()
            self.assertIsNone(self.admin.pending(CHAT,0))
        requests=[c.args[0] for c in self.client.await_args_list]
        self.assertEqual(len(requests),2)
        self.assertFalse(requests[0].banned_rights.send_messages)
        self.assertTrue(requests[1].banned_rights.send_messages)

    async def test_ambiguous_bot_reply_keeps_context_and_hides_provider_error(self):
        self.store.execute('INSERT INTO group_admin_pending VALUES (?,?,?,?,?,?,?,?)',
            (CHAT,0,OWNER,self.store.dialog_scope(CHAT,0)[2],80,None,'верни его',time.time()))
        self.rt.model.completion.side_effect=ModelUnavailable('API модели вернул код 503. RAW_PROVIDER_DETAILS')
        await self.handle(self.event('размуть его',reply_user=SELF))
        self.rt.model.completion.assert_awaited_once()
        self.client.assert_not_awaited()
        answer=self.client.send_message.call_args.args[1]
        self.assertIn('Сервис модели временно не отвечает.',answer)
        self.assertNotIn('503',answer);self.assertNotIn('RAW_PROVIDER_DETAILS',answer)
        self.assertIsNotNone(self.admin.pending(CHAT,0))
        self.assertIn('RAW_PROVIDER_DETAILS',self.store.rows('SELECT text FROM events ORDER BY id DESC LIMIT 1')[0]['text'])

    async def test_attention_calls_bypass_admin_model_and_never_post_its_errors(self):
        self.tg.account_username='darya_bot'
        self.rt.model.completion.side_effect=ValueError('API модели вернул код 502')
        for text in ['Даша але','ты тут?','@darya_bot','Дашка, ау']:
            self.assertFalse(await self.handle(self.event(text,reply_user=None)))
        self.rt.model.completion.assert_not_awaited()
        self.client.send_message.assert_not_awaited()

    async def test_conversation_does_not_send_classifier_clarification(self):
        self.rt.model.completion.return_value={'content':json.dumps({'intent':'conversation','actions':[],
            'clarification':'Что нужно сделать?'})}
        self.assertFalse(await self.handle(self.event('Как дела?',reply_user=None)))
        self.client.send_message.assert_not_awaited()
        self.assertIsNone(self.admin.pending(CHAT,0))

    async def test_owner_questions_reach_conversation_even_when_command_provider_is_down(self):
        self.rt.model.completion.side_effect=ModelUnavailable('API модели вернул код 503')
        for text in ['Даша, как дела?', 'Почему ты не отвечаешь?', 'Как подключить API в Python?',
                     'Даша, объясни почему такое бывает']:
            self.assertFalse(await self.handle(self.event(text,reply_user=None)))
        self.rt.model.completion.assert_not_awaited()
        self.client.assert_not_awaited()
        self.client.send_message.assert_not_awaited()

    async def test_all_forms_of_which_question_reach_conversation_without_planning(self):
        self.rt.model.completion.side_effect=ModelUnavailable('API модели вернул код 503')
        for index,text in enumerate(['даша какая погода в саратове','Какие новости?',
                                     'Какую книгу посоветуешь?','Какого цвета небо?',
                                     'Каким языком написано приложение?','Какой сегодня день?']):
            self.assertFalse(await self.handle(self.event(text,id=400+index,reply_user=None)))
        self.rt.model.completion.assert_not_awaited()
        self.client.get_permissions.assert_not_awaited()
        self.client.send_message.assert_not_awaited()

    async def test_explicit_group_search_prepares_answer_and_duplicate_does_not_send_again(self):
        self.rt.research.search=AsyncMock(return_value={'query':'Саратов','items':[{'title':'Саратов','url':'https://example.org/saratov','snippet':'Саратов, сегодня: −2 °C.'}]})
        self.rt.model.completion.side_effect=[
            {'content':json.dumps({'actions':[{'name':'web_search','args':{'query':'Саратов'}}]})},
            {'content':'В Саратове −2 °C. https://example.org/saratov'}]
        event=self.event('Даша найди погоду в Саратове',id=410,reply_user=None)
        self.assertTrue(await self.handle(event))
        self.assertIn('−2 °C',self.client.send_message.call_args.args[1])
        self.assertNotIn('"items"',self.client.send_message.call_args.args[1])
        self.assertTrue(await self.handle(event))
        self.rt.research.search.assert_awaited_once()
        self.client.send_message.assert_awaited_once()
        self.assertEqual(self.rt.model.completion.await_count,2)
        self.assertFalse(self.rt.model.completion.call_args.kwargs['with_tools'])

    async def test_new_research_request_does_not_resume_old_moderation(self):
        self.store.execute('INSERT INTO group_admin_pending VALUES (?,?,?,?,?,?,?,?)',
            (CHAT,0,OWNER,self.store.dialog_scope(CHAT,0)[2],80,None,'верни его',time.time()))
        self.rt.research.search=AsyncMock(return_value={'query':'Саратов','items':[]})
        self.rt.model.completion.side_effect=[
            {'content':json.dumps({'actions':[{'name':'telegram_action','args':{'action':'web_search','query':'Саратов'}}]})},
            {'content':'Текущую погоду в Саратове подтвердить не удалось.'}]
        self.assertTrue(await self.handle(self.event('Даша какая погода в саратове',id=411,reply_user=None)))
        response=self.client.send_message.call_args.args[1]
        self.assertIn('подтвердить не удалось',response)
        self.assertNotIn('верни',response)
        self.assertIsNone(self.admin.pending(CHAT,0))
        self.assertFalse(any(isinstance(call.args[0],functions.channels.EditBannedRequest) for call in self.client.await_args_list))
        data=json.loads(self.rt.model.completion.call_args.args[0][1]['content'])
        self.assertEqual(data['request'],'Даша какая погода в саратове')
        self.assertNotIn('верни его',json.dumps(data,ensure_ascii=False))

    async def test_question_with_embedded_moderation_request_keeps_command_planning(self):
        self.rt.model.completion.side_effect=ModelUnavailable('API модели вернул код 503')
        self.assertTrue(await self.handle(self.event('Почему он флудит? Замуть офицера',reply_user=None)))
        self.rt.model.completion.assert_awaited_once()

    async def test_informational_phrase_does_not_bypass_a_pending_command(self):
        self.store.execute('INSERT INTO group_admin_pending VALUES (?,?,?,?,?,?,?,?)',
            (CHAT,0,OWNER,self.store.dialog_scope(CHAT,0)[2],80,None,'верни его',time.time()))
        self.rt.model.completion.side_effect=ModelUnavailable('API модели вернул код 503')
        self.assertTrue(await self.handle(self.event('Что дальше?',reply_user=None)))
        self.rt.model.completion.assert_awaited_once()

    async def test_public_participant_name_can_resolve_to_id(self):
        self.store.group_message(CHAT,1,'привет',sender_id=1000000003,sender_name='Офицер Ксививайдовичь',username='DelegateTest')
        self.rt.model.completion.return_value={'content':'{"intent":"command","actions":[{"action":"mute","user":"1000000003"}]}'}
        self.assertTrue(await self.handle(self.event('замуть офицера',reply_user=None)))
        data=json.loads(self.rt.model.completion.call_args.args[0][1]['content'])['state']['context']
        self.assertEqual(data['known_participants'][0]['names'],['Офицер Ксививайдовичь'])
        self.assertEqual(self.client.call_args.args[0].participant.id,1000000003)
        self.assertTrue(self.client.call_args.args[0].banned_rights.send_messages)

    async def test_group_gift_global_off_explains_policy_and_never_pays(self):
        self.store.upsert_contact(10,'User');self.store.execute('UPDATE contacts SET gift_allowed=1 WHERE id=10')
        self.store.update_settings({'gifts_enabled':False,'gift_max_price':100,'gift_monthly_budget':100})
        self.rt.model.completion.return_value={'content':'{"intent":"command","actions":[{"action":"gift","user":"reply"}]}'}
        self.tg.send_gift=AsyncMock()
        self.assertTrue(await self.handle(self.event('Подари ему подарок')))
        self.assertIn('общим переключателем',self.client.send_message.call_args.args[1])
        self.tg.send_gift.assert_not_awaited()
        self.assertEqual(self.store.rows('SELECT * FROM gift_orders'),[])

    async def test_group_gift_requires_owner_and_deduplicates_message(self):
        self.rt.gifts.send_requested=AsyncMock(return_value={'sent':True})
        self.rt.model.completion.return_value={'content':'{"intent":"command","actions":[{"action":"gift","user":"reply","text":"сердечко"}]}'}
        await self.handle(self.event('подари ему сердечко',user=20))
        self.rt.gifts.send_requested.assert_not_awaited()
        event=self.event('подари ему сердечко')
        await self.handle(event);await self.handle(event)
        self.rt.gifts.send_requested.assert_awaited_once()
        self.assertEqual(self.rt.gifts.send_requested.call_args.args[:2],(10,'сердечко'))

    async def test_gift_to_owner_does_not_require_group_admin_rights(self):
        self.client.get_permissions.side_effect=lambda *args:self.member
        self.rt.gifts.send_requested=AsyncMock(return_value={'sent':True})
        result=await self.execute('gift',user=str(OWNER),text='любой')
        self.assertEqual(result['user_id'],OWNER)

    async def test_recent_target_restore_and_private_link_follow_screenshot_commands(self):
        await self.handle(self.event('даш забань его'))
        self.client.reset_mock()
        await self.handle(self.event('верни его',id=101,reply_user=None))
        requests=[c.args[0] for c in self.client.await_args_list]
        self.assertEqual(len(requests),2)
        self.assertIsInstance(requests[0],functions.channels.EditBannedRequest)
        self.assertFalse(requests[0].banned_rights.view_messages)
        self.assertIsInstance(requests[1],functions.channels.InviteToChannelRequest)
        self.assertEqual(requests[1].users[0].id,10)
        self.client.reset_mock()
        await self.handle(self.event('скинь ему в лс ссылку чата',id=102,reply_user=SELF))
        direct=[c for c in self.client.send_message.await_args_list if isinstance(c.args[0],types.User)]
        self.assertEqual(len(direct),1)
        self.assertEqual(direct[0].args[0].id,10)
        self.assertEqual(direct[0].args[1],'https://t.me/+fixture')
        await self.handle(self.event('скинь ему в лс ссылку чата',id=102,reply_user=SELF))
        self.assertEqual(self.client.await_count,1)
        self.rt.model.completion.assert_not_awaited()

    async def test_recent_target_is_persisted_but_scoped_to_owner_group_topic_and_time(self):
        await self.execute('ban',user='10')
        other=Store(self.store.directory)
        try:
            self.assertEqual(other.rows('SELECT user_id FROM group_admin_context')[0]['user_id'],10)
        finally:other.close()
        self.assertEqual(self.admin.context(CHAT,8)['user_id'],10)
        self.assertIsNone(self.admin.context(CHAT,None))
        self.assertIsNone(self.admin.context(CHAT-1,8))
        self.store.update_settings({'owner_id':20})
        self.assertIsNone(self.admin.context(CHAT,8))
        self.store.update_settings({'owner_id':OWNER})
        self.store.execute('UPDATE group_admin_context SET updated=?',(time.time()-1801,))
        self.assertIsNone(self.admin.context(CHAT,8))

    async def test_missing_context_asks_for_target_without_mutation(self):
        await self.handle(self.event('верни его',reply_user=None))
        self.client.assert_not_awaited()
        self.assertIn('Неясно',self.client.send_message.call_args.args[1])

    async def test_recipient_clarification_continues_pending_invite_after_restart(self):
        await self.handle(self.event('скинь ему ссылку в лс',reply_user=None))
        pending=self.admin.pending(CHAT,0)
        self.assertEqual(pending['command'],'скинь ему ссылку в лс')
        other=Store(self.store.directory)
        try:self.assertEqual(other.rows('SELECT command FROM group_admin_pending')[0]['command'],pending['command'])
        finally:other.close()
        self.rt.model.completion.return_value={'content':'{"actions":[{"action":"send_invite","user":"@example"}],"continue_pending":true}'}
        self.client.reset_mock()
        await self.handle(self.event('@example ему',id=101,reply_user=SELF))
        direct=[c for c in self.client.send_message.await_args_list if isinstance(c.args[0],types.User)]
        self.assertEqual(len(direct),1);self.assertEqual(direct[0].args[0].id,10)
        self.assertIsNone(self.admin.pending(CHAT,0))
        data=json.loads(self.rt.model.completion.call_args.args[0][1]['content'])['state']['context']
        self.assertEqual(data['pending_request']['command'],'скинь ему ссылку в лс')

    async def test_reply_to_old_confirmation_resolves_target_from_original_owner_command(self):
        self.store.group_message(CHAT,89,'забань @example',sender_id=OWNER)
        self.store.group_message(CHAT,90,'Выполнено: участник заблокирован.',sender_id=SELF,outgoing=True,reply_to=89)
        self.rt.model.completion.return_value={'content':'{"actions":[{"action":"send_invite","user":"@example"}]}'}
        await self.handle(self.event('скинь ему ссылку чата в лс',reply_user=SELF))
        direct=[c for c in self.client.send_message.await_args_list if isinstance(c.args[0],types.User)]
        self.assertEqual(len(direct),1)
        data=json.loads(self.rt.model.completion.call_args.args[0][1]['content'])['state']['context']
        self.assertEqual([r['id'] for r in data['reply_chain']],[89,90])

    async def test_reply_to_old_clarification_restores_unfinished_request_from_group_history(self):
        self.store.group_message(CHAT,89,'скинь ему ссылку чата в лс',sender_id=OWNER)
        self.store.group_message(CHAT,90,'Неясно, о ком речь. Ответь на сообщение участника',sender_id=SELF,outgoing=True,reply_to=89)
        self.rt.model.completion.return_value={'content':'{"actions":[{"action":"send_invite","user":"@example"}],"continue_pending":true}'}
        await self.handle(self.event('@example ему',reply_user=SELF))
        self.assertTrue(any(isinstance(c.args[0],types.User) for c in self.client.send_message.await_args_list))

    async def test_ordinary_chat_and_other_users_do_not_execute_a_pending_command(self):
        await self.handle(self.event('скинь ему ссылку в лс',reply_user=None))
        self.client.reset_mock()
        self.assertFalse(await self.handle(self.event('@example ему',user=20,id=101)))
        self.assertFalse(await self.handle(self.event('я пока отойду',id=102)))
        self.client.assert_not_awaited()
        self.assertIsNotNone(self.admin.pending(CHAT,0))

    async def test_cancel_pending_and_scope_expiry_do_not_replay_commands(self):
        await self.handle(self.event('скинь ему ссылку в лс',reply_user=None))
        self.assertIsNone(self.admin.pending(CHAT,8))
        self.assertIsNone(self.admin.pending(CHAT-1,0))
        self.store.update_settings({'owner_id':20})
        self.assertIsNone(self.admin.pending(CHAT,0))
        self.store.update_settings({'owner_id':OWNER})
        self.rt.model.completion.return_value={'content':'{"actions":[],"cancel_pending":true}'}
        await self.handle(self.event('отмена, ничего не надо',id=101,reply_user=None))
        self.assertIsNone(self.admin.pending(CHAT,0));self.client.assert_not_awaited()

    async def test_planner_can_ask_for_missing_fields_then_accept_a_followup(self):
        self.rt.model.completion.return_value={'content':'{"actions":[],"clarification":"Как назвать группу?"}'}
        await self.handle(self.event('поменяй название группы',reply_user=None))
        self.client.assert_not_awaited();self.assertIsNotNone(self.admin.pending(CHAT,0))
        self.rt.model.completion.return_value={'content':'{"actions":[{"action":"title","text":"Посиделки"}],"continue_pending":true}'}
        await self.handle(self.event('Посиделки',id=101,reply_user=SELF))
        self.assertIsInstance(self.client.call_args.args[0],functions.channels.EditTitleRequest)
        self.assertIsNone(self.admin.pending(CHAT,0))

    async def test_model_cannot_resume_a_missing_or_archived_request(self):
        self.rt.model.completion.return_value={'content':'{"actions":[{"action":"send_invite","user":"@example"}],"continue_pending":true}'}
        await self.handle(self.event('@example ему',reply_user=None))
        self.client.assert_not_awaited()
        self.rt.model.completion.return_value={'content':'{"actions":[]}'}
        await self.handle(self.event('скинь ему ссылку в лс',id=101,reply_user=None))
        self.assertIsNotNone(self.admin.pending(CHAT,0))
        self.store.archive_contexts()
        self.assertIsNone(self.admin.pending(CHAT,0))

    async def test_confirmations_are_saved_in_shared_group_context(self):
        self.client.send_message.return_value=NS(id=102)
        await self.handle(self.event('забань его'))
        row=self.store.group_history(CHAT)[-1]
        self.assertEqual(row['telegram_id'],102)
        self.assertEqual(row['reply_to'],100)
        self.assertTrue(row['outgoing'])

    async def test_targeted_watch_is_scoped_and_stops_when_owner_changes_or_disables_it(self):
        self.rt.model.completion.return_value={'content':'{"actions":[{"action":"watch_user","user":"@example","enabled":true}]}'}
        await self.handle(self.event('следи за ним @example',reply_user=None))
        self.assertTrue(self.admin.policy(CHAT,10));self.assertFalse(self.admin.policy(CHAT,20))
        self.assertFalse(self.admin.policy(CHAT-1,10));self.assertFalse(self.admin.policy(CHAT))
        for i in range(6):await self.admin.antispam(self.event('флуд',user=10,id=200+i),types.User(10))
        self.client.delete_messages.assert_awaited_once()
        self.assertTrue(self.client.call_args.args[0].banned_rights.send_messages)
        self.store.update_settings({'owner_id':20})
        self.assertFalse(self.admin.policy(CHAT,10))
        self.store.update_settings({'owner_id':OWNER})
        await self.execute('watch_user',user='10',enabled=False)
        self.assertFalse(self.admin.policy(CHAT,10))

    async def test_explicit_reply_takes_precedence_over_recent_target(self):
        await self.handle(self.event('забань его'))
        await self.handle(self.event('разбань его',id=101,reply_user=20))
        self.assertEqual(self.client.call_args.args[0].participant.id,20)

    async def test_model_gets_recent_context_and_resolves_reference(self):
        await self.handle(self.event('забань его'))
        self.rt.model.completion.return_value={'content':'{"actions":[{"action":"unban","user":"recent"}]}'}
        await self.handle(self.event('убери ему бан',id=101,reply_user=None))
        self.assertEqual(self.client.call_args.args[0].participant.id,10)
        data=json.loads(self.rt.model.completion.call_args.args[0][1]['content'])['state']['context']
        self.assertEqual(data['recent_action'],{'user_id':10,'action':'ban'})

    async def test_model_may_use_real_id_from_context_or_reply_but_cannot_invent_one(self):
        await self.handle(self.event('забань его'))
        self.rt.model.completion.return_value={'content':'{"actions":[{"action":"unban","user":"10"}]}'}
        await self.handle(self.event('убери ему бан',id=101,reply_user=None))
        self.assertEqual(self.client.call_args.args[0].participant.id,10)
        self.rt.model.completion.return_value={'content':'{"actions":[{"action":"ban","user":"20"}]}'}
        await self.handle(self.event('заблокируй этого',id=102,reply_user=20))
        self.assertEqual(self.client.call_args.args[0].participant.id,20)
        self.client.reset_mock()
        self.rt.model.completion.return_value={'content':'{"actions":[{"action":"ban","user":"999"}]}'}
        await self.handle(self.event('заблокируй этого',id=103,reply_user=None))
        self.client.assert_not_awaited()

    async def test_duplicate_command_cannot_replan_extra_mutations(self):
        self.rt.model.completion.return_value={'content':'{"actions":[{"action":"title","text":"New"}]}'}
        event=self.event('измени название на New')
        await self.handle(event)
        self.rt.model.completion.return_value={'content':'{"actions":[{"action":"title","text":"New"},{"action":"ban","user":"reply"}]}'}
        await self.handle(event)
        self.rt.model.completion.assert_awaited_once()
        self.client.assert_awaited_once()

    async def test_concurrent_duplicate_commands_execute_only_once(self):
        event=self.event('забань его')
        await asyncio.gather(self.handle(event),self.handle(event))
        self.client.assert_awaited_once()

    async def test_videochat_screenshot_is_a_command_without_character_model(self):
        async def rpc(request):
            if isinstance(request,functions.channels.GetFullChannelRequest):
                return NS(full_chat=NS(call=None))
            return await self.rpc(request)
        self.client.side_effect=rpc
        await self.handle(self.event('даш начни видеочат',reply_user=None))
        self.assertIsInstance(self.client.call_args.args[0],functions.phone.CreateGroupCallRequest)
        self.assertIn('создан',self.client.send_message.call_args.args[1])
        self.rt.model.completion.assert_not_awaited()

    async def test_regular_voice_request_is_not_refused_when_not_admin(self):
        self.me.is_admin=False
        self.assertFalse(await self.handle(self.event('даш запиши голосовое',reply_user=None)))
        self.client.assert_not_awaited()
        self.client.send_message.assert_not_awaited()

    async def test_intent_parser_receives_owner_requests_without_keyword_gate(self):
        self.rt.model.completion.return_value={'content':'{"actions":[{"action":"start_call"}]}'}
        self.admin.execute=AsyncMock(return_value={'text':'Голосовой чат создан.'})
        for i,text in enumerate(['дашуль включай видеочат','давай созвон в этой группе','можно нам войс?','хочу чтобы тут был голосовой чат']):
            await self.handle(self.event(text,id=200+i,reply_user=None))
        self.assertEqual(self.rt.model.completion.await_count,4)
        self.assertEqual(self.admin.execute.await_count,4)
        self.assertTrue(all(call.args[1].action=='start_call' for call in self.admin.execute.await_args_list))

    async def test_unrelated_owner_conversation_is_left_to_conversation_handler(self):
        self.assertFalse(await self.handle(self.event('дашуль как прошёл день?',reply_user=None)))
        self.rt.model.completion.assert_awaited_once()
        self.client.send_message.assert_not_awaited()

    async def test_non_owner_flexible_request_never_reaches_admin_planner(self):
        self.assertFalse(await self.handle(self.event('дашуль включай видеочат',user=20,reply_user=None)))
        self.rt.model.completion.assert_not_awaited()
        self.client.assert_not_awaited()

    async def test_restore_reports_partial_success_when_privacy_prevents_invite(self):
        async def rpc(request):
            if isinstance(request,functions.channels.InviteToChannelRequest):
                return NS(missing_invitees=[NS(user_id=10)])
            return await self.rpc(request)
        self.client.side_effect=rpc
        result=await self.execute('restore',user='10')
        self.assertIn('Бан снят',result['text'])
        self.assertIn('не разрешил добавить',result['text'])
        self.assertNotIn('добавлен обратно',result['text'])
        with self.assertRaises(ValueError):await self.execute('invite',user='10')

    async def test_private_invite_respects_disabled_contact_and_delivery_failure(self):
        self.store.upsert_contact(10,'User')
        self.store.execute('UPDATE contacts SET enabled=0 WHERE id=10')
        with self.assertRaises(ValueError):await self.execute('send_invite',user='10')
        self.client.assert_not_awaited()
        self.client.send_message.assert_not_awaited()
        self.store.execute('UPDATE contacts SET enabled=1 WHERE id=10')
        self.client.send_message.side_effect=ConnectionError('lost')
        await self.handle(self.event('скинь ему в лс ссылку чата'))
        self.assertEqual(self.store.rows('SELECT status FROM group_admin_actions')[0]['status'],'uncertain')
        self.client.reset_mock()
        await self.handle(self.event('скинь ему в лс ссылку чата'))
        self.client.send_message.assert_not_awaited()

    async def test_other_admin_forward_anonymous_and_old_updates_cannot_command(self):
        events=[self.event('забань этого',user=20),self.event('забань этого',id=101),self.event('забань этого',id=102),
                self.event('забань этого',id=103),self.event('забань этого',id=104),self.event('забань этого',id=105)]
        events[1].message.fwd_from=types.MessageFwdHeader(datetime.now(timezone.utc),from_id=types.PeerUser(OWNER))
        events[2].message.from_id=types.PeerChannel(123)
        events[3].message.date=datetime.now(timezone.utc)-timedelta(hours=1)
        events[4].message.via_bot_id=33
        events[5].message.entities=[types.MessageEntityBlockquote(0,12)]
        for e in events:await self.handle(e)
        self.client.assert_not_awaited();self.client.send_message.assert_not_awaited();self.rt.model.completion.assert_not_awaited()

    async def test_missing_right_and_protected_target_prevent_mutation(self):
        self.me.participant.admin_rights.ban_users=False
        await self.handle(self.event('забань этого'))
        self.client.assert_not_awaited()
        self.me.participant.admin_rights.ban_users=True
        for user in [OWNER,SELF]:
            with self.assertRaises(ValueError):await self.admin.execute(self.chat,AdminOperation(action='ban',user=str(user)),Mock())
        self.member.is_admin=True
        with self.assertRaises(ValueError):await self.execute('ban',user='10')
        self.client.assert_not_awaited()

    async def test_owner_change_during_permission_fetch_prevents_dispatch(self):
        async def changed(chat,user):
            self.store.update_settings({'owner_id':22});return self.me
        self.client.get_permissions.side_effect=changed
        await self.handle(self.event('забань этого'))
        self.client.assert_not_awaited()

    async def test_model_plan_has_group_metadata_only_and_validates_all_actions_first(self):
        self.store.upsert_contact(OWNER,'Owner');self.store.message(OWNER,'user','PRIVATE_OWNER_SECRET',5,sender_id=OWNER)
        self.rt.model.completion.return_value={'content':json.dumps({'actions':[{'action':'title','text':'New'}, {'action':'ban','user':'@invented'}]})}
        await self.handle(self.event('измени название группы на New'))
        self.client.assert_not_awaited()
        messages=self.rt.model.completion.call_args.args[0]
        self.assertNotIn('PRIVATE_OWNER_SECRET',str(messages));self.assertIn('my_admin_rights',str(messages))
        self.assertFalse(self.rt.model.completion.call_args.kwargs['with_tools'])

    async def test_admin_grants_cannot_exceed_own_rights_and_preserve_unedited_rights(self):
        self.me.participant.admin_rights.add_admins=True;self.me.participant.admin_rights.manage_call=False
        with self.assertRaises(ValueError):await self.execute('promote',user='10',rights={'manage_call':True})
        self.member.is_admin=True;self.member.participant.can_edit=True
        self.member.participant.admin_rights=types.ChatAdminRights(delete_messages=True,pin_messages=True)
        await self.execute('promote',user='10',rights={'ban_users':True})
        request=self.client.call_args.args[0]
        self.assertIsInstance(request,functions.channels.EditAdminRequest)
        self.assertTrue(request.admin_rights.delete_messages);self.assertTrue(request.admin_rights.pin_messages);self.assertTrue(request.admin_rights.ban_users)
        self.assertFalse(bool(request.admin_rights.add_admins))

    async def test_owner_can_receive_explicit_admin_grant(self):
        await self.execute('promote',user=str(OWNER),rights={'delete_messages':True})
        request=self.client.call_args.args[0]
        self.assertEqual(request.user_id.id,OWNER);self.assertTrue(request.admin_rights.delete_messages)

    async def test_natural_antispam_request_and_exact_member_reference(self):
        self.rt.model.completion.return_value={'content':'{"actions":[{"action":"antispam","enabled":true}]}'}
        await self.handle(self.event('Даша, следи за порядком в группе, пожалуйста'))
        self.assertTrue(self.admin.policy(CHAT));self.rt.model.completion.assert_awaited_once()
        self.client.reset_mock()
        self.rt.model.completion.return_value={'content':'{"actions":[{"action":"ban","user":"@example"}]}'}
        await self.handle(self.event('заблокируй @example_long',id=101))
        self.client.assert_not_awaited()

    async def test_model_json_fence_is_supported_but_unrequested_tools_are_rejected(self):
        self.rt.model.completion.return_value={'content':'```json\n{"actions":[{"action":"pending_requests"}]}\n```'}
        result=await self.admin.plan('покажи заявки',None,None,['invite_users'])
        self.assertEqual(result[0].action,'pending_requests')
        self.rt.model.completion.return_value={'content':'{"actions":[{"action":"pending_requests"}]}','tool_calls':[{'id':'unrequested'}]}
        with self.assertRaises(ValueError):await self.admin.plan('покажи заявки',None,None,['invite_users'])
        self.client.assert_not_awaited()
        self.rt.model.completion.side_effect=[{'content':'{"actions":[{"action":"join_voicechat","user":"reply","mode":"talk"}]}'},
                                             {'content':'{"actions":[{"action":"join_voicechat","mode":"talk"}]}'}]
        actions=await self.admin.plan('подключись к голосовому чату и говори',None,None,['manage_call'])
        self.assertEqual(actions[0].mode,'talk');self.client.assert_not_awaited()

    async def test_restriction_and_default_permissions_preserve_other_restrictions(self):
        later=datetime.now(timezone.utc)+timedelta(hours=2)
        self.member.participant.banned_rights=types.ChatBannedRights(until_date=later,send_messages=True,invite_users=True)
        await self.execute('mute',user='10',minutes=10)
        rights=self.client.call_args.args[0].banned_rights
        self.assertEqual(rights.until_date,later);self.assertTrue(rights.invite_users)
        await self.execute('unmute',user='10')
        self.assertFalse(self.client.call_args.args[0].banned_rights.send_messages)
        self.assertTrue(self.client.call_args.args[0].banned_rights.invite_users)
        self.chat.default_banned_rights=types.ChatBannedRights(None,pin_messages=True)
        await self.execute('permissions',permissions={'send_stickers':False})
        rights=self.client.call_args.args[0].banned_rights
        self.assertTrue(rights.pin_messages);self.assertTrue(rights.send_stickers)

    async def test_core_group_admin_operations_use_correct_scoped_rpcs(self):
        cases=[('unban',{'user':'10'},functions.channels.EditBannedRequest),
            ('restrict',{'user':'10','permissions':{'send_photos':False}},functions.channels.EditBannedRequest),
            ('member_rank',{'user':'10','text':'Помощник'},functions.messages.EditChatParticipantRankRequest),
            ('demote',{'user':'10'},functions.channels.EditAdminRequest),
            ('title',{'text':'New name'},functions.channels.EditTitleRequest),
            ('description',{'text':'New description'},functions.messages.EditChatAboutRequest),
            ('remove_photo',{},functions.channels.EditPhotoRequest),
            ('pin',{},functions.messages.UpdatePinnedMessageRequest),('unpin',{},functions.messages.UpdatePinnedMessageRequest),
            ('unpin_all',{},functions.messages.UnpinAllMessagesRequest),
            ('slowmode',{'seconds':30},functions.channels.ToggleSlowModeRequest),
            ('invite',{'user':'10'},functions.channels.InviteToChannelRequest),
            ('invite_link',{'text':'Test','minutes':20,'limit':3},functions.messages.ExportChatInviteRequest),
            ('revoke_invite',{'link':'https://t.me/+abcdef'},functions.messages.EditExportedChatInviteRequest),
            ('approve_join',{'user':'10'},functions.messages.HideChatJoinRequestRequest),
            ('decline_join',{'user':'10'},functions.messages.HideChatJoinRequestRequest),
            ('join_requests',{'enabled':True},functions.channels.ToggleJoinRequestRequest),
            ('history_visible',{'enabled':True},functions.channels.TogglePreHistoryHiddenRequest),
            ('forum',{'enabled':True},functions.channels.ToggleForumRequest),
            ('create_topic',{'text':'Topic'},functions.messages.CreateForumTopicRequest),
            ('edit_topic',{'text':'New topic'},functions.messages.EditForumTopicRequest),
            ('close_topic',{},functions.messages.EditForumTopicRequest),('open_topic',{},functions.messages.EditForumTopicRequest),
            ('delete_topic',{},functions.messages.DeleteTopicHistoryRequest),
            ('pin_topic',{},functions.messages.UpdatePinnedForumTopicRequest),('unpin_topic',{},functions.messages.UpdatePinnedForumTopicRequest),
            ('end_call',{},functions.phone.DiscardGroupCallRequest),
            ('call_title',{'text':'Call'},functions.phone.EditGroupCallTitleRequest),
            ('mute_in_call',{'user':'10'},functions.phone.EditGroupCallParticipantRequest),
            ('unmute_in_call',{'user':'10'},functions.phone.EditGroupCallParticipantRequest),
            ('record_call',{'enabled':True},functions.phone.ToggleGroupCallRecordRequest),
            ('delete_user_history',{'user':'10'},functions.channels.DeleteParticipantHistoryRequest)]
        for action,args,kind in cases:
            with self.subTest(action=action):
                self.client.reset_mock();result=await self.execute(action,**args)
                self.assertTrue(result['text']);request=self.client.call_args.args[0]
                self.assertIsInstance(request,kind)
                self.assertEqual(getattr(request,'channel',getattr(request,'peer',self.chat)),self.chat)

    async def test_start_call_does_not_replace_existing_call(self):
        with self.assertRaises(ValueError):await self.execute('start_call',text='Test')
        self.assertFalse(any(isinstance(c.args[0],functions.phone.CreateGroupCallRequest) for c in self.client.await_args_list))
        self.client.side_effect=lambda req:NS(full_chat=NS(call=None)) if isinstance(req,functions.channels.GetFullChannelRequest) else True
        await self.execute('start_call',text='Test')
        self.assertIsInstance(self.client.call_args.args[0],functions.phone.CreateGroupCallRequest)

    async def test_kick_preserves_existing_restrictions_and_guards_each_write(self):
        self.member.participant.banned_rights=types.ChatBannedRights(None,send_messages=True)
        guard=Mock()
        await self.admin.execute(self.chat,AdminOperation(action='kick',user='10'),guard)
        requests=[c.args[0] for c in self.client.await_args_list]
        self.assertEqual(len(requests),2);self.assertTrue(requests[0].banned_rights.view_messages)
        self.assertFalse(bool(requests[1].banned_rights.view_messages));self.assertTrue(requests[1].banned_rights.send_messages)
        self.assertGreaterEqual(guard.call_count,4)

    async def test_delete_and_photo_are_scoped_to_verified_group_message(self):
        await self.execute('delete');self.client.delete_messages.assert_awaited_once_with(self.chat,[90],revoke=True)
        photo=self.message(90,OWNER,'photo');photo.media=types.MessageMediaPhoto(photo=types.Photo(1,2,b'',datetime.now(timezone.utc),[types.PhotoSize('x',100,100,1000)],2))
        self.client.get_messages.return_value=photo;self.client.get_messages.side_effect=None
        await self.execute('photo')
        self.assertIsInstance(self.client.call_args.args[0],functions.channels.EditPhotoRequest)
        wrong=self.message(90,10,'wrong');wrong.peer_id=types.PeerChannel(999)
        self.client.get_messages.return_value=wrong
        with self.assertRaises(ValueError):await self.execute('pin')

    async def test_antispam_opt_in_owner_only_persistent_and_works_when_chatbot_disabled(self):
        self.store.update_settings({'enabled':False,'group_enabled':False})
        owner=self.event('следи за порядком в чате',reply_user=None)
        await self.tg.on_message(owner)
        self.assertTrue(self.admin.policy(CHAT));self.assertFalse(self.store.settings()['enabled'])
        before=self.client.send_message.await_count
        for id in [101,102,103]:
            event=self.event('Повторяющаяся реклама спама',user=10,id=id,reply_user=None)
            await self.tg.on_message(event)
        self.client.delete_messages.assert_awaited_once_with(self.chat,[101,102,103],revoke=True)
        bans=[c.args[0] for c in self.client.await_args_list if isinstance(c.args[0],functions.channels.EditBannedRequest)]
        self.assertEqual(len(bans),1);self.assertTrue(bans[0].banned_rights.send_messages);self.assertFalse(bool(bans[0].banned_rights.view_messages))
        self.assertEqual(self.client.send_message.await_count,before)
        self.assertNotIn('реклама',str(self.admin.recent))
        self.store.update_settings({'owner_id':22});self.assertFalse(self.admin.policy(CHAT))

    async def test_spam_excludes_admins_other_groups_and_ordinary_short_replies(self):
        await self.execute('antispam',enabled=True)
        for id in range(101,104):await self.handle(self.event('Да',user=10,id=id))
        self.client.delete_messages.assert_not_awaited()
        self.member.is_admin=True
        for id in range(104,111):await self.handle(self.event('Повторяющийся текст администратора',user=10,id=id))
        self.client.delete_messages.assert_not_awaited()
        self.member.is_admin=False
        for id in range(111,118):
            e=self.event('Реклама в другой группе',user=20,id=id);e.chat_id=-100999
            await self.handle(e)
        self.client.delete_messages.assert_not_awaited()

    async def test_antispam_checks_revoked_rights_and_respects_flood_wait(self):
        await self.execute('antispam',enabled=True)
        self.me.participant.admin_rights.delete_messages=False
        for id in range(101,104):await self.handle(self.event('Повторяющийся рекламный текст',user=10,id=id))
        self.client.delete_messages.assert_not_awaited()
        self.me.participant.admin_rights.delete_messages=True
        self.client.delete_messages.side_effect=errors.FloodWaitError(None,capture=30)
        await self.handle(self.event('Повторяющийся рекламный текст',user=10,id=104))
        self.assertGreater(self.admin.cooldown_until,time.time())
        await self.handle(self.event('Повторяющийся рекламный текст',user=10,id=105))
        self.client.delete_messages.assert_awaited_once()

    async def test_uncertain_actions_are_not_repeated_after_restart(self):
        operation=AsyncMock(side_effect=OSError('connection lost'))
        with self.assertRaises(OSError):await self.admin.recorded(CHAT,100,'0',operation)
        await self.admin.recorded(CHAT,100,'0',operation);operation.assert_awaited_once()
        self.assertEqual(self.store.rows('SELECT status FROM group_admin_actions')[0]['status'],'uncertain')

    async def test_private_owner_tool_is_restricted_and_returns_action_result(self):
        for id in [OWNER,10]:self.store.upsert_contact(id,str(id));self.store.message(id,'user','Измени название группы @test на Test',1,sender_id=id)
        args={'group':'@test','operation':{'action':'title','text':'Test'}}
        with self.assertRaises(ValueError):await self.rt.control.action(10,1,'manage_group',args,trusted=True)
        result=await self.rt.control.action(OWNER,1,'manage_group',args,trusted=True)
        self.assertIn('название',result['text']);self.assertIsInstance(self.client.call_args.args[0],functions.channels.EditTitleRequest)

    async def test_group_call_join_and_leave_cannot_touch_other_groups_call(self):
        self.rt.calls.join=AsyncMock(return_value={'mode':'talk'})
        self.rt.calls.leave=AsyncMock()
        await self.execute('join_voicechat',mode='talk')
        self.assertEqual(self.rt.calls.join.call_args.args[:2],(str(CHAT),'talk'))
        self.rt.calls.active={'chat_id':-100999}
        with self.assertRaises(ValueError):await self.execute('leave_voicechat')
        self.rt.calls.leave.assert_not_awaited()
        self.rt.calls.active={'chat_id':CHAT}
        await self.execute('leave_voicechat');self.rt.calls.leave.assert_awaited_once()
        self.rt.calls.active=None

    async def test_voicechat_join_uses_configured_talk_mode_when_unspecified(self):
        self.store.update_settings({'call_mode': 'talk'})
        self.rt.calls.join = AsyncMock(return_value={'mode': 'talk'})
        result = await self.execute('join_voicechat')
        self.assertEqual(self.rt.calls.join.call_args.args[:2], (str(CHAT), 'talk'))
        self.assertIn('Могу отвечать голосом', result['text'])

    async def test_screenshot_shout_command_goes_to_voice_tool_without_model(self):
        self.rt.calls.say = AsyncMock(return_value={'spoken': True, 'text': 'Произнесла фразу в голосовом чате.'})
        for index, text in enumerate(['закричи сильно', 'кричи в видеочате']):
            event = self.event(text, id=200 + index, reply_user=None)
            self.assertTrue(await self.handle(event))
            self.assertEqual(self.rt.calls.say.call_args.args[0], 'А-а-а-а!')
            self.assertEqual(self.rt.calls.say.call_args.kwargs['chat_id'], CHAT)
            self.assertEqual(self.rt.calls.say.call_args.kwargs['volume'], 2)
        self.rt.model.completion.assert_not_awaited()

    async def test_other_participant_cannot_make_bot_speak_in_voicechat(self):
        self.rt.calls.say = AsyncMock()
        await self.handle(self.event('кричи в видеочате', user=10, id=203, reply_user=None))
        self.rt.calls.say.assert_not_awaited()

    async def test_clear_account_command_in_group_requires_owner_and_bypasses_character_reply(self):
        self.rt.account.execute = AsyncMock(return_value={'id': 10, 'first_name': 'Друг', 'last_name': '',
            'username': 'example', 'bio': 'Био', 'status': 'недавно', 'birthday': None, 'premium': False,
            'has_photo': False, 'photos': []})
        await self.handle(self.event('покажи профиль @example', id=204, reply_user=None))
        self.rt.account.execute.assert_awaited_once()
        self.assertEqual(self.rt.account.execute.call_args.args[:2], ('get_telegram_profile', {'recipient': '@example', 'include_photo': False}))
        self.rt.model.completion.assert_not_awaited()
        await self.handle(self.event('покажи профиль @example', user=10, id=205, reply_user=None))
        self.rt.account.execute.assert_awaited_once()

    async def test_group_reads_do_not_expose_private_profile_fields(self):
        user=types.User(10,first_name='Test',username='test',phone='PRIVATE_PHONE')
        self.client.get_participants.return_value=[user]
        for action in ['members','admins','banned']:
            result=await self.execute(action);self.assertIn('@test',result['text']);self.assertNotIn('PRIVATE_PHONE',result['text'])
        self.client.side_effect=lambda req: NS(importers=[NS(user_id=10)],users=[user]) if isinstance(req,functions.messages.GetChatInviteImportersRequest) else NS(topics=[NS(id=8,title='Topic')])
        result=await self.execute('pending_requests');self.assertIn('@test',result['text']);self.assertNotIn('PRIVATE_PHONE',result['text'])
        result=await self.execute('topics');self.assertIn('Topic',result['text'])

    async def test_unpin_all_finishes_pagination_and_general_topic_visibility(self):
        self.client.side_effect=[NS(offset=2),NS(offset=0)]
        await self.execute('unpin_all');self.assertEqual(self.client.await_count,2)
        self.client.side_effect=self.rpc
        await self.execute('hide_topic',topic_id=1)
        self.assertTrue(self.client.call_args.args[0].hidden)
        await self.execute('show_topic',topic_id=1)
        self.assertFalse(self.client.call_args.args[0].hidden)

    def test_parser_and_schema_refuse_ambiguous_or_unknown_operations(self):
        self.assertEqual(parse_command('Даша, забань этого')[0].action,'ban')
        self.assertIsNone(parse_command('Он сказал: забань этого'))
        for op in [{'action':'shell','text':'id'},{'action':'ban','user':'@test','seconds':10},
                   {'action':'promote','user':'10'},{'action':'slowmode','seconds':31},
                   {'action':'permissions','permissions':{'unknown':True}}]:
            with self.assertRaises(ValueError):AdminOperation.model_validate(op).checked()
