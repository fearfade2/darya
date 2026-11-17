import asyncio
import json
import time
import unittest
from unittest.mock import AsyncMock, Mock

from telethon import types
from app.ai import ModelUnavailable

from app.dialogs import current_dialog
from tests import test_group_conversation as conversation
from tests import test_group_admin as administration

CHAT, SELF = conversation.CHAT, conversation.SELF


class GroupCadenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_direct_greeting_from_new_author_is_not_suppressed_by_another_authors_reply(self):
        self.rt.model.completion.return_value={'content':'{"reply":false}'}
        self.db.group_message(CHAT,1,'Вопрос другого участника',sender_id=20)
        self.db.group_message(CHAT,2,'Ответ другому участнику',sender_id=SELF,outgoing=True,
                              reply_to=1,created=time.time()-28)
        self.receive(3,'Дария привет',author=10,direct=True)
        await self.finish()
        self.rt.model.completion.assert_not_awaited()
        self.rt.model.run.assert_awaited_once();self.tg.send.assert_awaited_once()

    async def test_repeat_greeting_from_same_author_is_debounced_without_a_model_call(self):
        self.rt.model.completion.return_value={'content':'{"reply":true}'}
        self.db.group_message(CHAT,1,'Даша привет',sender_id=10)
        self.db.group_message(CHAT,2,'Привет!',sender_id=SELF,outgoing=True,reply_to=1)
        self.receive(3,'Даша привет',author=10,direct=True)
        await self.finish()
        self.rt.model.completion.assert_not_awaited()
        self.rt.model.run.assert_not_awaited();self.tg.send.assert_not_awaited()

    async def test_direct_greeting_to_another_person_does_not_force_a_reply(self):
        self.rt.model.completion.return_value={'content':'{"reply":false}'}
        self.receive(1,'Привет Аля',direct=True)
        await self.finish()
        self.rt.model.completion.assert_awaited_once()
        self.rt.model.run.assert_not_awaited();self.tg.send.assert_not_awaited()

    async def test_direct_identity_question_gets_one_reply_without_a_participation_call(self):
        self.rt.model.completion.side_effect=ModelUnavailable('The participation provider is unavailable')
        self.db.group_message(CHAT,1,'Сервис модели временно не отвечает.',sender_id=SELF,outgoing=True)
        self.receive(2,'ты кто',direct=True)
        await self.finish()
        self.rt.model.completion.assert_not_awaited()
        self.rt.model.run.assert_awaited_once();self.tg.send.assert_awaited_once()

    async def test_identity_question_to_other_people_does_not_force_a_reply(self):
        self.rt.model.completion.return_value={'content':'{"reply":false}'}
        self.receive(1,'ты кто',direct=False)
        await self.finish()
        self.rt.model.completion.assert_awaited_once();self.rt.model.run.assert_not_awaited()
        self.tg.send.assert_not_awaited()

    asyncTearDown = conversation.GroupConversationTests.asyncTearDown
    event = conversation.GroupConversationTests.event
    message = conversation.GroupConversationTests.message

    async def asyncSetUp(self):
        await conversation.GroupConversationTests.asyncSetUp(self)
        self.db.update_settings({'group_burst_window':0.025,'min_interval':0})
        self.db.upsert_contact(CHAT,'Group',kind='group')
        self.tg.receive=self.rt.receive
        self.tg.send=AsyncMock(side_effect=range(1000,1100))
        self.rt.model.run=AsyncMock(return_value='Один ответ')

    async def finish(self):
        while self.rt.work:
            results=await asyncio.gather(*list(self.rt.work),return_exceptions=True)
            for result in results:
                if isinstance(result,BaseException) and not isinstance(result,asyncio.CancelledError):
                    raise result

    def receive(self,id,text,author=10,topic=0,direct=False):
        if topic:
            self.db.group_message(CHAT,id,text,sender_id=author,topic_id=topic)
        return self.rt.receive(CHAT,text,id,sender_id=author,sender_name=str(author),group_direct=direct)

    async def test_direct_question_burst_gets_one_reply_without_a_routing_model(self):
        self.receive(1,'Даша, у меня вопрос',direct=True)
        self.receive(2,'Как собрать проект?')
        await self.finish()
        self.rt.model.completion.assert_not_awaited()
        self.rt.model.run.assert_awaited_once()
        self.tg.send.assert_awaited_once()
        self.assertEqual(self.tg.send.call_args.kwargs['reply_to'],2)
        self.assertEqual(self.db.rows("SELECT reply_to_telegram_id FROM messages WHERE role='assistant'")[0]['reply_to_telegram_id'],2)

    async def test_ordinary_callback_collects_messages_before_model_decision(self):
        await self.tg.on_message(self.event(1,'Даша, как собрать'))
        await self.tg.on_message(self.event(2,'этот проект?'))
        self.rt.model.completion.assert_not_awaited()
        await self.finish()
        self.rt.model.run.assert_awaited_once()
        self.tg.send.assert_awaited_once()

    async def test_name_or_reply_does_not_force_answer_to_every_ping(self):
        self.db.group_message(CHAT,1,'Даша ты тут?',sender_id=10)
        self.db.group_message(CHAT,2,'Я тут, только что ответила',sender_id=SELF,outgoing=True,reply_to=1)
        self.rt.model.completion.return_value={'content':'{"reply":false}'}
        self.tg.schedule_reply_typing=Mock()
        self.receive(3,'Даша',direct=True)
        await self.finish()
        self.rt.model.run.assert_not_awaited()
        self.tg.send.assert_not_awaited()
        self.tg.schedule_reply_typing.assert_not_called()

    async def test_fresh_attention_ping_replies_even_after_service_errors_and_debounces_followup(self):
        self.db.group_message(CHAT,1,'API модели вернул код 502. Ошибка шлюза',sender_id=SELF,outgoing=True)
        self.rt.model.completion.side_effect=ValueError('The extra routing model must not run')
        self.receive(2,'Даша але',direct=True)
        await self.finish()
        self.tg.send.assert_awaited_once()
        self.rt.model.run.assert_awaited_once()
        self.receive(1001,'Даша',direct=True)
        await self.finish()
        self.tg.send.assert_awaited_once()
        self.rt.model.completion.assert_not_awaited()

    async def test_attention_ping_and_actual_account_mention_after_a_pause_receive_answers(self):
        self.tg.account_username='darya_bot'
        self.tg.send.side_effect=[1000,2000]
        self.db.group_message(CHAT,1,'Предыдущий ответ',sender_id=SELF,outgoing=True,created=time.time()-60)
        for id,text in [(2,'Даша'),(1001,'@darya_bot')]:
            self.db.execute('UPDATE group_messages SET created=? WHERE chat_id=? AND outgoing=1',(time.time()-60,CHAT))
            self.receive(id,text,direct=True)
            await self.finish()
        self.assertEqual(self.tg.send.await_count,2)
        self.rt.model.completion.assert_not_awaited()

    async def test_recent_answer_followed_only_by_aliases_is_quiet(self):
        self.db.group_message(CHAT,1,'Даша ты тут?',sender_id=10)
        self.db.group_message(CHAT,2,'Уже ответила',sender_id=SELF,outgoing=True,reply_to=1)
        self.receive(3,'Даша',direct=True)
        self.receive(4,'дашка!',direct=True)
        await self.finish()
        self.rt.model.completion.assert_not_awaited()
        self.rt.model.run.assert_not_awaited()
        self.tg.send.assert_not_awaited()

    async def test_another_authors_recent_answer_cannot_swallow_a_new_attention_ping(self):
        self.db.group_message(CHAT,1,'Даша ты тут?',sender_id=20)
        self.db.group_message(CHAT,2,'Я тут',sender_id=SELF,outgoing=True,reply_to=1)
        self.rt.model.completion.side_effect=ModelUnavailable('The routing API is down')
        self.receive(3,'Даша але',author=10,direct=True)
        await self.finish()
        self.rt.model.completion.assert_not_awaited()
        self.rt.model.run.assert_awaited_once()
        self.tg.send.assert_awaited_once()

    async def test_direct_question_cannot_wait_for_or_fail_with_the_routing_provider(self):
        self.rt.model.completion.side_effect=ModelUnavailable('The routing API is down')
        self.receive(1,'Даша, объясни как подключить API',direct=True)
        await self.finish()
        self.rt.model.completion.assert_not_awaited()
        self.rt.model.run.assert_awaited_once()
        self.tg.send.assert_awaited_once()

    async def test_substantial_followup_to_our_reply_does_not_need_routing(self):
        self.db.group_message(CHAT,1,'Мой ответ',sender_id=SELF,outgoing=True)
        self.db.group_message(CHAT,2,'А почему?',sender_id=10,reply_to=1)
        self.rt.model.completion.side_effect=ModelUnavailable('The routing API is down')
        self.assertTrue(await self.rt.group_conversation.should_join(CHAT,10,2,direct=True))
        self.rt.model.completion.assert_not_awaited()

    async def test_single_word_objection_to_our_reply_is_not_silently_dropped(self):
        self.db.group_message(CHAT,1,'Попробуй эту команду',sender_id=SELF,outgoing=True)
        self.db.group_message(CHAT,2,'пиздабол',sender_id=10,reply_to=1)
        self.rt.model.completion.side_effect=ModelUnavailable('The routing provider is down')
        self.assertTrue(await self.rt.group_conversation.should_join(CHAT,10,2,direct=True))
        self.rt.model.completion.assert_not_awaited()

    async def test_named_statement_reaches_answer_generation_without_a_routing_call(self):
        self.rt.model.completion.return_value={'content':'{"reply":false}'}
        self.receive(1,'Даша, это не работает',direct=True)
        await self.finish()
        self.rt.model.completion.assert_not_awaited()
        self.rt.model.run.assert_awaited_once()
        self.tg.send.assert_awaited_once()

    async def test_reply_correction_without_question_mark_reaches_generation(self):
        self.db.group_message(CHAT,1,'Неправильная команда',sender_id=SELF,outgoing=True)
        self.db.group_message(CHAT,2,'неверная команда',sender_id=10,reply_to=1)
        self.rt.model.completion.return_value={'content':'{"reply":false}'}
        self.receive(2,'неверная команда',direct=True)
        await self.finish()
        self.rt.model.completion.assert_not_awaited()
        self.rt.model.run.assert_awaited_once()
        self.tg.send.assert_awaited_once()

    async def test_direct_thanks_can_end_a_conversation_without_a_routing_call(self):
        self.db.group_message(CHAT,1,'Мой ответ',sender_id=SELF,outgoing=True)
        self.db.group_message(CHAT,2,'Спасибо, Даша',sender_id=10,reply_to=1)
        self.rt.model.completion.side_effect=ModelUnavailable('The routing provider is down')
        self.assertFalse(await self.rt.group_conversation.should_join(CHAT,10,2,direct=True))
        self.rt.model.completion.assert_not_awaited()

    async def test_request_to_stay_silent_is_respected_even_if_routing_is_down(self):
        self.db.group_message(CHAT,1,'Мой ответ',sender_id=SELF,outgoing=True)
        self.db.group_message(CHAT,2,'Даша, не отвечай пожалуйста',sender_id=10,reply_to=1)
        self.rt.model.completion.side_effect=ModelUnavailable('The routing provider is down')
        self.assertFalse(await self.rt.group_conversation.should_join(CHAT,10,2,direct=True))
        self.rt.model.completion.assert_not_awaited()

    async def test_reply_with_only_emoji_does_not_force_another_answer(self):
        self.db.group_message(CHAT,1,'Мой ответ',sender_id=SELF,outgoing=True)
        self.db.group_message(CHAT,2,'👍',sender_id=10,reply_to=1)
        self.rt.model.completion.side_effect=ModelUnavailable('The routing provider is down')
        self.assertFalse(await self.rt.group_conversation.should_join(CHAT,10,2,direct=True))
        self.rt.model.completion.assert_not_awaited()

    async def test_explicit_other_name_overrides_a_reply_to_darya(self):
        self.db.group_message(CHAT,1,'Мой ответ',sender_id=SELF,outgoing=True)
        self.db.group_message(CHAT,2,'Моя реплика',sender_id=20,sender_name='Аля')
        self.db.group_message(CHAT,3,'Аля почему ты злая?',sender_id=10,reply_to=1)
        self.rt.model.completion.return_value={'content':'{"reply":false}'}
        self.assertFalse(await self.rt.group_conversation.should_join(CHAT,10,3,direct=True))
        self.rt.model.completion.assert_awaited_once()

    async def test_explicit_other_username_overrides_a_reply_to_darya(self):
        self.tg.account_username='darya_bot'
        self.db.group_message(CHAT,1,'Мой ответ',sender_id=SELF,outgoing=True)
        self.db.group_message(CHAT,2,'@user20 ты врёшь',sender_id=10,reply_to=1)
        self.rt.model.completion.return_value={'content':'{"reply":false}'}
        self.assertFalse(await self.rt.group_conversation.should_join(CHAT,10,2,direct=True))
        self.rt.model.completion.assert_awaited_once()

    async def test_mention_inside_question_to_someone_else_still_uses_semantic_routing(self):
        self.db.group_message(CHAT,1,'Говорим вдвоём',sender_id=20)
        self.db.group_message(CHAT,2,'Как тебе Даша?',sender_id=10,reply_to=1)
        self.rt.model.completion.return_value={'content':'{"reply":false}'}
        self.assertFalse(await self.rt.group_conversation.should_join(CHAT,10,2,direct=True))
        self.rt.model.completion.assert_awaited_once()

    async def test_unrelated_dialogue_is_remembered_without_answer(self):
        self.rt.model.completion.return_value={'content':'{"reply":false}'}
        self.receive(1,'@user20 я отвечал тебе')
        self.receive(2,'@user20 принесу завтра')
        await self.finish()
        self.assertEqual(len(self.db.group_history(CHAT)),2)
        self.rt.model.run.assert_not_awaited()
        self.tg.send.assert_not_awaited()

    async def test_recent_bot_response_and_current_ids_are_in_participation_context(self):
        self.db.group_message(CHAT,1,'Это уже обсуждали',sender_id=SELF,outgoing=True)
        self.receive(2,'И чо',direct=True)
        await self.finish()
        data=json.loads(self.rt.model.completion.call_args.args[0][1]['content'])
        self.assertLess(data['seconds_since_darya_reply'],2)
        self.assertTrue(data['history'][0]['darya'])
        self.assertEqual(data['current_message_ids'],[2])

    async def test_other_authors_have_independent_batches_and_context_ownership(self):
        authors=[]
        async def answer(*args,**kwargs):
            authors.append(self.db.dialog_scope(CHAT)[1])
            return 'Ответ'
        self.rt.model.run.side_effect=answer
        self.receive(1,'Вопрос Оли',10,direct=True)
        self.receive(2,'Вопрос Паши',20,direct=True)
        self.receive(3,'Дополнение Оли',10)
        await self.finish()
        self.assertCountEqual(authors,[10,20])
        self.assertEqual(self.tg.send.await_count,2)
        batches=[json.loads(call.args[0][1]['content'])['current_message_ids'] for call in self.rt.model.completion.await_args_list]
        self.assertCountEqual(batches,[[1,3],[2]])

    async def test_same_author_in_two_topics_gets_replies_to_the_correct_messages(self):
        self.receive(1,'Вопрос в теме A',topic=390,direct=True)
        self.receive(2,'Вопрос в теме B',topic=38,direct=True)
        await self.finish()
        self.assertEqual(self.tg.send.await_count,2)
        self.assertCountEqual([call.kwargs['reply_to'] for call in self.tg.send.await_args_list],[1,2])
        self.assertEqual(self.db.group_topic(CHAT,1000),390)
        self.assertEqual(self.db.group_topic(CHAT,1001),38)

    async def test_new_message_during_generation_replaces_unsent_response(self):
        started=asyncio.Event();resume=asyncio.Event()
        calls=[]
        async def answer(*args,**kwargs):
            calls.append(kwargs['current_from'])
            if len(calls)==1:
                started.set();await resume.wait();return 'Устаревший ответ'
            return 'Учитываю оба сообщения'
        self.rt.model.run.side_effect=answer
        self.receive(1,'Первый вопрос',direct=True)
        await asyncio.wait_for(started.wait(),1)
        self.receive(2,'Важное дополнение')
        resume.set()
        await self.finish()
        self.tg.send.assert_awaited_once()
        self.assertEqual(self.tg.send.call_args.args[1],'Учитываю оба сообщения')
        self.assertEqual(calls[0],calls[1])

    async def test_another_author_cannot_cancel_current_reply(self):
        started=asyncio.Event();resume=asyncio.Event()
        async def answer(*args,**kwargs):
            if self.db.dialog_scope(CHAT)[1]==10:
                started.set();await resume.wait()
            return 'Ответ'
        self.rt.model.run.side_effect=answer
        self.receive(1,'Вопрос Оли',10,direct=True)
        await asyncio.wait_for(started.wait(),1)
        self.receive(2,'Вопрос Паши',20,direct=True)
        resume.set();await self.finish()
        self.assertEqual(self.tg.send.await_count,2)

    async def test_committed_effect_is_not_replayed_after_followup(self):
        started=asyncio.Event();resume=asyncio.Event();count=0
        async def answer(contact,execute,*args,**kwargs):
            nonlocal count
            count+=1
            if count==1:
                await execute('remember',{'key':'hobby','value':'music'})
                started.set();await resume.wait()
            return 'Ответ'
        self.rt.model.run.side_effect=answer
        self.receive(1,'Запомни, люблю музыку',direct=True)
        await asyncio.wait_for(started.wait(),1)
        self.receive(2,'А теперь новый вопрос',direct=True)
        resume.set();await self.finish()
        self.assertEqual(self.tg.send.await_count,2)
        self.assertNotEqual(self.rt.model.run.await_args_list[0].kwargs['current_from'],self.rt.model.run.await_args_list[1].kwargs['current_from'])

    async def test_group_answer_is_one_message_even_with_private_split_setting(self):
        self.db.update_settings({'split_replies':True})
        self.rt.model.run.return_value='Первая мысль\n\nВторая мысль'
        self.receive(1,'Даша, расскажи',direct=True)
        await self.finish()
        self.tg.send.assert_awaited_once()
        self.assertEqual(self.tg.send.call_args.args[1],'Первая мысль\n\nВторая мысль')

    async def test_duplicate_incoming_does_not_start_another_response(self):
        self.assertTrue(self.receive(1,'Первый вопрос',direct=True))
        self.assertFalse(self.receive(1,'Первый вопрос',direct=True))
        await self.finish()
        self.assertFalse(self.receive(1,'Первый вопрос',direct=True))
        self.tg.send.assert_awaited_once()

    async def test_disable_or_archive_during_window_prevents_send(self):
        self.receive(1,'Вопрос',direct=True)
        self.db.execute('UPDATE contacts SET enabled=0 WHERE id=?',(CHAT,))
        await self.finish()
        self.tg.send.assert_not_awaited()
        self.db.execute('UPDATE contacts SET enabled=1 WHERE id=?',(CHAT,))
        self.receive(2,'Новый вопрос',direct=True)
        self.db.archive_contexts()
        await self.finish()
        self.tg.send.assert_not_awaited()

    async def test_invalid_decision_cannot_execute_tools_but_clear_request_has_fallback(self):
        self.rt.model.completion.return_value={'content':'broken'}
        self.receive(1,'Даша, объясни задачу',direct=True)
        await self.finish()
        self.tg.send.assert_awaited_once()
        self.rt.model.completion.return_value={'content':'{"reply":true}','tool_calls':[{'name':'ban'}]}
        self.receive(2,'Разговор между людьми')
        await self.finish()
        self.assertEqual(self.tg.send.await_count,1)

    async def test_explicit_calls_work_when_automatic_participation_disabled(self):
        self.db.update_settings({'group_auto_reply_enabled':False})
        self.receive(1,'Даша, вопрос',direct=True)
        await self.finish()
        self.rt.model.completion.assert_not_awaited()
        self.tg.send.assert_awaited_once()

    async def test_stopping_runtime_discards_waiting_replies(self):
        self.receive(1,'Вопрос',direct=True)
        await self.rt.stop()
        self.tg.send.assert_not_awaited()


