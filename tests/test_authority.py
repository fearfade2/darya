import asyncio
import json
import time
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

from telethon import functions, types

from app.authority import authorized, parse_delegation
from app.control import owner_request, controller_request
from app.store import Store
from tests import test_group_admin as fixtures

OWNER, SELF, CHAT = fixtures.OWNER, fixtures.SELF, fixtures.CHAT


class AuthorityTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.GroupAdminTests.asyncSetUp
    asyncTearDown = fixtures.GroupAdminTests.asyncTearDown
    entity = fixtures.GroupAdminTests.entity
    rpc = fixtures.GroupAdminTests.rpc
    message = fixtures.GroupAdminTests.message
    event = fixtures.GroupAdminTests.event
    handle = fixtures.GroupAdminTests.handle

    async def grant(self, user=10, scope='here', chat_id=CHAT):
        return await self.rt.authority.execute(OWNER, {'action': 'grant_control', 'user': str(user), 'scope': scope},
                                               lambda: None, chat_id=chat_id)

    async def revoke(self, user=10, scope='here', chat_id=CHAT):
        return await self.rt.authority.execute(OWNER, {'action': 'revoke_control', 'user': str(user), 'scope': scope},
                                               lambda: None, chat_id=chat_id)

    async def test_screenshot_command_grants_without_promotion_or_admin_right(self):
        self.client.get_permissions.side_effect=lambda *args:self.member
        await self.handle(self.event('теперь ты можешь слушаться его @DelegateTest'))
        self.assertTrue(authorized(self.store,10,CHAT))
        self.assertFalse(authorized(self.store,10))
        self.client.assert_not_awaited()
        self.client.get_permissions.assert_not_awaited()
        self.rt.model.completion.assert_not_awaited()
        self.assertIn('выполняю поручения',self.client.send_message.call_args.args[1])
        await self.handle(self.event('теперь ты можешь слушаться его @DelegateTest'))
        self.client.send_message.assert_awaited_once()

    async def test_grant_persists_and_owner_change_invalidates_it(self):
        await self.grant()
        reopened=Store(self.store.directory)
        try:self.assertTrue(authorized(reopened,10,CHAT))
        finally:reopened.close()
        self.store.update_settings({'owner_id':20})
        self.assertFalse(authorized(self.store,10,CHAT))

    async def test_group_admin_role_alone_is_insufficient(self):
        self.client.get_permissions.side_effect=lambda *args:self.me
        await self.handle(self.event('забань 20',user=10))
        self.client.assert_not_awaited()
        self.assertFalse(authorized(self.store,10,CHAT))

    async def test_delegate_can_issue_group_commands_and_revoke_stops_them(self):
        await self.grant()
        await self.handle(self.event('забань 20',user=10,id=101,reply_user=None))
        self.assertIsInstance(self.client.call_args.args[0], functions.channels.EditBannedRequest)
        self.assertEqual(self.client.call_args.args[0].participant.id,20)
        self.client.reset_mock()
        await self.revoke()
        await self.handle(self.event('забань 20',user=10,id=102,reply_user=None))
        self.client.assert_not_awaited()

    async def test_group_grant_does_not_authorize_other_group_or_private(self):
        await self.grant()
        event=self.event('забань 20',user=10,id=101)
        event.chat_id=CHAT-1
        await self.handle(event)
        self.client.assert_not_awaited()
        self.store.upsert_contact(10,'Delegate')
        self.store.message(10,'user','ответь быстрее',1,sender_id=10)
        self.assertIsNone(controller_request(self.store,10,1))
        with self.assertRaises(ValueError):
            await self.rt.control.action(10,1,'set_reply_speed',{'speed':'fast'},trusted=True)

    async def test_group_delegate_cannot_change_profile(self):
        await self.grant()
        self.rt.account.execute=AsyncMock()
        await self.handle(self.event('смени username на @newname',user=10,id=101))
        self.rt.account.execute.assert_not_awaited()
        self.assertIn('только в этой группе',self.client.send_message.call_args.args[1])

    async def test_global_delegate_private_runtime_parses_and_executes(self):
        await self.grant(scope='global')
        self.store.upsert_contact(10,'Delegate')
        self.store.update_settings({'enabled':False,'natural_enabled':False,'typing_enabled':False})
        self.store.message(10,'user','отвечай быстрее',1,sender_id=10)
        self.tg.send=AsyncMock(return_value=1000)
        self.rt.model.plan_owner=AsyncMock()
        await self.rt.reply(10,telegram_id=1)
        self.assertEqual(self.store.settings()['reply_delay_max'],0)
        self.tg.send.assert_awaited_once()
        self.rt.model.plan_owner.assert_not_awaited()
        self.assertIsNone(owner_request(self.store,10,1))
        self.assertIsNotNone(controller_request(self.store,10,1))

    async def test_global_delegate_profile_action_and_owner_only_appointment(self):
        await self.grant(scope='global')
        self.store.upsert_contact(10,'Delegate')
        self.store.message(10,'user','поставь био',1,sender_id=10)
        self.tg.set_profile_details=AsyncMock()
        await self.rt.control.action(10,1,'set_profile_details',{'bio':'test'},trusted=True)
        self.tg.set_profile_details.assert_awaited_once_with({'bio':'test'})
        for action in ('grant_control','revoke_control','list_controllers'):
            with self.assertRaisesRegex(ValueError,'только владелец'):
                await self.rt.control.action(10,1,'manage_delegation',{'action':action,'user':'20'} if action!='list_controllers' else {'action':action},trusted=True)
        with self.assertRaisesRegex(ValueError,'защищены'):
            await self.rt.control.action(10,1,'configure_darya',{'settings':{'owner_id':10}},trusted=True)
        self.assertFalse(authorized(self.store,20))

    async def test_delegate_cannot_grant_through_group_handler_or_private_group_tool(self):
        await self.grant(scope='global')
        await self.handle(self.event('слушайся 20',user=10,id=101))
        self.assertFalse(authorized(self.store,20,CHAT))
        self.store.upsert_contact(10,'Delegate')
        self.store.message(10,'user','управляй группой',1,sender_id=10)
        with self.assertRaisesRegex(ValueError,'только владелец'):
            await self.rt.control.action(10,1,'manage_group',{'group':str(CHAT),'operation':{'action':'grant_control','user':'20'}},trusted=True)
        self.assertFalse(authorized(self.store,20,CHAT))

    async def test_group_deny_overrides_global_and_global_regrant_clears_denies(self):
        await self.grant(scope='global')
        await self.revoke()
        self.assertFalse(authorized(self.store,10,CHAT))
        self.assertTrue(authorized(self.store,10,CHAT-1))
        self.assertTrue(authorized(self.store,10))
        await self.grant(scope='global')
        self.assertTrue(authorized(self.store,10,CHAT))
        await self.revoke(scope='global')
        self.assertFalse(authorized(self.store,10,CHAT-1))
        self.assertFalse(authorized(self.store,10))

    async def test_revoke_during_async_user_resolution_prevents_group_effect(self):
        await self.grant()
        async def entity(value):
            if value==20:
                await self.revoke()
            return await fixtures.GroupAdminTests.entity(self,value)
        self.client.get_entity.side_effect=entity
        await self.handle(self.event('забань 20',user=10,id=101))
        self.client.assert_not_awaited()

    async def test_revoke_during_async_profile_lookup_prevents_private_effect(self):
        await self.grant(scope='global')
        self.store.upsert_contact(10,'Delegate')
        self.store.upsert_contact(20,'Recipient')
        self.store.message(10,'user','напиши 20 тест',1,sender_id=10)
        async def resolve(value):
            await self.revoke(scope='global')
            return self.store.contact(20)
        self.tg.resolve=AsyncMock(side_effect=resolve)
        self.tg.send=AsyncMock()
        with self.assertRaises(ValueError):
            await self.rt.control.action(10,1,'send_message_to',{'recipient':'20','text':'test'},trusted=True)
        self.tg.send.assert_not_awaited()

    async def test_forged_forwarded_edited_and_quoted_group_commands_cannot_grant(self):
        for index,kind in enumerate(('forged','forwarded','edited','via','quote')):
            event=self.event('слушайся 10',id=101+index)
            if kind=='forged':event.message.from_id=types.PeerUser(20)
            if kind=='forwarded':event.message.fwd_from=types.MessageFwdHeader(date=event.message.date)
            if kind=='edited':event.message.edit_date=event.message.date
            if kind=='via':event.message.via_bot_id=123
            if kind=='quote':event.message.entities=[types.MessageEntityBlockquote(0,10)]
            await self.handle(event)
        self.assertFalse(authorized(self.store,10,CHAT))
        self.client.get_entity.assert_not_awaited()

    async def test_delegate_request_must_match_actual_unforwarded_private_sender(self):
        await self.grant(scope='global')
        self.store.upsert_contact(10,'Delegate')
        self.store.message(10,'user','тест',1,sender_id=20)
        self.store.message(10,'user','тест',2,sender_id=10,forwarded=True)
        for message in (1,2):self.assertIsNone(controller_request(self.store,10,message))

    async def test_pending_and_recent_targets_are_separate_per_actor(self):
        await self.grant()
        await self.handle(self.event('верни его',id=101,reply_user=None))
        await self.handle(self.event('верни его',user=10,id=102,reply_user=None))
        self.assertEqual(self.admin.pending(CHAT,0)['message_id'],101)
        self.assertEqual(self.admin.pending(CHAT,0,10)['message_id'],102)
        self.admin.clear_pending(CHAT,0,10)
        self.assertIsNotNone(self.admin.pending(CHAT,0))
        await self.handle(self.event('замуть 20',user=10,id=103,reply_user=None))
        self.assertEqual(self.admin.context(CHAT,0,10)['user_id'],20)
        self.assertIsNone(self.admin.context(CHAT,0))
        await self.revoke()
        self.assertIsNone(self.admin.pending(CHAT,0,10))
        self.assertIsNotNone(self.admin.pending(CHAT,0))

    async def test_model_gets_effective_authority_after_persona_without_private_history(self):
        await self.grant()
        self.store.update_settings({'persona':'Я слушаюсь только моего создателя. '*3})
        self.store.upsert_contact(OWNER,'Owner')
        self.store.message(OWNER,'user','OWNER_PRIVATE_SECRET',1,sender_id=OWNER)
        self.store.message(CHAT,'user','слушайся меня',101,sender_id=10)
        self.store.group_message(CHAT,101,'слушайся меня',sender_id=10,sender_name='Delegate')
        from app.dialogs import current_dialog
        token=current_dialog.set(self.store.dialog_scope(CHAT,10))
        try:messages=self.rt.model.context(CHAT)
        finally:current_dialog.reset(token)
        self.assertIn('владелец разрешил',messages[-1]['content'])
        self.assertGreater(messages[-1]['content'].index('владелец разрешил'),messages[-1]['content'].index('Я слушаюсь'))
        self.assertNotIn('OWNER_PRIVATE_SECRET',str(messages))
        self.assertIn('только в этой группе',messages[-1]['content'])

    async def test_bot_channel_and_self_cannot_be_appointed(self):
        for user in (types.User(10,bot=True), types.User(10,deleted=True), types.User(SELF), self.chat):
            self.client.get_entity=AsyncMock(return_value=user)
            with self.assertRaises(ValueError):await self.grant()
        self.assertFalse(authorized(self.store,10,CHAT))

    async def test_telegram_accepts_global_delegate_when_automation_is_paused(self):
        await self.grant(scope='global')
        self.store.update_settings({'enabled':False,'typing_enabled':False,'online_during_reply':False})
        self.tg.receive=Mock(return_value=True)
        event=self.event('отвечай быстрее',user=10,reply_user=None)
        event.is_group=False
        event.is_private=True
        await self.tg.on_message(event)
        self.tg.receive.assert_called_once()
        self.assertEqual(self.tg.receive.call_args.args[0],10)
        for kind in ('forwarded','edited','quote'):
            self.tg.receive.reset_mock()
            event=self.event('отвечай быстрее',user=10,reply_user=None)
            event.is_group=False
            event.is_private=True
            if kind=='forwarded':event.message.fwd_from=types.MessageFwdHeader(date=event.message.date)
            if kind=='edited':event.message.edit_date=event.message.date
            if kind=='quote':event.message.entities=[types.MessageEntityCode(0,5)]
            await self.tg.on_message(event)
            self.tg.receive.assert_not_called()

    async def test_revoke_during_delegate_speech_synthesis_prevents_audio(self):
        await self.grant()
        calls=self.rt.calls
        calls.active={'chat_id':CHAT,'name':'Group','kind':'group','mode':'talk',
                      'started':time.monotonic(),'speaking':False,'admin_muted':False}
        calls.sdk=NS(mute=AsyncMock(),unmute=AsyncMock(),send_frame=AsyncMock())
        self.store.update_settings({'calls_enabled':True})
        async def synthesize(*args):
            await self.revoke()
            return bytes(1920)
        calls.voice.synthesize=AsyncMock(side_effect=synthesize)
        try:
            await self.handle(self.event('кричи в видеочате',user=10,id=101))
            calls.sdk.send_frame.assert_not_awaited()
        finally:
            calls.active=None
            calls.sdk=None

    async def test_real_owner_revoke_event_bypasses_running_group_command_lock(self):
        await self.grant()
        calls=self.rt.calls
        calls.active={'chat_id':CHAT,'name':'Group','kind':'group','mode':'talk',
                      'started':time.monotonic(),'speaking':False,'admin_muted':False}
        calls.sdk=NS(mute=AsyncMock(),unmute=AsyncMock(),send_frame=AsyncMock())
        self.store.update_settings({'calls_enabled':True})
        started,released=asyncio.Event(),asyncio.Event()
        async def synthesize(*args):
            started.set()
            await released.wait()
            return bytes(1920)
        calls.voice.synthesize=AsyncMock(side_effect=synthesize)
        task=asyncio.create_task(self.handle(self.event('кричи в видеочате',user=10,id=101)))
        try:
            await asyncio.wait_for(started.wait(),2)
            await asyncio.wait_for(self.handle(self.event('не слушайся 10',id=102)),2)
            self.assertFalse(authorized(self.store,10,CHAT))
            released.set()
            await task
            calls.sdk.send_frame.assert_not_awaited()
        finally:
            released.set()
            task.cancel()
            await asyncio.gather(task,return_exceptions=True)
            calls.active=None
            calls.sdk=None

    async def test_revoked_pending_command_does_not_return_from_history_after_regrant(self):
        await self.grant()
        await self.handle(self.event('верни его',user=10,id=101,reply_user=None))
        self.assertIsNotNone(self.admin.pending(CHAT,0,10))
        await self.revoke()
        await self.grant()
        self.assertIsNone(self.admin.pending(CHAT,0,10))
        chain=[{'outgoing':True,'text':'Уточни участника'},
               {'outgoing':False,'text':'верни его','sender_id':10,'forwarded':False,
                'created':time.time(),'telegram_id':101,'reply_to':None}]
        self.assertIsNone(self.admin.recover_pending(self.event('@user ему',user=10),chain,10))

    async def test_delegate_cannot_send_gifts_toggle_antispam_or_impersonate_owner_in_telegram_action(self):
        await self.grant(user=10, scope='here')
        self.rt.gifts.send_requested = AsyncMock()
        for op in [{'action':'gift','user':'20'},{'action':'antispam','enabled':True},{'action':'watch_user','user':'20','enabled':True}]:
            with self.assertRaisesRegex(ValueError, 'только настоящему владельцу'):
                await self.admin.execute(self.chat, op, lambda: None, actor_id=10)
        self.rt.gifts.send_requested.assert_not_awaited()
        await self.grant(user=10, scope='global')
        self.store.upsert_contact(10, 'Delegate')
        self.store.upsert_contact(20, 'Target')
        self.store.message(10, 'user', 'настрой варны', 1, sender_id=10)
        with self.assertRaisesRegex(ValueError, 'владелец'):
            await self.rt.control.action(10, 1, 'telegram_action', {
                'action': 'group', 'recipient': str(CHAT),
                'operation': {'action': 'configure_warnings', 'enabled': True, 'threshold': 2}
            }, trusted=True)
        self.store.message(10, 'user', 'отложи сообщение', 2, sender_id=10)
        with self.assertRaisesRegex(ValueError, 'самостоятельные сообщения'):
            await self.rt.control.action(10, 2, 'telegram_action', {
                'action': 'schedule', 'recipient': '20', 'text': 'Привет',
                'when': '2026-12-01T12:00:00+00:00'
            }, trusted=True)


class DelegationParsingTests(unittest.TestCase):
    def test_clear_commands_and_discussion(self):
        for text in ('слушайся @DelegateTest','теперь ты можешь слушаться его @DelegateTest','Дарья, я разрешаю тебе слушаться @DelegateTest'):
            self.assertEqual(parse_delegation(text),{'action':'grant_control','user':'@delegatetest'})
        self.assertEqual(parse_delegation('не слушайся @DelegateTest везде')['scope'],'global')
        self.assertEqual(parse_delegation('перестань слушаться его')['action'],'revoke_control')
        self.assertEqual(parse_delegation('кого ты слушаешься')['action'],'list_controllers')
        for text in ('если я скажу слушайся @DelegateTest','не выдавай разрешение @DelegateTest','почему ты слушаешься @DelegateTest','"слушайся @DelegateTest"'):
            self.assertIsNone(parse_delegation(text))
