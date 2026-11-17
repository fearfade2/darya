import json
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

from telethon import errors, types

from app.direct_messages import parse_direct_message, pending_message
from app.group_admin import AdminOperation
from app.store import Store
from tests import test_group_admin as fixtures

OWNER, SELF, CHAT = fixtures.OWNER, fixtures.SELF, fixtures.CHAT


class DirectMessageParsingTests(unittest.TestCase):
    def test_common_commands_preserve_exact_message_body(self):
        for text in ('Даша, напиши @DelegateTest в лс: ПрИвет!\nВторая строка.',
                     'напиши в личку @DelegateTest: ПрИвет!\nВторая строка.',
                     'а теперь отправь сообщение @DelegateTest: «ПрИвет!\nВторая строка.»'):
            with self.subTest(text=text):
                request = parse_direct_message(text)
                self.assertEqual(request.user,'@DelegateTest')
                self.assertEqual(request.body,'ПрИвет!\nВторая строка.')
        request = parse_direct_message('а теперь спамь ему в лс')
        self.assertTrue(request.repeated)
        self.assertIsNone(request.body)

    def test_discussion_negation_and_quoted_examples_do_not_parse(self):
        for text in ('не напиши @DelegateTest в лс: привет', 'почему не пишет ему в лс',
                     'если я скажу напиши @DelegateTest привет', '«напиши @DelegateTest привет»',
                     'она написала: напиши @DelegateTest привет', 'привет @DelegateTest'):
            with self.subTest(text=text):self.assertIsNone(parse_direct_message(text))

    def test_body_limit_does_not_change_admin_title_limit(self):
        AdminOperation(action='send_message_to',user='10',body='я'*4000).checked()
        for data in ({'action':'send_message_to','user':'10','body':'я'*4001},
                     {'action':'title','text':'я'*256}, {'action':'send_message_to','user':'10','minutes':5}):
            with self.subTest(data=data), self.assertRaises(ValueError):AdminOperation(**data).checked()


