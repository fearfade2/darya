import json
import unittest

from app.dialogs import current_dialog
from app.group_conversation import group_turn
from tests import test_group_conversation as conversation
from tests import test_group_cadence as cadence

CHAT, SELF = conversation.CHAT, conversation.SELF
MARKER = 'Текущая реплика и её разговор (данные сервера): '


class GroupTurnTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = cadence.GroupCadenceTests.asyncSetUp
    asyncTearDown = conversation.GroupConversationTests.asyncTearDown
    receive = cadence.GroupCadenceTests.receive
    finish = cadence.GroupCadenceTests.finish

    def frame(self, dialogue):
        content = next(row['content'] for row in dialogue if MARKER in row['content'])
        return json.JSONDecoder().raw_decode(content.split(MARKER,1)[1])[0]

    async def test_busy_chat_cannot_push_direct_greeting_out_of_its_processing_window(self):
        self.rt.model.completion.return_value={'content':'{"reply":false}'}
        self.receive(1,'Дария привет',direct=True)
        for id in range(2,150):
            self.db.group_message(CHAT,id,'Поздняя чужая ссора',sender_id=20)
        await self.finish()
        self.rt.model.run.assert_awaited_once()
        self.tg.send.assert_awaited_once()
        self.assertEqual(self.tg.send.call_args.kwargs['reply_to'],1)

    async def test_unaddressed_followup_uses_conversation_before_its_arrival(self):
        self.db.group_message(CHAT,1,'Я люблю музыку',sender_id=SELF,outgoing=True)
        self.db.group_message(CHAT,2,'а почему ты её любишь?',sender_id=10)
        for id in range(3,120):
            self.db.group_message(CHAT,id,'Чужой разговор после вопроса',sender_id=20)
        self.assertTrue(await self.rt.group_conversation.should_join(CHAT,10,2,direct=False))
        data=json.loads(self.rt.model.completion.call_args.args[0][1]['content'])
        self.assertEqual([row['id'] for row in data['history']],[1,2])
        self.assertEqual(data['turn']['recent_darya_messages'][0]['text'],'Я люблю музыку')
        self.assertEqual(data['turn']['current_messages'][0]['author_id'],10)
        self.assertFalse(data['addressed_by_name_mention_or_reply'])

    async def test_malformed_semantic_decision_is_retried_for_an_unaddressed_question(self):
        self.db.group_message(CHAT,1,'Люблю музыку',sender_id=SELF,outgoing=True)
        self.db.group_message(CHAT,2,'почему ты её любишь?',sender_id=10)
        self.rt.model.completion.side_effect=[{'content':''},{'content':'{"reply":true}'}]
        self.assertTrue(await self.rt.group_conversation.should_join(CHAT,10,2,direct=False))
        self.assertEqual(self.rt.model.completion.await_count,2)

    async def test_new_greeting_keeps_its_author_and_old_reply_quote_in_a_busy_group(self):
        self.db.group_message(CHAT,1,'Мур? Я тут, солнце~ Что хотел?',sender_id=SELF,outgoing=True)
        for id in range(2,100):
            self.db.group_message(CHAT,id,'Чужая длинная переписка',sender_id=20,sender_name='Аля')
        first=self.db.message(CHAT,'user','привет',100,sender_id=10,sender_name='Finger',reply_to_telegram_id=1)
        for id in range(101,200):
            self.db.group_message(CHAT,id,'FUTURE_OTHER_CONVERSATION',sender_id=20)
        token=current_dialog.set(self.db.dialog_scope(CHAT,10))
        try:
            dialogue=self.rt.model.context(CHAT,current_from=first,current_to=first)
        finally:
            current_dialog.reset(token)
        frame=self.frame(dialogue)
        self.assertEqual(frame['author_id'],10)
        self.assertEqual(frame['current_messages'][0]['text'],'привет')
        self.assertEqual(frame['current_message_ids'],[100])
        quote=frame['reply_chains'][0]['messages'][-1]
        self.assertTrue(quote['darya'])
        self.assertEqual(quote['id'],1)
        self.assertNotIn('FUTURE_OTHER_CONVERSATION',str(dialogue))

    async def test_old_messages_by_same_author_are_not_new_in_the_current_turn(self):
        self.db.message(CHAT,'user','Старая претензия',1,sender_id=10,sender_name='Finger')
        first=self.db.message(CHAT,'user','привет',2,sender_id=10,sender_name='Finger')
        token=current_dialog.set(self.db.dialog_scope(CHAT,10))
        try:dialogue=self.rt.model.context(CHAT,current_from=first,current_to=first)
        finally:current_dialog.reset(token)
        frame=self.frame(dialogue)
        self.assertEqual([row['text'] for row in frame['current_messages']],['привет'])

    async def test_reply_chain_cannot_import_a_different_forum_topic(self):
        self.db.group_message(CHAT,1,'OTHER_TOPIC_SECRET',sender_id=20,topic_id=38)
        self.db.group_message(CHAT,2,'Ответ в моей теме',sender_id=10,reply_to=1,topic_id=390)
        frame=group_turn(self.db,CHAT,10,2)
        self.assertEqual(frame['reply_chains'],[])
        self.assertNotIn('OTHER_TOPIC_SECRET',str(frame))

    async def test_late_answer_to_previous_turn_still_provides_continuity(self):
        self.db.message(CHAT,'user','Первый вопрос',1,sender_id=20)
        first=self.db.message(CHAT,'user','Новое уточнение',2,sender_id=10)
        self.db.message(CHAT,'assistant','Ответ на первый вопрос уже отправлен',3,reply_to_telegram_id=1)
        token=current_dialog.set(self.db.dialog_scope(CHAT,10))
        try:dialogue=self.rt.model.context(CHAT,current_from=first,current_to=first)
        finally:current_dialog.reset(token)
        self.assertIn('Ответ на первый вопрос уже отправлен',str(dialogue))
        self.assertEqual(self.frame(dialogue)['current_message_ids'],[2])
