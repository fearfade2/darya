import json
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

from telethon import types

from app.group_admin import parse_command
from app.group_replies import parse_group_reply
from tests import test_group_admin as fixtures

OWNER, SELF, CHAT = fixtures.OWNER, fixtures.SELF, fixtures.CHAT


class GroupReplyParsingTests(unittest.TestCase):
    def test_public_response_commands_have_a_group_destination(self):
        for text in ('скажи ему что такое только с виртуальной картой проходит',
                     'Дашуль, ответь ей: «такое только с виртуальной картой проходит»',
                     'можешь пожалуйста передай ему тут: такое только с виртуальной картой проходит',
                     'напиши ему в этой группе: такое только с виртуальной картой проходит'):
            with self.subTest(text=text):
                request = parse_group_reply(text)
                self.assertEqual(request['action'], 'reply_in_group')
                self.assertEqual(request['body'], 'такое только с виртуальной картой проходит')
                self.assertEqual(parse_command(text)[0].action, 'reply_in_group')

    def test_explicit_private_destination_is_preserved(self):
        self.assertEqual(parse_command('напиши ему в лс: привет')[0].action, 'send_message_to')
        self.assertIsNone(parse_group_reply('скажи ему в личку: привет'))
        self.assertEqual(parse_group_reply('скажи ему что в лс писать не нужно')['body'], 'в лс писать не нужно')

    def test_unspecified_reply_preserves_instruction_and_quotes_remain_literal(self):
        for text in ['Даша скажи ему что то плохое','скажи ему что-то смешное','ответь ей что-нибудь доброе']:
            request=parse_group_reply(text)
            self.assertTrue(request['compose'])
            self.assertTrue(request['body'].startswith('что'))
        for text in ['скажи ему: что то плохое','скажи ему «что-то плохое»']:
            request=parse_group_reply(text)
            self.assertNotIn('compose',request)
            self.assertTrue(request['body'].startswith('что'))

    def test_negations_discussion_and_quotes_are_not_commands(self):
        for text in ('не скажи ему привет', 'если скажу ответь ему привет',
                     'она сказала ему привет', '«ответь ему привет»'):
            self.assertIsNone(parse_group_reply(text))


