import json
import time
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

from telethon import errors, functions, types

from app.ai import ModelUnavailable
from app.group_admin import AdminOperation, parse_command
from app.groups import parse_navigation_command
from tests import test_group_admin as fixtures

OWNER, SELF, CHAT = fixtures.OWNER, fixtures.SELF, fixtures.CHAT
LINK='https://t.me/+FixtureInvite1234'


class NavigationParsingTests(unittest.TestCase):
    def test_screenshot_and_common_membership_commands_preserve_invite_case(self):
        for text in (f'дашка зайди туда {LINK}', f'Даша, зайди по этой ссылке {LINK}',
                     f'вступи в группу {LINK}', f'присоединись к группе {LINK}',
                     f'заходи сюда {LINK}', f'дашуль зайди туда {LINK}'):
            with self.subTest(text=text):
                self.assertEqual(parse_navigation_command(text),{'action':'join_group','target':LINK})
                self.assertEqual(parse_command(text)[0].target,LINK)
        self.assertEqual(parse_navigation_command('зайди в чат @example')['action'],'join_group')
        self.assertEqual(parse_navigation_command('выйди из группы @example')['action'],'leave_group')

    def test_group_links_calls_and_local_voicechat_have_distinct_actions(self):
        self.assertEqual(parse_command('подключись к голосовому чату https://t.me/example и говори')[0].action,'join_call')
        parsed=parse_navigation_command('дашка зайди туда https://t.me/call/AbC_def')
        self.assertEqual(parsed,{'action':'join_call','target':'https://t.me/call/AbC_def'})
        self.assertEqual(parse_navigation_command('зайди https://t.me/example?voicechat=AbC')['action'],'join_call')
        self.assertEqual(parse_command('запусти голосовой чат')[0].action,'start_call')

    def test_negations_discussions_quotes_and_unrelated_links_are_not_join_requests(self):
        for text in (f'не заходи {LINK}', f'если я скажу зайди туда {LINK}',
                     f'она написала зайди туда {LINK}', f'"зайди туда {LINK}"',
                     f'почему не зашла {LINK}', f'посмотри ссылку {LINK}',
                     'зайди на сайт https://example.com', 'привет @example', LINK):
            with self.subTest(text=text):self.assertIsNone(parse_navigation_command(text))

    def test_navigation_schema_requires_explicit_valid_target(self):
        for data in ({'action':'join_group'},{'action':'join_group','target':'file:///tmp/private'},
                     {'action':'join_group','target':LINK,'user':'20'},
                     {'action':'join_call','target':'https://t.me/+abcdef?payment=true'}):
            with self.subTest(data=data),self.assertRaises(ValueError):AdminOperation(**data).checked()