class GroupCommandCadenceTests(unittest.IsolatedAsyncioTestCase):
    asyncTearDown = administration.GroupAdminTests.asyncTearDown
    entity = administration.GroupAdminTests.entity
    rpc = administration.GroupAdminTests.rpc
    message = administration.GroupAdminTests.message
    event = administration.GroupAdminTests.event

    async def asyncSetUp(self):
        await administration.GroupAdminTests.asyncSetUp(self)
        self.store.update_settings({'group_burst_window':0.04})

    async def dispatch(self,event):
        return await self.admin.dispatch(event,await event.get_sender())

    async def test_mute_duration_and_ping_are_one_command_and_one_ack(self):
        first=self.event('замуть его')
        task=asyncio.create_task(self.dispatch(first))
        await asyncio.sleep(0.005)
        duration=asyncio.create_task(self.dispatch(self.event('на 5 минут',id=101,reply_user=None)))
        ping=asyncio.create_task(self.dispatch(self.event('даша',id=102,reply_user=None)))
        await asyncio.gather(task,duration,ping)
        self.client.assert_awaited_once()
        request=self.client.call_args.args[0]
        self.assertEqual(request.participant.id,10)
        self.assertAlmostEqual(request.banned_rights.until_date.timestamp()-time.time(),300,delta=2)
        self.client.send_message.assert_awaited_once()
        self.assertNotIn('reply_to',self.client.send_message.call_args.kwargs)
        self.rt.model.completion.assert_not_awaited()

    async def test_followup_from_another_actor_cannot_change_owner_command(self):
        task=asyncio.create_task(self.dispatch(self.event('замуть его')))
        await asyncio.sleep(0.005)
        await self.dispatch(self.event('на 5 минут',user=20,id=101,reply_user=None))
        await task
        self.assertAlmostEqual(self.client.call_args.args[0].banned_rights.until_date.timestamp()-time.time(),600,delta=2)

    async def test_duplicate_command_does_not_bypass_collection_window_or_repeat(self):
        event=self.event('замуть его')
        one=asyncio.create_task(self.dispatch(event));two=asyncio.create_task(self.dispatch(event))
        await asyncio.sleep(0.005)
        self.client.assert_not_awaited()
        duration=asyncio.create_task(self.dispatch(self.event('на 5 минут',id=101,reply_user=None)))
        await asyncio.gather(one,two,duration)
        self.client.assert_awaited_once()

    async def test_revoking_delegate_during_collection_stops_action(self):
        await self.rt.authority.execute(administration.OWNER,{'action':'grant_control','user':'20','scope':'here'},lambda:None,chat_id=administration.CHAT)
        self.client.reset_mock()
        task=asyncio.create_task(self.dispatch(self.event('замуть 10',user=20)))
        await asyncio.sleep(0.005)
        await self.dispatch(self.event('не слушайся 20',id=101,reply_user=None))
        await task
        self.client.assert_not_awaited()

    async def test_forwarded_duration_does_not_extend_trusted_command(self):
        task=asyncio.create_task(self.dispatch(self.event('замуть его')))
        await asyncio.sleep(0.005)
        event=self.event('на 5 минут',id=101,reply_user=None)
        event.message.fwd_from=types.MessageFwdHeader(event.message.date)
        await self.dispatch(event)
        await task
        self.assertAlmostEqual(self.client.call_args.args[0].banned_rights.until_date.timestamp()-time.time(),600,delta=2)

    async def test_late_duration_adjusts_only_recent_mute_by_same_author(self):
        await self.dispatch(self.event('замуть его'))
        self.client.reset_mock()
        await self.dispatch(self.event('на 5 минут',id=101,reply_user=None))
        self.assertEqual(self.client.call_args.args[0].participant.id,10)
        self.assertAlmostEqual(self.client.call_args.args[0].banned_rights.until_date.timestamp()-time.time(),300,delta=2)

    async def test_duration_without_request_returns_to_conversation_without_error(self):
        self.assertFalse(await self.dispatch(self.event('на 5 минут',reply_user=None)))
        self.client.send_message.assert_not_awaited()

    async def test_topic_ack_is_sent_to_topic_root_without_quoting_command(self):
        event=self.event('замуть 10',reply_user=None)
        event.message.reply_to=types.MessageReplyHeader(reply_to_msg_id=390,forum_topic=True)
        await self.dispatch(event)
        self.assertEqual(self.client.send_message.call_args.kwargs['reply_to'],390)
