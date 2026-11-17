import asyncio
import json
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

from telethon import errors, functions, types

from app.group_admin import AdminOperation, parse_command
from app.runtime import Runtime
from app.store import Store
from app.telegram_actions import TelegramAction
from app.telegram_autonomy import AutonomyStopped


OWNER, SELF, CHAT = 1000000001, 1000000002, -1000000000123


class JoinRequestsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name))
        self.store.update_settings({'owner_id': OWNER, 'enabled': True, 'proactive_enabled': False})
        self.store.upsert_contact(CHAT,'Test',kind='group')
        self.rt = Runtime(self.store)
        self.admin, self.tg = self.rt.group_admin, self.rt.telegram
        self.joins = self.admin.join_requests
        self.tg.ready = Mock(return_value=True)
        self.tg.account_id = SELF
        self.chat = types.Channel(123,'Test',types.ChatPhotoEmpty(),datetime.now(timezone.utc),megagroup=True)
        self.client = AsyncMock()
        self.tg.client = self.client
        self.client.get_entity.return_value = self.chat
        self.client.get_permissions.return_value = NS(is_creator=False,is_admin=True,is_chat=False,
            participant=NS(admin_rights=types.ChatAdminRights(invite_users=True)))
        self.client.side_effect = self.rpc
        self.client.send_message.side_effect = self.sent
        self.rt.model.completion = AsyncMock(side_effect=ValueError('API модели вернул код 502'))
        self.people = [types.User(10,access_hash=100,first_name='One'),types.User(11,access_hash=110,first_name='Two')]
        self.date = datetime.now(timezone.utc)
        self.pending = {10:self.date,11:self.date}
        self.last_sent = None

    async def asyncTearDown(self):
        self.tg.client = None
        await self.rt.stop()
        self.store.close()
        self.tmp.cleanup()

    def message(self,mid,user,text,topic=0):
        return types.Message(mid,types.PeerChannel(123),datetime.now(timezone.utc),text,from_id=types.PeerUser(user),
            reply_to=types.MessageReplyHeader(reply_to_msg_id=topic,forum_topic=True) if topic else None)

    def event(self,text,mid=100,actor=OWNER,topic=0,reply=None):
        msg = self.message(mid,actor,text,topic)
        if reply is not None:
            msg.reply_to = types.MessageReplyHeader(reply_to_msg_id=reply.id,reply_to_top_id=topic or None,forum_topic=bool(topic))
        self.store.group_message(CHAT,mid,text,sender_id=actor,topic_id=topic,reply_to=getattr(reply,'id',None))
        return NS(id=mid,chat_id=CHAT,raw_text=text,message=msg,is_group=True,is_private=False,is_reply=bool(reply),
            get_chat=AsyncMock(return_value=self.chat),get_reply_message=AsyncMock(return_value=reply))

    async def handle(self,event):
        return await self.admin.handle(event,types.User(event.message.sender_id,first_name='Actor'))

    async def sent(self,chat,text,**kwargs):
        self.last_sent = self.message(900+self.client.send_message.await_count,SELF,text,kwargs.get('reply_to',0))
        return self.last_sent

    async def rpc(self,request):
        if isinstance(request,functions.messages.GetChatInviteImportersRequest):
            return NS(importers=[NS(user_id=uid,date=date) for uid,date in self.pending.items()],
                users=[u for u in self.people if u.id in self.pending])
        if isinstance(request,functions.messages.HideAllChatJoinRequestsRequest):
            self.pending.clear()
        if isinstance(request,functions.messages.HideChatJoinRequestRequest):
            self.pending.pop(request.user_id.user_id,None)
        return True

    def effects(self,kind=functions.messages.HideAllChatJoinRequestsRequest):
        return [call.args[0] for call in self.client.await_args_list if isinstance(call.args[0],kind)]

    async def enable(self,actor=OWNER):
        await self.handle(self.event('принимай заявки автоматически',actor=actor))

    def mode(self):
        return self.store.rows('SELECT * FROM group_join_automation WHERE chat_id=?',(CHAT,))[0]

    async def test_screenshot_bulk_phrases_ignore_stale_pending_and_model_failure(self):
        for n,text in enumerate(['прими все заявки тут','прими все заявки','даша прими все заявки тут','Даша, одобри все заявки здесь']):
            self.store.execute('INSERT OR REPLACE INTO group_admin_pending VALUES (?,?,?,?,?,?,?,?)',
                (CHAT,0,OWNER,0,99,None,'прими их',time.time()))
            event = self.event(text,100+n)
            await self.handle(event)
            await self.handle(event)
        self.assertEqual(len(self.effects()),4)
        self.assertTrue(all(r.approved for r in self.effects()))
        self.rt.model.completion.assert_not_awaited()
        self.assertEqual(self.store.rows('SELECT * FROM group_admin_pending'),[])

    async def test_bulk_is_native_single_request_for_more_than_fifty(self):
        self.pending = {uid:self.date for uid in range(1000,1101)}
        await self.handle(self.event('прими все заявки'))
        self.assertEqual(len(self.effects()),1)
        self.assertEqual(self.pending,{})

    async def test_list_followup_excludes_later_applicant_and_checks_input_users(self):
        await self.handle(self.event('покажи заявки',topic=390))
        reply = self.last_sent
        self.people.append(types.User(12,access_hash=120,first_name='New'))
        self.pending[12] = self.date
        event = self.event('прими их',101,topic=390,reply=reply)
        await self.handle(event)
        await self.handle(event)
        requests = self.effects(functions.messages.HideChatJoinRequestRequest)
        self.assertEqual([r.user_id.user_id for r in requests],[10,11])
        self.assertEqual([r.user_id.access_hash for r in requests],[100,110])
        self.assertEqual(set(self.pending),{12})
        self.assertEqual(self.effects(),[])
        self.rt.model.completion.assert_not_awaited()

    async def test_list_followup_without_reply_uses_last_same_author_list(self):
        await self.handle(self.event('покажи заявки'))
        await self.handle(self.event('одобри эти заявки',101))
        self.assertEqual(len(self.effects(functions.messages.HideChatJoinRequestRequest)),2)

    async def test_list_is_not_available_before_successful_publication(self):
        self.client.send_message.side_effect = RuntimeError('delivery failed')
        await self.handle(self.event('покажи заявки'))
        await self.handle(self.event('прими их',101))
        self.assertEqual(self.effects(functions.messages.HideChatJoinRequestRequest),[])

    async def test_list_cannot_be_borrowed_by_another_author_or_topic(self):
        await self.handle(self.event('покажи заявки',topic=390))
        reply = self.last_sent
        self.store.execute('INSERT INTO controller_permissions VALUES (?,?,?,?,?)',(OWNER,20,CHAT,1,time.time()))
        await self.handle(self.event('прими их',101,actor=20,topic=390,reply=reply))
        await self.handle(self.event('прими их',102,topic=391,reply=reply))
        self.assertEqual(self.effects(functions.messages.HideChatJoinRequestRequest),[])

    async def test_untrusted_list_reply_cannot_supply_ids(self):
        fake = self.message(70,20,'Заявки (до 50):\nOne · 10')
        await self.handle(self.event('прими их',reply=fake))
        self.assertEqual(self.effects(functions.messages.HideChatJoinRequestRequest),[])

    async def test_expired_or_archived_list_is_not_used(self):
        await self.handle(self.event('покажи заявки'))
        self.store.execute('UPDATE group_join_lists SET created=?',(time.time()-1801,))
        await self.handle(self.event('прими их',101))
        self.assertEqual(self.effects(functions.messages.HideChatJoinRequestRequest),[])
        self.store.execute('UPDATE group_join_lists SET created=?,generation=999',(time.time(),))
        await self.handle(self.event('прими их',102))
        self.assertEqual(self.effects(functions.messages.HideChatJoinRequestRequest),[])

    async def test_listed_request_can_be_found_beyond_first_page(self):
        await self.handle(self.event('покажи заявки'))
        unrelated = [types.User(uid,access_hash=uid,first_name='New') for uid in range(100,150)]
        base = self.rpc
        async def paginated(request):
            if isinstance(request,functions.messages.GetChatInviteImportersRequest) and request.offset_date is None:
                return NS(importers=[NS(user_id=u.id,date=self.date) for u in unrelated],users=unrelated)
            return await base(request)
        self.client.side_effect = paginated
        await self.handle(self.event('прими их',101))
        self.assertEqual([r.user_id.user_id for r in self.effects(functions.messages.HideChatJoinRequestRequest)],[10,11])

    async def test_legacy_screenshot_list_is_verified_against_server_result(self):
        original = self.event('даша прими все заявки тут',3047,topic=390)
        text = 'Заявки (до 50):\nOne · 10\nTwo · 11'
        reply = self.message(3048,SELF,text,390)
        self.store.group_message(CHAT,3048,text,sender_id=SELF,reply_to=original.id,topic_id=390,outgoing=True)
        self.store.execute('INSERT INTO group_admin_actions VALUES (?,?,?,?,?,?)',
            (CHAT,original.id,'0','sent',json.dumps({'text':text}),time.time()))
        await self.handle(self.event('прими их',3049,topic=390,reply=reply))
        self.assertEqual(len(self.effects(functions.messages.HideChatJoinRequestRequest)),2)

    async def test_different_request_from_same_user_is_not_original_list(self):
        await self.handle(self.event('покажи заявки'))
        self.pending[10] = datetime.fromtimestamp(self.date.timestamp()+1,timezone.utc)
        await self.handle(self.event('прими их',101))
        self.assertEqual([r.user_id.user_id for r in self.effects(functions.messages.HideChatJoinRequestRequest)],[11])

    async def test_approval_checks_permission_between_each_user(self):
        await self.handle(self.event('покажи заявки'))
        base = self.rpc
        async def revoke(request):
            result = await base(request)
            if isinstance(request,functions.messages.HideChatJoinRequestRequest):
                self.store.update_settings({'owner_id':99})
            return result
        self.client.side_effect = revoke
        await self.handle(self.event('прими их',101))
        self.assertEqual(len(self.effects(functions.messages.HideChatJoinRequestRequest)),1)

    async def test_auto_default_off_and_only_enabled_group_is_polled(self):
        await self.joins.poll()
        self.client.assert_not_awaited()
        await self.enable()
        self.rt.model.completion.assert_not_awaited()
        self.assertEqual(self.mode()['enabled'],1)
        await self.joins.poll()
        self.assertEqual(len(self.effects()),1)
        self.client.get_entity.assert_awaited_once_with(CHAT)
        self.assertEqual(self.mode()['enabled'],1)
        self.assertFalse(self.store.contact(CHAT)['proactive'])

    async def test_auto_new_requests_on_later_poll(self):
        await self.enable()
        await self.joins.poll()
        self.pending[10] = self.date
        self.store.execute('UPDATE group_join_automation SET next_check=0')
        await self.joins.poll()
        self.assertEqual(len(self.effects()),2)

    async def test_auto_disable_persists_and_no_admin_right_needed_to_stop(self):
        await self.enable()
        self.client.get_permissions.side_effect = errors.ChatAdminRequiredError(None)
        await self.handle(self.event('отключи автоприем заявок',101))
        await self.joins.poll()
        self.assertEqual(self.mode()['enabled'],0)
        self.assertEqual(self.effects(),[])

    async def test_auto_permission_revoked_mid_read_stops_before_dispatch(self):
        self.store.execute('INSERT INTO controller_permissions VALUES (?,?,?,?,?)',(OWNER,20,CHAT,1,time.time()))
        await self.enable(actor=20)
        base = self.rpc
        async def revoke(request):
            result = await base(request)
            if isinstance(request,functions.messages.GetChatInviteImportersRequest):
                self.store.execute('UPDATE controller_permissions SET enabled=0')
            return result
        self.client.side_effect = revoke
        await self.joins.poll()
        self.assertEqual(self.effects(),[])
        self.assertEqual(self.mode()['enabled'],0)

    async def test_regrant_does_not_restore_old_automation(self):
        self.store.execute('INSERT INTO controller_permissions VALUES (?,?,?,?,?)',(OWNER,20,CHAT,1,1))
        await self.enable(actor=20)
        self.store.execute('UPDATE controller_permissions SET updated=2')
        await self.joins.poll()
        self.assertEqual(self.effects(),[])
        self.assertEqual(self.mode()['enabled'],0)

    async def test_auto_lost_acknowledgement_disables_instead_of_repeating(self):
        await self.enable()
        base = self.rpc
        async def lose_ack(request):
            result = await base(request)
            if isinstance(request,functions.messages.HideAllChatJoinRequestsRequest):
                raise ConnectionError('ack lost')
            return result
        self.client.side_effect = lose_ack
        await self.joins.poll()
        self.pending[10] = self.date
        self.store.execute('UPDATE group_join_automation SET next_check=0')
        await self.joins.poll()
        self.assertEqual(len(self.effects()),1)
        self.assertEqual(self.mode()['enabled'],0)
        self.assertEqual(self.mode()['status'],'uncertain')

    async def test_auto_read_failure_is_deferred_without_dispatch(self):
        await self.enable()
        self.client.side_effect = ConnectionError('read failed')
        await self.joins.poll()
        self.assertEqual(self.mode()['enabled'],1)
        self.assertGreater(self.mode()['next_check'],time.time())
        self.assertEqual(self.effects(),[])

    async def test_auto_entity_cache_error_does_not_disable_and_recovers(self):
        await self.enable()
        self.client.get_entity.side_effect=ValueError('Input entity is not cached')
        await self.joins.poll()
        self.assertEqual(self.mode()['enabled'],1)
        self.assertEqual(self.effects(),[])
        self.client.get_entity.side_effect=None
        self.store.execute('UPDATE group_join_automation SET next_check=0')
        await self.joins.poll()
        self.assertEqual(len(self.effects()),1)
        self.assertEqual(self.mode()['status'],'idle')

    async def test_auto_disconnect_after_read_resumes_after_reconnect(self):
        await self.enable()
        base=self.rpc
        async def disconnect(request):
            result=await base(request)
            if isinstance(request,functions.messages.GetChatInviteImportersRequest):
                self.tg.ready.return_value=False
            return result
        self.client.side_effect=disconnect
        await self.joins.poll()
        self.assertEqual(self.mode()['enabled'],1)
        self.assertEqual(self.effects(),[])
        self.tg.ready.return_value=True
        self.client.side_effect=base
        self.store.execute('UPDATE group_join_automation SET next_check=0')
        await self.joins.poll()
        self.assertEqual(len(self.effects()),1)

    async def test_auto_approval_flood_wait_keeps_mode_and_waits_before_retry(self):
        await self.enable()
        base=self.rpc
        async def limited(request):
            if isinstance(request,functions.messages.HideAllChatJoinRequestsRequest):
                raise errors.FloodWaitError(request,capture=180)
            return await base(request)
        self.client.side_effect=limited
        started=time.time()
        await self.joins.poll()
        self.assertEqual(self.mode()['enabled'],1)
        self.assertEqual(self.mode()['status'],'failed')
        self.assertGreaterEqual(self.mode()['next_check'],started+180)
        self.assertEqual(len(self.effects()),1)
        await self.joins.poll()
        self.assertEqual(len(self.effects()),1)
        self.client.side_effect=base
        self.admin.cooldown_until=0
        self.store.execute('UPDATE group_join_automation SET next_check=0')
        await self.joins.poll()
        self.assertEqual(len(self.effects()),2)
        self.assertEqual(self.pending,{})
        self.assertEqual(self.mode()['enabled'],1)

    async def test_auto_missing_invite_permission_still_disables(self):
        await self.enable()
        self.client.get_permissions.return_value.participant.admin_rights.invite_users=False
        await self.joins.poll()
        self.assertEqual(self.mode()['enabled'],0)
        self.assertEqual(self.effects(),[])

    async def test_restart_preserves_enabled_mode_and_stops_inflight_attempt(self):
        await self.enable()
        self.store.close()
        self.store = Store(Path(self.tmp.name))
        self.assertEqual(self.mode()['enabled'],1)
        self.store.execute("UPDATE group_join_automation SET status='sending'")
        self.store.close()
        self.store = Store(Path(self.tmp.name))
        self.assertEqual(self.mode()['enabled'],0)
        self.assertEqual(self.mode()['status'],'uncertain')

    async def test_runtime_tick_polls_without_model_key_or_proactive(self):
        await self.enable()
        self.rt.model.key = Mock(return_value='')
        await self.rt.tick()
        await self.joins.task
        self.assertEqual(len(self.effects()),1)

    async def test_noncontroller_or_expired_command_cannot_enable(self):
        await self.handle(self.event('принимай заявки автоматически',actor=20))
        event = self.event('принимай заявки автоматически',101)
        event.message.date = datetime.fromtimestamp(time.time()-400,timezone.utc)
        await self.handle(event)
        self.assertEqual(self.store.rows('SELECT * FROM group_join_automation'),[])

    async def test_model_initiative_cannot_enable_or_approve_requests(self):
        self.rt.telegram_autonomy.allowed = Mock(return_value=True)
        self.store.execute('UPDATE contacts SET proactive=1 WHERE id=?',(CHAT,))
        for action in ['auto_approve_joins','approve_all_joins','approve_listed_joins']:
            op = TelegramAction(action='group',recipient=str(CHAT),operation={'action':action,**({'enabled':True} if action=='auto_approve_joins' else {})})
            with self.assertRaisesRegex(AutonomyStopped,'команде владельца'):
                self.rt.telegram_autonomy.guard(op,CHAT,self.store.dialog_scope(CHAT,0),self.store.settings()['telegram_autonomy_revision'],'autonomous')

    async def test_missing_telegram_right_blocks_bulk(self):
        self.client.get_permissions.return_value.participant.admin_rights.invite_users = False
        await self.handle(self.event('прими все заявки'))
        self.assertEqual(self.effects(),[])
        self.assertIn('прав',self.last_sent.message)