class GroupNavigationTests(unittest.IsolatedAsyncioTestCase):
    async def test_public_join_accepts_acknowledgment_without_chats_vector(self):
        self.client.get_entity=AsyncMock(return_value=self.target_chat)
        self.client.side_effect=lambda request:types.UpdatesTooLong()
        result=await self.rt.calls.groups.join('@example',lambda:None)
        self.assertEqual(result['status'],'joined')
        self.assertEqual(result['group_id'],-1000000000777)
        self.assertEqual(self.client.await_count,1)

    async def test_invite_join_reads_membership_when_acknowledgment_omits_chats(self):
        checks=0
        async def rpc(request):
            nonlocal checks
            if isinstance(request,functions.messages.CheckChatInviteRequest):
                checks+=1
                return self.invite if checks==1 else types.ChatInviteAlready(self.target_chat)
            if isinstance(request,functions.messages.ImportChatInviteRequest):return types.UpdatesTooLong()
            return await self.navigation_rpc(request)
        self.client.side_effect=rpc
        result=await self.rt.calls.groups.join(LINK,lambda:None)
        self.assertEqual(result['status'],'joined')
        self.assertEqual(checks,2)
        imports=[call for call in self.client.await_args_list if isinstance(call.args[0],functions.messages.ImportChatInviteRequest)]
        self.assertEqual(len(imports),1)

    async def test_missing_invite_membership_is_not_retried_or_claimed_as_joined(self):
        async def rpc(request):
            if isinstance(request,functions.messages.CheckChatInviteRequest):return self.invite
            if isinstance(request,functions.messages.ImportChatInviteRequest):return types.UpdatesTooLong()
            return await self.navigation_rpc(request)
        self.client.side_effect=rpc
        with self.assertRaisesRegex(ValueError,'не подтвердил вступление'):
            await self.rt.calls.groups.join(LINK,lambda:None)
        imports=[call for call in self.client.await_args_list if isinstance(call.args[0],functions.messages.ImportChatInviteRequest)]
        self.assertEqual(len(imports),1)

    asyncSetUp=fixtures.GroupAdminTests.asyncSetUp
    asyncTearDown=fixtures.GroupAdminTests.asyncTearDown
    entity=fixtures.GroupAdminTests.entity
    rpc=fixtures.GroupAdminTests.rpc
    message=fixtures.GroupAdminTests.message
    event=fixtures.GroupAdminTests.event
    handle=fixtures.GroupAdminTests.handle

    async def asyncSetUp(self):
        await fixtures.GroupAdminTests.asyncSetUp(self)
        self.tg.rpc=lambda operation:operation()
        self.target_chat=types.Channel(777,'Target',types.ChatPhotoEmpty(),None,megagroup=True,left=True)
        self.invite=NS(title='Target',broadcast=False,subscription_pricing=None,subscription_form_id=None)
        self.client.side_effect=self.navigation_rpc
        self.rt.model.completion=AsyncMock(side_effect=ModelUnavailable('API модели вернул код 502'))

    async def navigation_rpc(self,request):
        if isinstance(request,functions.messages.CheckChatInviteRequest):return self.invite
        if isinstance(request,functions.messages.ImportChatInviteRequest):return NS(chats=[self.target_chat])
        return await fixtures.GroupAdminTests.rpc(self,request)

    async def grant(self,user=10,scope='global'):
        await self.rt.authority.execute(OWNER,{'action':'grant_control','user':str(user),'scope':scope},lambda:None,chat_id=CHAT)

    async def test_owner_screenshot_command_joins_without_model_or_origin_admin_rights(self):
        event=self.event(f'дашка зайди туда {LINK}',reply_user=None)
        self.assertTrue(await self.handle(event))
        requests=[call.args[0] for call in self.client.await_args_list]
        self.assertEqual([type(r) for r in requests],[functions.messages.CheckChatInviteRequest,functions.messages.ImportChatInviteRequest])
        self.assertEqual(requests[1].hash,'FixtureInvite1234')
        self.client.get_permissions.assert_not_awaited()
        self.rt.model.completion.assert_not_awaited()
        self.assertIn('Вступила в группу',self.client.send_message.call_args.args[1])
        self.assertTrue(self.store.contact(-1000000000777)['enabled'])
        await self.handle(event)
        self.assertEqual(self.client.await_count,2)
        self.client.send_message.assert_awaited_once()

    async def test_private_owner_parser_handles_same_screenshot_phrase(self):
        self.store.upsert_contact(OWNER,'Owner')
        self.store.message(OWNER,'user',f'дашка зайди туда {LINK}',1,sender_id=OWNER)
        plan=self.rt.control.parse([f'дашка зайди туда {LINK}'])
        answer=await self.rt.control.execute_plan(OWNER,1,plan)
        self.assertIn('Вступила в группу',answer)
        self.rt.model.completion.assert_not_awaited()

    async def test_join_request_reports_pending_and_never_claims_membership(self):
        async def rpc(request):
            if isinstance(request,functions.messages.ImportChatInviteRequest):raise errors.InviteRequestSentError(request)
            return await self.navigation_rpc(request)
        self.client.side_effect=rpc
        event=self.event(f'дашка зайди туда {LINK}',reply_user=None)
        await self.handle(event)
        answer=self.client.send_message.call_args.args[1]
        self.assertIn('Отправила заявку',answer)
        self.assertIn('Жду одобрения',answer)
        self.assertNotIn('Вступила',answer)
        self.assertEqual(self.store.rows('SELECT id FROM contacts WHERE name=?',('Target',)),[])
        await self.handle(event)
        self.assertEqual(self.client.await_count,2)

    async def test_already_joined_invite_is_reported_without_import(self):
        self.client.side_effect=None
        self.client.return_value=types.ChatInviteAlready(self.target_chat)
        await self.handle(self.event(f'зайди туда {LINK}',reply_user=None))
        self.assertIn('Уже состою',self.client.send_message.call_args.args[1])
        self.assertEqual(self.client.await_count,1)

    async def test_unapproved_user_and_group_admin_cannot_move_account(self):
        self.client.get_permissions.side_effect=lambda *args:self.me
        await self.handle(self.event(f'зайди туда {LINK}',user=10,reply_user=None))
        self.client.assert_not_awaited()
        self.client.send_message.assert_not_awaited()

    async def test_group_only_delegate_cannot_move_account_but_global_delegate_can(self):
        await self.grant(scope='here')
        await self.handle(self.event(f'зайди туда {LINK}',user=10,id=101,reply_user=None))
        self.client.assert_not_awaited()
        self.assertIn('разрешением владельца везде',self.client.send_message.call_args.args[1])
        await self.grant()
        self.client.reset_mock()
        await self.handle(self.event(f'зайди туда {LINK}',user=10,id=102,reply_user=None))
        self.assertIsInstance(self.client.call_args.args[0],functions.messages.ImportChatInviteRequest)

    async def test_revocation_during_invite_lookup_stops_import(self):
        await self.grant()
        async def rpc(request):
            if isinstance(request,functions.messages.CheckChatInviteRequest):
                await self.rt.authority.execute(OWNER,{'action':'revoke_control','user':'10','scope':'global'},lambda:None)
            return await self.navigation_rpc(request)
        self.client.side_effect=rpc
        await self.handle(self.event(f'зайди туда {LINK}',user=10,reply_user=None))
        self.assertEqual(self.client.await_count,1)
        self.assertIsInstance(self.client.call_args.args[0],functions.messages.CheckChatInviteRequest)
        self.assertEqual(self.store.rows('SELECT id FROM contacts WHERE name=?',('Target',)),[])

    async def test_forwarded_edited_quoted_and_stale_owner_requests_do_not_join(self):
        for index,kind in enumerate(('forwarded','edited','quoted','stale')):
            event=self.event(f'зайди туда {LINK}',id=101+index,reply_user=None)
            if kind=='forwarded':event.message.fwd_from=types.MessageFwdHeader(date=event.message.date)
            if kind=='edited':event.message.edit_date=event.message.date
            if kind=='quoted':event.message.entities=[types.MessageEntityBlockquote(0,10)]
            if kind=='stale':
                from datetime import datetime,timezone
                event.message.date=datetime.fromtimestamp(time.time()-400,timezone.utc)
            await self.handle(event)
        self.client.assert_not_awaited()

    async def test_public_group_and_conference_commands_use_exact_target(self):
        self.admin.groups.resolve=AsyncMock(return_value=self.target_chat)
        self.client.side_effect=None
        self.client.return_value=NS(chats=[self.target_chat])
        await self.handle(self.event('Даша зайди в группу @example',id=101,reply_user=None))
        self.assertIsInstance(self.client.call_args.args[0],functions.channels.JoinChannelRequest)
        self.rt.calls.join=AsyncMock(return_value={'connected':True,'name':'Conference','mode':'talk','admin_muted':False})
        await self.handle(self.event('дашка зайди туда https://t.me/call/AbC_def',id=102,reply_user=None))
        self.assertEqual(self.rt.calls.join.call_args.args[:2],('https://t.me/call/AbC_def',self.store.settings()['call_mode']))
        self.rt.model.completion.assert_not_awaited()

    async def test_model_cannot_invent_destination_or_authorize_a_transition(self):
        self.rt.model.completion=AsyncMock(return_value={'content':json.dumps({'intent':'command','actions':[{'action':'join_group','target':LINK}]})})
        await self.handle(self.event('зайди в ту группу',reply_user=None))
        self.client.assert_not_awaited()
        self.assertIn('Укажи точную ссылку',self.client.send_message.call_args.args[1])

    async def test_nonstandard_wording_uses_navigation_schema_in_classifier(self):
        self.rt.model.completion=AsyncMock(return_value={'content':json.dumps({'intent':'command','actions':[{'action':'join_group','target':LINK}]})})
        await self.handle(self.event(f'Дашуль, давай переберёмся вот сюда {LINK}',reply_user=None))
        self.assertIsInstance(self.client.call_args.args[0],functions.messages.ImportChatInviteRequest)
        payload=json.loads(self.rt.model.completion.call_args.args[0][1]['content'])
        entries={entry['name']:entry for entry in payload['catalog']}
        self.assertIn('join_group',entries)
        self.assertEqual(entries['join_group']['required'],['target'])
        self.assertIn('пригласительной',entries['join_group']['description'])

    async def test_paid_expired_or_channel_invite_reports_real_failure(self):
        for index,kind in enumerate(('paid','expired','channel')):
            if kind=='paid':self.invite.subscription_pricing=NS(amount=10)
            else:self.invite.subscription_pricing=None
            self.invite.broadcast=kind=='channel'
            async def rpc(request,kind=kind):
                if kind=='expired':raise errors.InviteHashExpiredError(request)
                return await self.navigation_rpc(request)
            self.client.side_effect=rpc
            self.client.reset_mock()
            await self.handle(self.event(f'зайди туда {LINK}',id=101+index,reply_user=None))
            self.assertEqual(self.client.await_count,1)
            self.assertNotIn('Вступила',self.client.send_message.call_args.args[1])

    async def test_model_context_overrides_old_false_membership_denial_after_persona(self):
        self.store.update_settings({'persona':'В приватные ссылки я не захожу и не умею вступать. '*3})
        self.store.upsert_contact(OWNER,'Owner')
        self.store.message(CHAT,'user',f'зайди туда {LINK}',101,sender_id=OWNER)
        from app.dialogs import current_dialog
        token=current_dialog.set(self.store.dialog_scope(CHAT,OWNER))
        try:messages=self.rt.model.context(CHAT)
        finally:current_dialog.reset(token)
        self.assertIn('включая приватные',messages[-1]['content'])
        self.assertGreater(messages[-1]['content'].index('Подтверждённые функции сервера'),messages[-1]['content'].index('не умею вступать'))