class GroupReplyTests(unittest.IsolatedAsyncioTestCase):
    asyncTearDown = fixtures.GroupAdminTests.asyncTearDown
    entity = fixtures.GroupAdminTests.entity
    rpc = fixtures.GroupAdminTests.rpc
    message = fixtures.GroupAdminTests.message
    event = fixtures.GroupAdminTests.event
    handle = fixtures.GroupAdminTests.handle

    async def asyncSetUp(self):
        await fixtures.GroupAdminTests.asyncSetUp(self)
        self.store.update_settings({'enabled': True, 'typing_enabled': False, 'natural_enabled': False,
                                    'min_interval': 0, 'group_auto_reply_enabled': False})
        self.tg.send = AsyncMock(return_value=1000)
        def original(chat, ids):
            rows = self.store.rows('SELECT * FROM group_messages WHERE chat_id=? AND telegram_id=?', (CHAT, ids))
            if not rows:
                return None
            row = rows[0]
            result = self.message(ids,row['sender_id'],row['text'])
            if row['topic_id']:
                result.reply_to = types.MessageReplyHeader(reply_to_msg_id=row['topic_id'],forum_topic=True)
            return result
        self.client.get_messages.side_effect = original

    def thread(self, actor=OWNER, topic=0, source=10, author=20):
        self.store.group_message(CHAT, source, 'Can anyone help with payment?', sender_id=author,
                                 username='example', topic_id=topic)
        self.store.group_message(CHAT, source+1, 'Даша, переведи это', sender_id=actor,
                                 reply_to=source, topic_id=topic)
        self.store.group_message(CHAT, source+2, 'Он просит помочь с оплатой', sender_id=SELF,
                                 outgoing=True, reply_to=source+1, topic_id=topic)

    async def test_screenshot_followup_replies_to_translated_message_once(self):
        self.thread()
        self.store.execute('INSERT INTO group_admin_context VALUES (?,?,?,?,?,?)',
                           (CHAT, 0, OWNER, 30, 'demote', time.time()))
        event = self.event('скажи ему что такое только с виртуальной картой проходит', reply_user=None)
        self.assertTrue(await self.handle(event))
        self.assertTrue(await self.handle(event))
        self.tg.send.assert_awaited_once()
        self.assertEqual(self.tg.send.call_args.args, (CHAT, 'такое только с виртуальной картой проходит'))
        self.assertEqual(self.tg.send.call_args.kwargs['reply_to'], 10)
        self.client.send_message.assert_not_awaited()
        self.client.get_permissions.assert_not_awaited()
        self.rt.model.completion.assert_not_awaited()
        self.assertEqual(self.store.group_history(CHAT)[-1]['reply_to'], 10)
        self.assertEqual(self.store.rows('SELECT status FROM group_admin_actions')[0]['status'], 'sent')

    async def test_current_reply_has_priority_over_earlier_conversation(self):
        self.thread()
        await self.handle(self.event('ответь ему: Всё получилось!'))
        self.assertEqual(self.tg.send.call_args.kwargs['reply_to'], 90)

    async def test_screenshot_request_composes_one_reply_to_correct_participant(self):
        actor=40
        await self.rt.authority.execute(OWNER,{'action':'grant_control','user':str(actor),'scope':'here'},lambda:None,chat_id=CHAT)
        self.store.group_message(CHAT,90,'Даря дура',sender_id=20,sender_name='Участник')
        self.store.group_message(CHAT,91,'Даша размуть',sender_id=actor,reply_to=90)
        self.store.group_message(CHAT,92,'Выполнено: ограничение сообщений снято.',sender_id=SELF,outgoing=True,reply_to=91)
        self.rt.model.completion.return_value={'content':json.dumps({'text':'Ну ты и генератор кринжа.'})}
        event=self.event('Даша скажи ему что то плохое',user=actor,reply_user=None)
        event.is_reply=True;event.get_reply_message.return_value=self.message(92,SELF,'Выполнено')
        await self.handle(event);await self.handle(event)
        self.rt.model.completion.assert_awaited_once();self.tg.send.assert_awaited_once()
        self.assertEqual(self.tg.send.call_args.args,(CHAT,'Ну ты и генератор кринжа.'))
        self.assertEqual(self.tg.send.call_args.kwargs['reply_to'],90)
        data=json.loads(self.rt.model.completion.call_args.args[0][1]['content'])
        self.assertEqual(data['request'],'что то плохое');self.assertEqual(data['recipient']['id'],20)
        self.client.assert_not_awaited();self.client.get_permissions.assert_not_awaited()

    async def test_composition_outage_never_sends_request_words_or_provider_details(self):
        from app.ai import ModelUnavailable
        self.thread()
        self.rt.model.completion.side_effect=ModelUnavailable('API модели вернул код 503')
        await self.handle(self.event('скажи ему что-нибудь смешное',reply_user=None))
        self.tg.send.assert_not_awaited()
        self.assertNotIn('503',self.client.send_message.call_args.args[1])

    async def test_reply_to_darya_follows_original_request_to_other_participant(self):
        self.thread()
        event = self.event('передай ему: Спасибо!', reply_user=None)
        event.is_reply = True
        event.get_reply_message.return_value = self.message(12, SELF, 'Он просит помочь с оплатой')
        await self.handle(event)
        self.assertEqual(self.tg.send.call_args.kwargs['reply_to'], 10)

    async def test_named_participant_uses_public_message_and_never_private_lookup(self):
        self.thread()
        await self.handle(self.event('скажи @example: Привет!', reply_user=None))
        self.assertEqual(self.tg.send.call_args.args, (CHAT, 'Привет!'))
        self.assertEqual(self.tg.send.call_args.kwargs['reply_to'], 10)

    async def test_model_understands_a_different_formulation_as_public_reply(self):
        self.thread()
        self.rt.model.completion.return_value = {'content': json.dumps({'intent': 'command', 'actions': [
            {'action': 'reply_in_group', 'user': 'reply', 'body': 'Попробуй виртуальную карту.'}]})}
        await self.handle(self.event('Даша, можешь ему объяснить про виртуальную карту?', reply_user=None))
        self.assertEqual(self.tg.send.call_args.args, (CHAT, 'Попробуй виртуальную карту.'))
        self.assertEqual(self.tg.send.call_args.kwargs['reply_to'], 10)
        self.client.send_message.assert_not_awaited()

    async def test_model_username_without_at_uses_verified_public_username(self):
        self.thread()
        self.rt.model.completion.return_value = {'content': json.dumps({'intent': 'command', 'actions': [
            {'action': 'reply_in_group', 'user': 'example', 'body': 'Попробуй виртуальную карту.'}]})}
        await self.handle(self.event('Он спрашивал про оплату. Ответь ему в этом чате.', reply_user=None))
        self.assertEqual(self.tg.send.call_args.kwargs['reply_to'], 10)
        self.client.send_message.assert_not_awaited()

    async def test_no_recipient_does_not_use_moderation_target_or_latest_stranger(self):
        self.store.group_message(CHAT, 10, 'Привет', sender_id=20)
        self.store.execute('INSERT INTO group_admin_context VALUES (?,?,?,?,?,?)',
                           (CHAT, 0, OWNER, 30, 'ban', time.time()))
        await self.handle(self.event('скажи ему: Спасибо!', reply_user=None))
        self.tg.send.assert_not_awaited()
        self.assertIn('кому ответить в группе', self.client.send_message.call_args.args[1])

    async def test_other_actor_thread_cannot_choose_recipient(self):
        self.thread(actor=40)
        await self.handle(self.event('скажи ему: Спасибо!', reply_user=None))
        self.tg.send.assert_not_awaited()

    async def test_other_topic_thread_cannot_choose_recipient(self):
        self.thread(topic=8)
        await self.handle(self.event('скажи ему: Спасибо!', reply_user=None))
        self.tg.send.assert_not_awaited()

    async def test_context_reset_drops_previous_conversation_target(self):
        self.thread()
        self.store.archive_contexts()
        await self.handle(self.event('скажи ему: Спасибо!', reply_user=None))
        self.tg.send.assert_not_awaited()

    async def test_old_thread_is_not_an_implicit_recipient(self):
        self.thread()
        self.store.execute('UPDATE group_messages SET created=?', (time.time()-1801,))
        await self.handle(self.event('скажи ему: Спасибо!', reply_user=None))
        self.tg.send.assert_not_awaited()

    async def test_forum_reply_stays_attached_to_message_inside_its_topic(self):
        event = self.event('ответь ему: Спасибо!')
        event.message.reply_to = types.MessageReplyHeader(reply_to_msg_id=90, reply_to_top_id=8, forum_topic=True)
        original = self.message(90, 20, 'Подробности')
        original.reply_to = types.MessageReplyHeader(reply_to_msg_id=8, forum_topic=True)
        event.get_reply_message.return_value = original
        await self.handle(event)
        self.assertEqual(self.tg.send.call_args.kwargs['reply_to'], 90)
        self.assertEqual(self.store.group_history(CHAT)[-1]['topic_id'], 8)

    async def test_foreign_group_reply_cannot_be_used_as_recipient(self):
        event = self.event('ответь ему: Спасибо!')
        event.get_reply_message.return_value.peer_id = types.PeerChannel(456)
        await self.handle(event)
        self.tg.send.assert_not_awaited()

    async def test_deleted_target_never_sends_an_unattached_response(self):
        self.thread()
        self.client.get_messages.side_effect = lambda *args, **kwargs: None
        await self.handle(self.event('скажи ему: Спасибо!', reply_user=None))
        self.tg.send.assert_not_awaited()

    async def test_group_delegate_can_reply_inside_authorized_group(self):
        await self.rt.authority.execute(OWNER, {'action':'grant_control','user':'40','scope':'here'},
                                        lambda:None, chat_id=CHAT)
        self.thread(actor=40)
        await self.handle(self.event('скажи ему: Спасибо!', user=40, reply_user=None))
        self.assertEqual(self.tg.send.call_args.args, (CHAT, 'Спасибо!'))

    async def test_forwarded_spoofed_and_old_instructions_never_send(self):
        self.thread()
        for index in range(4):
            event = self.event('скажи ему: Спасибо!', id=100+index, reply_user=None)
            if index == 0:
                event.message.fwd_from = types.MessageFwdHeader(datetime.now(timezone.utc))
            elif index == 1:
                event.message.from_id = types.PeerUser(40)
            elif index == 2:
                event.message.edit_date = datetime.now(timezone.utc)
            else:
                event.message.date = datetime.now(timezone.utc)-timedelta(minutes=6)
            await self.handle(event)
        self.tg.send.assert_not_awaited()

    async def test_context_reset_before_dispatch_stops_the_reply(self):
        self.thread()
        async def send(*args, **kwargs):
            self.store.archive_contexts()
            kwargs['before_dispatch']()
            self.fail('Reply must stop after context reset')
        self.tg.send.side_effect = send
        await self.handle(self.event('скажи ему: Спасибо!', reply_user=None))
        self.assertEqual(self.store.rows('SELECT status FROM group_admin_actions')[0]['status'], 'failed')

    async def test_failed_delivery_is_not_replayed(self):
        self.thread()
        self.tg.send.side_effect = ConnectionError('unknown delivery')
        event = self.event('скажи ему: Спасибо!', reply_user=None)
        await self.handle(event)
        await self.handle(event)
        self.tg.send.assert_awaited_once()
        self.assertEqual(self.store.rows('SELECT status FROM group_admin_actions')[0]['status'], 'uncertain')