class GroupDirectMessageTests(unittest.IsolatedAsyncioTestCase):
    asyncTearDown = fixtures.GroupAdminTests.asyncTearDown
    entity = fixtures.GroupAdminTests.entity
    rpc = fixtures.GroupAdminTests.rpc
    message = fixtures.GroupAdminTests.message
    event = fixtures.GroupAdminTests.event
    handle = fixtures.GroupAdminTests.handle

    async def asyncSetUp(self):
        await fixtures.GroupAdminTests.asyncSetUp(self)
        self.store.update_settings({'typing_enabled':False})
        self.tg.send = AsyncMock(return_value=1000)

    async def grant(self, user=20, scope='global'):
        await self.rt.authority.execute(OWNER,{'action':'grant_control','user':str(user),'scope':scope},lambda:None,chat_id=CHAT)
        self.client.reset_mock()

    async def test_clear_dm_works_without_admin_rights_and_is_deduplicated(self):
        self.client.get_permissions.side_effect = lambda *args:self.member
        event = self.event('напиши @DelegateTest в лс: Привет!',reply_user=None)
        self.assertTrue(await self.handle(event))
        await self.handle(event)
        self.tg.send.assert_awaited_once()
        self.assertEqual(self.tg.send.call_args.args,(10,'Привет!'))
        self.client.get_permissions.assert_not_awaited()
        self.rt.model.completion.assert_not_awaited()
        self.assertIn('Одно сообщение отправлено',self.client.send_message.call_args.args[1])
        self.assertEqual(self.store.history(10)[0]['kind'],'owner_command')

    async def test_screenshot_gets_missing_text_question_with_frozen_recent_user(self):
        self.store.execute('INSERT INTO group_admin_context VALUES (?,?,?,?,?,?)',
            (CHAT,0,OWNER,1000000003,'demote',time.time()))
        event = self.event('а теперь спамь ему в лс',reply_user=None)
        await self.handle(event)
        self.tg.send.assert_not_awaited()
        self.rt.model.completion.assert_not_awaited()
        self.assertIn('текст одного сообщения',self.client.send_message.call_args.args[1])
        self.assertEqual(pending_message(self.admin.pending(CHAT,0)['command']).user,'1000000003')
        self.store.execute('UPDATE group_admin_context SET user_id=30')
        await self.handle(event)
        await self.handle(self.event('текст: Привет!',id=101,reply_user=SELF))
        self.tg.send.assert_awaited_once()
        self.assertEqual(self.tg.send.call_args.args,(1000000003,'Привет!'))

    async def test_reply_selects_user_and_dm_never_requires_permission_to_demote_them(self):
        self.member.is_admin=True
        await self.handle(self.event('напиши ему в лс: Привет!'))
        self.tg.send.assert_awaited_once()
        self.assertEqual(self.tg.send.call_args.args,(10,'Привет!'))
        self.client.get_permissions.assert_not_awaited()

    async def test_missing_recipient_keeps_text_until_explicit_username(self):
        event=self.event('напиши в лс: Привет!',reply_user=None)
        await self.handle(event)
        self.assertIn('Уточни адресата',self.client.send_message.call_args.args[1])
        self.tg.send.assert_not_awaited()
        # Duplicate original messages do not become valid after another action.
        self.store.execute('INSERT INTO group_admin_context VALUES (?,?,?,?,?,?)',(CHAT,0,OWNER,30,'ban',time.time()))
        await self.handle(event)
        self.tg.send.assert_not_awaited()
        await self.handle(self.event('@DelegateTest',id=101,reply_user=None))
        self.tg.send.assert_awaited_once()
        self.assertEqual(self.tg.send.call_args.args,(10,'Привет!'))

    async def test_unknown_name_does_not_use_unrelated_recent_target(self):
        self.store.execute('INSERT INTO group_admin_context VALUES (?,?,?,?,?,?)',(CHAT,0,OWNER,30,'ban',time.time()))
        await self.handle(self.event('напиши офицеру в лс: Привет!',reply_user=None))
        self.tg.send.assert_not_awaited()
        self.assertIn('Уточни адресата',self.client.send_message.call_args.args[1])

    async def test_pending_cannot_be_completed_by_ordinary_chat_or_model_invention(self):
        await self.handle(self.event('напиши @DelegateTest в лс',reply_user=None))
        self.rt.model.completion.return_value={'content':json.dumps({'intent':'command','actions':[
            {'action':'send_message_to','user':'@DelegateTest','body':'Выдуманный текст'}],'continue_pending':True})}
        await self.handle(self.event('как дела?',id=101,reply_user=None))
        self.tg.send.assert_not_awaited()
        self.assertIsNotNone(self.admin.pending(CHAT,0))

    async def test_explicit_cancel_removes_pending(self):
        await self.handle(self.event('напиши @DelegateTest в лс',reply_user=None))
        await self.handle(self.event('не отправляй',id=101,reply_user=None))
        self.assertIsNone(self.admin.pending(CHAT,0))
        self.tg.send.assert_not_awaited()

    async def test_group_delegate_cannot_send_dm_but_global_delegate_can(self):
        await self.grant(scope='here')
        await self.handle(self.event('напиши @DelegateTest в лс: Привет!',user=20,reply_user=None))
        self.tg.send.assert_not_awaited()
        self.assertIn('только в этой группе',self.client.send_message.call_args.args[1])
        await self.grant()
        await self.handle(self.event('напиши @DelegateTest в лс: Привет!',user=20,id=101,reply_user=None))
        self.tg.send.assert_awaited_once()

    async def test_pending_is_isolated_by_actor(self):
        await self.grant()
        await self.handle(self.event('напиши @DelegateTest в лс',user=20,reply_user=None))
        await self.handle(self.event('текст: Привет!',user=OWNER,id=101,reply_user=None))
        self.tg.send.assert_not_awaited()
        await self.handle(self.event('текст: Привет!',user=20,id=102,reply_user=None))
        self.tg.send.assert_awaited_once()

    async def test_unauthorized_forwarded_edited_and_spoofed_commands_never_send(self):
        events=[self.event('напиши @DelegateTest в лс: Привет!',user=20),self.event('напиши @DelegateTest в лс: Привет!'),
                self.event('напиши @DelegateTest в лс: Привет!'),self.event('напиши @DelegateTest в лс: Привет!')]
        events[1].message.fwd_from=types.MessageFwdHeader(datetime.now(timezone.utc))
        events[2].message.edit_date=datetime.now(timezone.utc)
        events[3].message.from_id=types.PeerUser(20)
        for event in events:await self.handle(event)
        self.tg.send.assert_not_awaited()

    async def test_old_command_is_not_replayed(self):
        event=self.event('напиши @DelegateTest в лс: Привет!')
        event.message.date=datetime.now(timezone.utc)-timedelta(minutes=6)
        await self.handle(event)
        self.tg.send.assert_not_awaited()

    async def test_disabled_and_blocked_contacts_never_send(self):
        self.store.upsert_contact(10,'User')
        self.store.execute('UPDATE contacts SET enabled=0 WHERE id=10')
        await self.handle(self.event('напиши @DelegateTest в лс: Привет!'))
        self.assertIn('отключена',self.client.send_message.call_args.args[1])
        self.store.execute('UPDATE contacts SET enabled=1 WHERE id=10')
        self.store.execute('INSERT INTO contact_blocks VALUES (?,?,?,?,?,?,?)',(10,'blocked','harassment',10,1,0,0))
        await self.handle(self.event('напиши @DelegateTest в лс: Привет!',id=101))
        self.assertIn('заблокирован',self.client.send_message.call_args.args[1])
        self.tg.send.assert_not_awaited()

    async def test_rate_limit_is_reported_without_claiming_delivery(self):
        self.store.upsert_contact(10,'User')
        self.store.update_settings({'daily_contact_limit':1})
        self.store.message(10,'assistant','already sent',50)
        await self.handle(self.event('напиши @DelegateTest в лс: Привет!'))
        self.assertIn('Дневной лимит',self.client.send_message.call_args.args[1])
        self.tg.send.assert_not_awaited()

    async def test_revocation_during_lookup_prevents_dispatch(self):
        await self.grant()
        async def resolve(value):
            await self.rt.authority.execute(OWNER,{'action':'revoke_control','user':'20','scope':'global'},lambda:None,chat_id=CHAT)
            return self.store.upsert_contact(10,'User')
        self.tg.resolve=resolve
        await self.handle(self.event('напиши @DelegateTest в лс: Привет!',user=20))
        self.tg.send.assert_not_awaited()

    async def test_permission_and_contact_are_rechecked_immediately_before_send(self):
        self.store.upsert_contact(10,'User')
        async def send(contact,body,**kwargs):
            self.store.execute('UPDATE contacts SET enabled=0 WHERE id=10')
            kwargs['before_dispatch']()
            self.fail('Must not dispatch after disable')
        self.tg.send.side_effect=send
        await self.handle(self.event('напиши @DelegateTest в лс: Привет!'))
        self.assertIn('отключена',self.client.send_message.call_args.args[1])
        self.assertEqual(self.store.history(10),[])

    async def test_telegram_failure_or_unknown_result_is_never_claimed_or_retried(self):
        for index,error in enumerate((errors.PeerFloodError(None),ConnectionError('private details'))):
            self.tg.send.side_effect=error
            event=self.event('напиши @DelegateTest в лс: Привет!',id=100+index)
            await self.handle(event)
            await self.handle(event)
            self.assertNotIn('Одно сообщение отправлено',self.client.send_message.call_args.args[1])
        self.assertEqual(self.tg.send.await_count,2)
        self.assertEqual(self.store.history(10),[])

    async def test_spam_text_and_count_do_not_start_repeated_sends(self):
        await self.handle(self.event('спамь @DelegateTest в лс: Привет 100 раз'))
        self.tg.send.assert_not_awaited()
        await self.handle(self.event('100 раз',id=101,reply_user=None))
        self.tg.send.assert_not_awaited()
        await self.handle(self.event('текст: Привет!',id=102,reply_user=None))
        self.tg.send.assert_awaited_once()

    async def test_send_result_requires_positive_telegram_message_id(self):
        self.tg.send.return_value=None
        await self.handle(self.event('напиши @DelegateTest в лс: Привет!'))
        self.assertIn('не подтвердил',self.client.send_message.call_args.args[1])
        self.assertEqual(self.store.rows('SELECT status FROM group_admin_actions')[0]['status'],'uncertain')

    async def test_dm_never_sends_to_group_entity(self):
        self.client.get_entity.side_effect=lambda value:self.chat
        await self.handle(self.event('напиши @DelegateTest в лс: Привет!'))
        self.tg.send.assert_not_awaited()
        self.assertIn('личный аккаунт',self.client.send_message.call_args.args[1])

    async def test_pending_and_dedup_survive_restart(self):
        await self.handle(self.event('напиши @DelegateTest в лс',reply_user=None))
        reopened=Store(self.store.directory)
        try:
            self.assertEqual(pending_message(reopened.rows('SELECT command FROM group_admin_pending')[0]['command']).user,'@DelegateTest')
            self.assertEqual(reopened.rows('SELECT status FROM group_admin_actions')[0]['status'],'waiting')
        finally:reopened.close()
