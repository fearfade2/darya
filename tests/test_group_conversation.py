import json
import tempfile
import time
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

from telethon import types

from app.dialogs import current_dialog
from app.runtime import Runtime
from app.store import Store
from app.group_conversation import service_error_reply

CHAT=-1000000000123
SELF=50
OWNER=1000000001


class GroupConversationTests(unittest.IsolatedAsyncioTestCase):
    def test_friendly_admin_outage_is_not_treated_as_a_successful_reply(self):
        self.assertTrue(service_error_reply('Сейчас не смогла разобрать команду. Попробуй ещё раз чуть позже.'))
        self.assertTrue(service_error_reply('Сервис модели временно не отвечает. Команда пока не выполнена; повтори её после восстановления связи.'))
        self.assertTrue(service_error_reply('Не удалось создать изображение: API вернул 429'))
        self.assertFalse(service_error_reply('Участник размучен.'))

    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.db=Store(Path(self.tmp.name))
        self.db.update_settings({'owner_id':OWNER,'enabled':True,'group_enabled':True,'typing_enabled':False,
                                 'natural_enabled':False,'online_during_reply':False,'min_interval':0})
        self.rt=Runtime(self.db);self.tg=self.rt.telegram;self.tg.account_id=SELF
        self.tg.ready=Mock(return_value=True);self.tg.client=AsyncMock()
        self.tg.client.get_messages.return_value=[]
        self.tg.receive=Mock(return_value=True)
        self.rt.model.completion=AsyncMock(return_value={'content':'{"reply":true}'})
        self.chat=types.Channel(123,'Общий чат',types.ChatPhotoEmpty(),datetime.now(timezone.utc),megagroup=True)

    async def asyncTearDown(self):
        self.tg.client=None;await self.rt.stop();self.db.close();self.tmp.cleanup()

    def message(self, id, text, sender=10, reply=None, out=False):
        return types.Message(id,types.PeerChannel(123),datetime.now(timezone.utc),text,
            from_id=types.PeerUser(sender),out=out,
            reply_to=types.MessageReplyHeader(reply_to_msg_id=reply) if reply else None)

    def event(self,id,text,sender=10,reply=None,out=False):
        message=self.message(id,text,sender,reply,out)
        return NS(id=id,chat_id=CHAT,raw_text=text,message=message,is_private=False,is_group=True,
            is_reply=bool(reply),mentioned=False,get_chat=AsyncMock(return_value=self.chat),
            get_sender=AsyncMock(return_value=types.User(sender,first_name=f'Участник {sender}',username=f'user{sender}')),
            get_reply_message=AsyncMock(return_value=self.message(reply,'ранняя реплика',SELF) if reply else None))

    async def test_unaddressed_question_routes_by_meaning_and_includes_every_author(self):
        self.db.upsert_contact(CHAT,'group',kind='group')
        self.db.group_message(CHAT,1,'Обсуждаем любимую музыку',sender_id=20,sender_name='Второй')
        await self.tg.on_message(self.event(2,'А ты какую музыку любишь?'))
        self.tg.receive.assert_called_once()
        self.assertEqual(self.tg.receive.call_args.args[:3],(CHAT,'А ты какую музыку любишь?',2))
        self.assertTrue(await self.rt.group_conversation.should_reply(self.event(2,'А ты какую музыку любишь?'),types.User(10)))
        data=json.loads(self.rt.model.completion.call_args.args[0][1]['content'])
        self.assertEqual([row['author_id'] for row in data['history']],[20,10])
        self.assertEqual(data['current_message_id'],2)

    async def test_unrelated_message_is_still_read_and_saved_without_download_or_reply(self):
        self.rt.model.completion.return_value={'content':'{"reply":false}'}
        event=self.event(1,'@user20 я завтра принесу тебе книгу')
        await self.tg.on_message(event)
        self.tg.receive.assert_called_once();self.tg.client.download_media.assert_not_awaited()
        self.assertFalse(await self.rt.group_conversation.should_reply(event,types.User(10)))
        self.assertIn('книгу',self.db.group_history(CHAT)[0]['text'])

    async def test_direct_address_does_not_require_an_extra_model_decision(self):
        await self.tg.on_message(self.event(1,'Даша, привет'))
        self.tg.receive.assert_called_once();self.rt.model.completion.assert_not_awaited()

    async def test_display_name_variants_count_as_direct_addresses_without_a_reply(self):
        for id,name in enumerate(('Дария','Daria'),1):
            with self.subTest(name=name):
                self.tg.receive.reset_mock()
                await self.tg.on_message(self.event(id,name+' привет'))
                self.tg.receive.assert_called_once()
                self.assertTrue(self.tg.receive.call_args.kwargs['group_direct'])

    async def test_normal_reply_top_id_keeps_entire_group_context(self):
        self.db.upsert_contact(CHAT,'group',kind='group')
        self.db.group_message(CHAT,5,'Я офицер',sender_id=20,sender_name='Офицер Ксививайдовичь',username='DelegateTest',topic_id=5)
        now=time.time()
        for topic,user,updated in [(0,10,now-2),(26,20,now-1)]:
            self.db.execute('INSERT INTO group_admin_context VALUES (?,?,?,?,?,?)',(CHAT,topic,OWNER,user,'mute',updated))
            self.db.execute('INSERT INTO group_admin_pending VALUES (?,?,?,?,?,?,?,?)',
                (CHAT,topic,OWNER,self.db.dialog_scope(CHAT,0)[2],topic+1,None,'замуть офицера',updated))
        event=self.event(73,'А ты помнишь офицера?',reply=72)
        event.message.reply_to=types.MessageReplyHeader(reply_to_msg_id=72,reply_to_top_id=69,forum_topic=False)
        await self.rt.group_conversation.prepare(event,await event.get_sender())
        await self.rt.group_conversation.should_reply(event,await event.get_sender())
        data=json.loads(self.rt.model.completion.call_args.args[0][1]['content'])
        self.assertIn('Офицер Ксививайдовичь',str(data['history']))
        self.assertEqual({row['topic_id'] for row in data['history']},{0})
        contexts=self.db.rows('SELECT topic_id,user_id FROM group_admin_context')
        self.assertEqual(contexts,[{'topic_id':0,'user_id':20}])
        self.assertEqual(self.db.rows('SELECT topic_id,message_id FROM group_admin_pending'),[{'topic_id':0,'message_id':27}])

    async def test_real_forum_topics_remain_separate(self):
        self.chat.forum=True
        self.db.upsert_contact(CHAT,'forum',kind='group')
        for topic in (5,26):
            self.db.group_message(CHAT,topic,'Тема '+str(topic),sender_id=20,topic_id=topic)
        event=self.event(73,'Как думаешь?',reply=72)
        event.message.reply_to=types.MessageReplyHeader(reply_to_msg_id=72,reply_to_top_id=26,forum_topic=True)
        await self.rt.group_conversation.prepare(event,await event.get_sender())
        await self.rt.group_conversation.should_reply(event,await event.get_sender())
        data=json.loads(self.rt.model.completion.call_args.args[0][1]['content'])
        self.assertEqual({row['topic_id'] for row in data['history']},{26})
        self.assertEqual({row['topic_id'] for row in self.db.group_history(CHAT)},{5,26})

    async def test_participant_names_do_not_come_from_private_or_archived_context(self):
        self.db.upsert_contact(CHAT,'group',kind='group')
        self.db.upsert_contact(20,'PRIVATE_ALIAS')
        self.db.group_message(CHAT,1,'old',sender_id=30,sender_name='OLD_ALIAS')
        self.db.archive_contexts()
        self.db.group_message(CHAT,2,'hi',sender_id=20,sender_name='Офицер',username='DelegateTest')
        self.db.group_message(CHAT-1,3,'hi',sender_id=40,sender_name='OTHER_GROUP_ALIAS')
        self.assertEqual(self.db.group_participants(CHAT),[{'user_id':20,'names':['Офицер'],'usernames':['DelegateTest']}])

    async def test_history_backfill_and_reply_chain_are_scoped_to_the_current_group(self):
        original=self.message(5,'забань @example',OWNER)
        confirmed=self.message(6,'Выполнено: участник заблокирован.',SELF,5,True)
        unrelated=types.Message(4,types.PeerUser(10),datetime.now(timezone.utc),'PRIVATE_SECRET',from_id=types.PeerUser(10))
        async def get(chat,**kwargs):
            self.assertEqual(chat,CHAT)
            if 'limit' in kwargs:return [confirmed,unrelated]
            return original if kwargs['ids']==5 else None
        self.tg.client.get_messages.side_effect=get
        event=self.event(7,'скинь ему ссылку',20,6)
        await self.rt.group_conversation.prepare(event,await event.get_sender())
        rows=self.db.group_chain(CHAT,6)
        self.assertEqual([row['telegram_id'] for row in rows],[6,5])
        self.assertNotIn('PRIVATE_SECRET',str(self.db.group_history(CHAT)))
        self.tg.receive.assert_not_called()

    async def test_shared_group_context_does_not_import_private_memory_or_another_group(self):
        for id,kind in [(10,'private'),(-20,'group'),(CHAT,'group')]:self.db.upsert_contact(id,str(id),kind=kind)
        self.db.message(10,'user','PRIVATE_HISTORY',1,sender_id=10)
        self.db.remember(10,'secret','PRIVATE_MEMORY')
        self.db.message(-20,'user','OTHER_GROUP',1,sender_id=20)
        self.db.message(CHAT,'user','PUBLIC_FIRST',1,sender_id=10,sender_name='Оля')
        self.db.message(CHAT,'user','PUBLIC_SECOND',2,sender_id=20,sender_name='Паша',reply_to_telegram_id=1)
        token=current_dialog.set(self.db.dialog_scope(CHAT,10))
        try:context=str(self.rt.model.context(CHAT))
        finally:current_dialog.reset(token)
        for public in ['PUBLIC_FIRST','PUBLIC_SECOND','Оля','Паша']:self.assertIn(public,context)
        for secret in ['PRIVATE_HISTORY','PRIVATE_MEMORY','OTHER_GROUP']:self.assertNotIn(secret,context)

    async def test_archive_prevents_loading_old_group_messages_again(self):
        event=self.event(1,'OLD_GROUP_TEXT')
        await self.rt.group_conversation.prepare(event,await event.get_sender())
        self.db.archive_contexts()
        self.tg.client.get_messages.return_value=[event.message]
        latest=self.event(2,'NEW_GROUP_TEXT')
        await self.rt.group_conversation.prepare(latest,await latest.get_sender())
        self.assertEqual([row['text'] for row in self.db.group_history(CHAT)],['NEW_GROUP_TEXT'])

    async def test_own_outgoing_message_is_context_and_does_not_start_a_reply_loop(self):
        self.db.upsert_contact(CHAT,'group',kind='group')
        await self.tg.on_group_outgoing(self.event(3,'Мой ответ',SELF,1,True))
        self.assertTrue(self.db.group_history(CHAT)[0]['outgoing'])
        self.tg.receive.assert_not_called();self.rt.model.completion.assert_not_awaited()

    async def test_disabled_automatic_participation_retains_reading_and_explicit_replies(self):
        self.db.update_settings({'group_auto_reply_enabled':False})
        await self.tg.on_message(self.event(1,'Общая беседа'))
        self.assertEqual(len(self.db.group_history(CHAT)),1)
        self.rt.model.completion.assert_not_awaited();self.tg.receive.assert_not_called()
        await self.tg.on_message(self.event(2,'Даша, привет'))
        self.tg.receive.assert_called_once()

    async def test_blocked_sender_is_read_but_does_not_receive_an_automatic_reply(self):
        self.db.execute('INSERT INTO contact_blocks VALUES (?,?,?,?,?,?,?)',(10,'blocked','insults',10,1,time.time(),time.time()))
        await self.tg.on_message(self.event(2,'Почему ты молчишь?'))
        self.assertTrue(self.db.group_history(CHAT))
        self.tg.receive.assert_not_called();self.rt.model.completion.assert_not_awaited()

    async def test_invalid_decision_never_runs_tools_or_discards_group_context(self):
        self.rt.model.completion.return_value={'content':'{"reply":"true"}','tool_calls':[{'name':'ban'}]}
        event=self.event(1,'Поболтаем?')
        await self.tg.on_message(event)
        self.assertFalse(await self.rt.group_conversation.should_reply(event,types.User(10)))
        self.assertTrue(self.db.group_history(CHAT))
