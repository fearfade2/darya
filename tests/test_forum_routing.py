import json
import time
import unittest
from unittest.mock import AsyncMock

from telethon import types

from app.dialogs import current_dialog
from app.group_conversation import topic_id, topic_root_reply, reply_message_id
from tests import test_group_conversation as conversation
from tests import test_group_admin as administration

CHAT, SELF = conversation.CHAT, conversation.SELF


class ForumRoutingTests(unittest.IsolatedAsyncioTestCase):
    asyncTearDown = conversation.GroupConversationTests.asyncTearDown
    event = conversation.GroupConversationTests.event
    message = conversation.GroupConversationTests.message

    async def asyncSetUp(self):
        await conversation.GroupConversationTests.asyncSetUp(self)
        self.chat.forum=True

    def forum_event(self,id,text,topic,sender=10,parent=None):
        event=self.event(id,text,sender,reply=parent or topic)
        event.message.reply_to=types.MessageReplyHeader(reply_to_msg_id=parent or topic,
            reply_to_top_id=topic if parent else None,forum_topic=True)
        return event

    async def test_topic_root_is_routing_metadata_and_does_not_reply_to_creator(self):
        event=self.forum_event(500,'Дашка привет',390)
        self.assertEqual(topic_id(event.message),390)
        self.assertTrue(topic_root_reply(event.message))
        self.assertIsNone(reply_message_id(event.message))
        await self.tg.on_message(event)
        event.get_reply_message.assert_not_awaited()
        self.tg.receive.assert_called_once()
        self.assertIsNone(self.tg.receive.call_args.kwargs['reply_to_telegram_id'])
        self.tg.client.get_messages.assert_awaited_once_with(CHAT,limit=100,max_id=500,reply_to=390)
        self.assertEqual(self.db.group_history(CHAT)[0]['topic_id'],390)
        self.assertIsNone(self.db.group_history(CHAT)[0]['reply_to'])

    async def test_normal_message_in_topic_does_not_trigger_reply_to_darya_topic_creator(self):
        self.rt.model.completion.return_value={'content':'{"reply":false}'}
        event=self.forum_event(500,'@user20 это тебе',390)
        await self.tg.on_message(event)
        event.get_reply_message.assert_not_awaited()
        self.tg.receive.assert_called_once()
        self.assertFalse(self.tg.receive.call_args.kwargs['group_direct'])
        self.assertFalse(await self.rt.group_conversation.should_reply(event,types.User(10)))

    async def test_explicit_reply_in_topic_remains_a_real_reply(self):
        event=self.forum_event(502,'Да, согласен',390,parent=501)
        self.assertEqual(reply_message_id(event.message),501)
        self.assertFalse(topic_root_reply(event.message))
        await self.tg.on_message(event)
        event.get_reply_message.assert_awaited_once()
        self.tg.receive.assert_called_once()
        self.assertEqual(self.tg.receive.call_args.kwargs['reply_to_telegram_id'],501)

    async def test_each_topic_loads_its_own_history(self):
        for id,topic in [(500,390),(501,38),(502,390)]:
            event=self.forum_event(id,'Привет',topic)
            await self.rt.group_conversation.prepare(event,await event.get_sender())
        self.assertEqual(self.tg.client.get_messages.await_count,2)
        self.assertEqual([call.kwargs['reply_to'] for call in self.tg.client.get_messages.await_args_list],[390,38])

    async def test_model_keeps_current_request_and_history_in_its_topic(self):
        self.db.upsert_contact(CHAT,'Forum',kind='group')
        rows=[]
        for id,topic,text in [(500,390,'TOPIC_A_CURRENT'),(501,38,'TOPIC_B_LATER')]:
            self.db.group_message(CHAT,id,text,sender_id=10,topic_id=topic)
            rows.append(self.db.message(CHAT,'user',text,id,sender_id=10,sender_name='Автор'))
        self.db.group_message(CHAT,499,'TOPIC_A_PUBLIC',sender_id=20,topic_id=390)
        self.db.group_message(CHAT,498,'TOPIC_B_PUBLIC',sender_id=30,topic_id=38)
        token=current_dialog.set(self.db.dialog_scope(CHAT,10))
        try:
            content=str(self.rt.model.context(CHAT,current_from=rows[0],current_to=rows[0]))
        finally:current_dialog.reset(token)
        self.assertIn('TOPIC_A_CURRENT',content)
        self.assertIn('TOPIC_A_PUBLIC',content)
        self.assertNotIn('TOPIC_B_LATER',content)
        self.assertNotIn('TOPIC_B_PUBLIC',content)

    async def test_outgoing_reply_inherits_topic_before_telegram_outgoing_event(self):
        self.db.upsert_contact(CHAT,'Forum',kind='group')
        self.db.group_message(CHAT,500,'Привет',sender_id=10,topic_id=390)
        self.db.message(CHAT,'assistant','Ответ',501,reply_to_telegram_id=500)
        self.assertEqual(self.db.group_topic(CHAT,501),390)
        self.assertEqual(self.db.group_history(CHAT,topic_id=390)[-1]['reply_to'],500)

    async def test_queued_request_sees_already_sent_bot_response_but_not_later_user_requests(self):
        self.db.upsert_contact(CHAT,'Forum',kind='group')
        self.db.group_message(CHAT,500,'ORIGINAL_REQUEST',sender_id=10,topic_id=390)
        source=self.db.message(CHAT,'user','ORIGINAL_REQUEST',500,sender_id=10)
        self.db.group_message(CHAT,501,'LATER_UNHANDLED_REQUEST',sender_id=10,topic_id=390)
        self.db.message(CHAT,'user','LATER_UNHANDLED_REQUEST',501,sender_id=10)
        self.db.group_message(CHAT,502,'ALREADY_SENT_BOT_RESPONSE',sender_id=SELF,topic_id=390,outgoing=True)
        token=current_dialog.set(self.db.dialog_scope(CHAT,10))
        try:
            content=str(self.rt.model.context(CHAT,current_from=source,current_to=source))
        finally:current_dialog.reset(token)
        self.assertIn('ORIGINAL_REQUEST',content)
        self.assertIn('ALREADY_SENT_BOT_RESPONSE',content)
        self.assertNotIn('LATER_UNHANDLED_REQUEST',content)

    async def test_runtime_replies_to_each_original_message_with_bounded_model_context(self):
        self.db.upsert_contact(CHAT,'Forum',kind='group')
        self.rt.model.run=AsyncMock(return_value='Ответ')
        self.tg.send=AsyncMock(side_effect=[600,601])
        rows=[]
        for id,topic in [(500,390),(501,38)]:
            self.db.group_message(CHAT,id,'Даша привет',sender_id=10,topic_id=topic)
            rows.append(self.db.message(CHAT,'user','Даша привет',id,sender_id=10))
        for index,id in enumerate((500,501)):
            await self.rt.reply(CHAT,telegram_id=id,request_start=rows[index],prepared=True)
        self.assertEqual([call.kwargs['current_to'] for call in self.rt.model.run.await_args_list],rows)
        self.assertEqual([call.kwargs['reply_to'] for call in self.tg.send.await_args_list],[500,501])
        self.assertEqual(self.db.group_topic(CHAT,600),390)
        self.assertEqual(self.db.group_topic(CHAT,601),38)

    async def test_topic_lookup_never_crosses_archive_or_group_boundary(self):
        self.db.upsert_contact(CHAT,'Forum',kind='group')
        self.db.group_message(CHAT,500,'Привет',sender_id=10,topic_id=390)
        self.assertEqual(self.db.group_topic(CHAT-1,500),0)
        self.db.archive_contexts()
        self.assertEqual(self.db.group_topic(CHAT,500),0)


class ForumAdminTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = administration.GroupAdminTests.asyncSetUp
    asyncTearDown = administration.GroupAdminTests.asyncTearDown
    entity = administration.GroupAdminTests.entity
    rpc = administration.GroupAdminTests.rpc
    message = administration.GroupAdminTests.message
    event = administration.GroupAdminTests.event
    handle = administration.GroupAdminTests.handle

    async def test_topic_creator_is_never_implicit_target_of_ban(self):
        event=self.event('забань его',reply_user=20)
        event.message.reply_to=types.MessageReplyHeader(reply_to_msg_id=390,forum_topic=True)
        await self.handle(event)
        event.get_reply_message.assert_not_awaited()
        self.client.assert_not_awaited()
        self.assertIn('Неясно, о ком речь',self.client.send_message.call_args.args[1])

    async def test_pronoun_uses_recent_target_in_same_topic(self):
        event=self.event('забань его',reply_user=20)
        event.message.reply_to=types.MessageReplyHeader(reply_to_msg_id=390,forum_topic=True)
        self.store.execute('INSERT INTO group_admin_context VALUES (?,?,?,?,?,?)',
            (administration.CHAT,390,administration.OWNER,30,'mute',time.time()))
        self.store.execute('INSERT INTO group_admin_context VALUES (?,?,?,?,?,?)',
            (administration.CHAT,38,administration.OWNER,40,'mute',time.time()))
        await self.handle(event)
        self.assertEqual(self.client.call_args.args[0].participant.id,30)

    async def test_dm_clarification_does_not_select_topic_creator(self):
        event=self.event('а теперь спамь ему в лс',reply_user=20)
        event.message.reply_to=types.MessageReplyHeader(reply_to_msg_id=390,forum_topic=True)
        self.tg.send=AsyncMock()
        await self.handle(event)
        self.tg.send.assert_not_awaited()
        self.assertIn('Уточни адресата',self.client.send_message.call_args.args[1])