class JoinParserTests(unittest.TestCase):
    def test_explicit_enable_disable_and_telegram_requirement_are_distinct(self):
        for text in ['принимай заявки автоматически','автоматом принимай все заявки тут','включи автоприём заявок','/autoapprove on']:
            self.assertEqual(parse_command(text)[0].model_dump(exclude_none=True),{'action':'auto_approve_joins','enabled':True})
        for text in ['не принимай заявки автоматически','перестань принимать заявки','отключи автоприём заявок','/autoapprove off']:
            self.assertEqual(parse_command(text)[0].model_dump(exclude_none=True),{'action':'auto_approve_joins','enabled':False})
        for text in ['не прими все заявки','она сказала прими все заявки','как принимать заявки?','"прими все заявки"']:
            self.assertIsNone(parse_command(text))
        with self.assertRaises(ValueError):
            AdminOperation(action='auto_approve_joins').checked()
        with self.assertRaises(ValueError):
            AdminOperation(action='approve_all_joins',user='10').checked()
        for text in ['даша удаляй удаленные аккаунты автоматически всегда','удаляй удаленные аккаунты автоматически','автоматом кикай всех собачек тут','включи автоудаление удаленных аккаунтов','/autodeleted on']:
            self.assertEqual(parse_command(text)[0].model_dump(exclude_none=True),{'action':'auto_kick_deleted_accounts','enabled':True})
        for text in ['не удаляй удаленные аккаунты автоматически','перестань удалять удаленные аккаунты','отключи автоудаление удаленных аккаунтов','/autodeleted off']:
            self.assertEqual(parse_command(text)[0].model_dump(exclude_none=True),{'action':'auto_kick_deleted_accounts','enabled':False})
        for text in ['удали удаленные аккаунты','кикни всех удаленных пользователей тут','почисти чат от собачек','/kick_deleted']:
            self.assertEqual(parse_command(text)[0].model_dump(exclude_none=True),{'action':'kick_deleted_accounts'})
        with self.assertRaises(ValueError):
            AdminOperation(action='auto_kick_deleted_accounts').checked()
        with self.assertRaises(ValueError):
            AdminOperation(action='kick_deleted_accounts',user='10').checked()


class DeletedAccountsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name))
        self.store.update_settings({'owner_id': OWNER, 'enabled': True, 'proactive_enabled': False})
        self.store.upsert_contact(CHAT,'Test',kind='group')
        self.rt = Runtime(self.store)
        self.admin, self.tg = self.rt.group_admin, self.rt.telegram
        self.deleted = self.admin.deleted_accounts
        self.tg.ready = Mock(return_value=True)
        self.tg.account_id = SELF
        self.chat = types.Channel(123,'Test',types.ChatPhotoEmpty(),datetime.now(timezone.utc),megagroup=True)
        self.client = AsyncMock()
        self.tg.client = self.client
        self.client.get_entity.return_value = self.chat
        self.client.get_permissions.return_value = NS(is_creator=False,is_admin=True,is_chat=False,
            participant=NS(admin_rights=types.ChatAdminRights(ban_users=True)))
        self.client.side_effect = self.rpc
        self.client.send_message.side_effect = self.sent
        self.rt.model.completion = AsyncMock(side_effect=ValueError('API модели вернул код 502'))
        self.members = [
            (types.ChannelParticipant(10,datetime.now(timezone.utc)), types.User(10,access_hash=100,first_name='',deleted=True)),
            (types.ChannelParticipant(11,datetime.now(timezone.utc)), types.User(11,access_hash=110,first_name='Alive',deleted=False)),
            (types.ChannelParticipantAdmin(12,OWNER,OWNER,types.ChatAdminRights(ban_users=True),datetime.now(timezone.utc)),
             types.User(12,access_hash=120,first_name='',deleted=True)),
            (types.ChannelParticipantBanned(types.PeerUser(13),OWNER,datetime.now(timezone.utc),
             types.ChatBannedRights(None,send_messages=True),left=False), types.User(13,access_hash=130,first_name='',deleted=True)),
        ]
        self.last_sent = None

    async def asyncTearDown(self):
        self.tg.client = None
        await self.rt.stop()
        self.store.close()
        self.tmp.cleanup()

    def message(self,mid,user,text,topic=0):
        return types.Message(mid,types.PeerChannel(123),datetime.now(timezone.utc),text,from_id=types.PeerUser(user),
            reply_to=types.MessageReplyHeader(reply_to_msg_id=topic,forum_topic=True) if topic else None)

    def event(self,text,mid=100,actor=OWNER,topic=0):
        msg = self.message(mid,actor,text,topic)
        self.store.group_message(CHAT,mid,text,sender_id=actor,topic_id=topic)
        return NS(id=mid,chat_id=CHAT,raw_text=text,message=msg,is_group=True,is_private=False,is_reply=False,
            get_chat=AsyncMock(return_value=self.chat),get_reply_message=AsyncMock(return_value=None))

    async def handle(self,event):
        return await self.admin.handle(event,types.User(event.message.sender_id,first_name='Actor'))

    async def sent(self,chat,text,**kwargs):
        self.last_sent = self.message(900+self.client.send_message.await_count,SELF,text,kwargs.get('reply_to',0))
        return self.last_sent

    async def rpc(self,request):
        if isinstance(request,functions.channels.GetParticipantsRequest):
            slice_ = self.members[request.offset:request.offset+request.limit]
            return NS(participants=[p for p,_ in slice_],users=[u for _,u in slice_])
        if isinstance(request,functions.channels.EditBannedRequest):
            return True
        return True

    def kicks(self):
        return [call.args[0] for call in self.client.await_args_list if isinstance(call.args[0],functions.channels.EditBannedRequest)]

    def mode(self):
        return self.store.rows('SELECT * FROM group_deleted_automation WHERE chat_id=?',(CHAT,))[0]

    async def test_screenshot_command_enables_persistent_deleted_cleanup_without_model_call(self):
        event = self.event('даша удаляй удаленные аккаунты автоматически всегда',mid=3834,topic=390)
        await self.handle(event)
        await self.handle(event)
        self.rt.model.completion.assert_not_awaited()
        self.assertEqual(self.mode()['enabled'],1)
        self.assertIn('Автоудаление удалённых аккаунтов включено',self.last_sent.message)
        await self.deleted.poll()
        kicks = self.kicks()
        # Two deleted non-admin members (10 and 13), each kicked via ban + restore previous banned_rights
        self.assertEqual([r.participant.id for r in kicks],[10,10,13,13])
        self.assertTrue(kicks[0].banned_rights.view_messages)
        self.assertFalse(bool(kicks[1].banned_rights.view_messages))
        self.assertTrue(kicks[2].banned_rights.view_messages)
        self.assertFalse(bool(kicks[3].banned_rights.view_messages))
        self.assertTrue(kicks[3].banned_rights.send_messages)
        self.assertEqual(self.mode()['status'],'idle')
        self.assertGreater(self.mode()['next_check'],time.time())

    async def test_manual_cleanup_kicks_only_deleted_non_admins_and_reports_count(self):
        await self.handle(self.event('удали удаленные аккаунты',mid=200))
        self.rt.model.completion.assert_not_awaited()
        self.assertEqual([r.participant.id for r in self.kicks()],[10,10,13,13])
        self.assertIn('2',self.last_sent.message)

    async def test_disable_without_admin_rights_and_autonomy_guard_blocks_unrequested(self):
        await self.handle(self.event('удаляй удаленные аккаунты автоматически'))
        self.client.get_permissions.side_effect = errors.ChatAdminRequiredError(None)
        await self.handle(self.event('не удаляй удаленные аккаунты автоматически',101))
        self.assertEqual(self.mode()['enabled'],0)
        self.rt.telegram_autonomy.allowed = Mock(return_value=True)
        self.store.execute('UPDATE contacts SET proactive=1 WHERE id=?',(CHAT,))
        for action in ['auto_kick_deleted_accounts','kick_deleted_accounts']:
            op = TelegramAction(action='group',recipient=str(CHAT),operation={'action':action,**({'enabled':True} if action=='auto_kick_deleted_accounts' else {})})
            with self.assertRaisesRegex(AutonomyStopped,'команде владельца'):
                self.rt.telegram_autonomy.guard(op,CHAT,self.store.dialog_scope(CHAT,0),self.store.settings()['telegram_autonomy_revision'],'autonomous')

    async def test_lost_acknowledgement_disables_instead_of_looping(self):
        await self.handle(self.event('удаляй удаленные аккаунты автоматически'))
        base = self.rpc
        async def lose_ack(request):
            if isinstance(request,functions.channels.EditBannedRequest):
                raise ConnectionError('ack lost')
            return await base(request)
        self.client.side_effect = lose_ack
        await self.deleted.poll()
        self.assertEqual(self.mode()['enabled'],0)
        self.assertEqual(self.mode()['status'],'uncertain')
