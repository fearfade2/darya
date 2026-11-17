import asyncio
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

from telethon import types, functions

from app.authority import authorized
from app.group_admin import AdminOperation, parse_command
from app.group_warnings import parse_warning_command
from app.store import Store
from telegram_fixtures import TelegramFixture, GROUP, OWNER, SELF, message

ADMIN=30


class GroupWarningTests(TelegramFixture):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.admin=self.rt.group_admin
        self.warnings=self.admin.warnings
        self.users[ADMIN]=types.User(ADMIN,first_name='Admin')
        self.member=NS(is_admin=False,is_creator=False)
        self.administrator=NS(is_admin=True,is_creator=False,is_chat=False,
            participant=NS(admin_rights=types.ChatAdminRights(ban_users=True)))
        self.roles={SELF:self.administrator,ADMIN:self.administrator}
        self.client.get_permissions.side_effect=lambda chat,user:self.roles.get(getattr(user,'id',user),self.member)
        self.admin.groups.resolve=AsyncMock(return_value=self.channel)
        self.serial=500
        async def sent(chat,text,**kwargs):
            self.serial+=1
            return message(id=self.serial,chat=GROUP,sender=SELF,out=True,text=text)
        self.client.send_message.side_effect=sent
        self.store.update_settings({'group_enabled':True})
        await self.configure()
        self.client.reset_mock()

    async def configure(self,**kwargs):
        return await self.warnings.execute(self.channel,
            AdminOperation(action='configure_warnings',enabled=True,**kwargs).checked(),Mock(),actor_id=OWNER)

    def event(self,text='варн за спам',sender=OWNER,id=100,reply_id=80,reply_user=20):
        current=message(id=id,chat=GROUP,sender=sender,text=text)
        reply=message(id=reply_id,chat=GROUP,sender=reply_user,text='violation') if reply_id else None
        return NS(id=id,chat_id=GROUP,raw_text=text,message=current,is_group=True,is_private=False,is_reply=bool(reply),
                  get_chat=AsyncMock(return_value=self.channel),get_reply_message=AsyncMock(return_value=reply))

    async def handle(self,event):
        return await self.admin.handle(event,self.users[event.message.sender_id])

    def count(self,user=20):
        return self.warnings.count(self.warnings.policy(GROUP),user)

    def bans(self):
        return [call for call in self.client.await_args_list if isinstance(call.args[0],functions.channels.EditBannedRequest)]

    def decision(self,event,violation=True,quote='Реклама запрещена',reason='Новая рекламная ссылка'):
        return {'content':json.dumps({'violation':violation,'rule_quote':quote,'reason':reason,'message_id':event.id})}

    def age_warnings(self):
        self.store.execute('UPDATE group_warning_events SET created=created-61')

    async def test_screenshot_rule_is_a_real_native_operation(self):
        op=parse_command('запомни: через 3 варна бан')[0]
        self.assertEqual(op.action,'configure_warnings')
        self.assertEqual(op.threshold,3)
        self.assertTrue(await self.handle(self.event('запомни: через 3 варна бан',reply_id=None)))
        self.assertEqual(self.warnings.policy(GROUP)['threshold'],3)
        self.rt.model.completion.assert_not_awaited()

    async def test_three_owner_commands_produce_one_ban_after_three_delivered_warnings(self):
        for index in range(3):
            await self.handle(self.event(id=100+index,reply_id=80+index))
        self.assertEqual(self.count(),3)
        self.assertEqual(len(self.bans()),1)
        self.assertTrue(self.bans()[0].args[0].banned_rights.view_messages)
        rows=self.store.rows('SELECT * FROM group_warning_events ORDER BY id')
        self.assertEqual([row['status'] for row in rows],['sent']*3)
        self.assertEqual(rows[-1]['ban_status'],'sent')
        self.assertEqual(self.client.send_message.await_count,4)

    async def test_current_telegram_admin_can_warn_without_global_control(self):
        self.assertFalse(authorized(self.store,ADMIN,GROUP))
        await self.handle(self.event(sender=ADMIN))
        self.assertEqual(self.count(),1)
        self.assertFalse(authorized(self.store,ADMIN,GROUP))
        self.assertFalse(authorized(self.store,ADMIN))
        await self.handle(self.event('забань 20',sender=ADMIN,id=101))
        self.assertEqual(len(self.bans()),0)

    async def test_stranger_and_admin_of_another_group_cannot_issue_warns(self):
        await self.handle(self.event(sender=10))
        self.assertEqual(self.count(),0)
        self.client.send_message.assert_not_awaited()
        self.roles[ADMIN]=self.member
        await self.handle(self.event(sender=ADMIN,id=101))
        self.assertEqual(self.count(),0)

    async def test_admin_cannot_change_owner_rules_or_threshold(self):
        await self.handle(self.event('запомни: через 1 варн бан',sender=ADMIN))
        self.assertEqual(self.warnings.policy(GROUP)['threshold'],3)
        self.assertEqual(self.warnings.policy(GROUP)['rules'],'')

    async def test_foreign_reply_cannot_select_someone_from_this_group(self):
        event=self.event()
        event.get_reply_message.return_value=message(id=80,chat=10,sender=20)
        await self.handle(event)
        self.assertEqual(self.count(),0)

    async def test_owner_self_and_admin_targets_are_protected(self):
        for index,target in enumerate([OWNER,SELF,ADMIN]):
            await self.handle(self.event(id=100+index,reply_user=target))
            self.assertEqual(self.count(target),0)
        self.assertEqual(len(self.bans()),0)

    async def test_forwarded_edited_quoted_and_old_commands_do_not_execute(self):
        events=[self.event(id=100+i) for i in range(4)]
        events[0].message.fwd_from=types.MessageFwdHeader(datetime.now(timezone.utc),from_id=types.PeerUser(OWNER))
        events[1].message.edit_date=datetime.now(timezone.utc)
        events[2].message.entities=[types.MessageEntityBlockquote(0,4)]
        events[3].message.date=datetime.now(timezone.utc)-timedelta(minutes=10)
        for event in events:
            await self.handle(event)
        self.assertEqual(self.count(),0)
        self.client.send_message.assert_not_awaited()

    async def test_duplicate_update_or_same_evidence_cannot_count_twice(self):
        event=self.event()
        await self.handle(event)
        await self.handle(event)
        await self.handle(self.event(id=101,reply_id=80,sender=ADMIN))
        self.assertEqual(self.count(),1)
        self.assertEqual(len(self.store.rows('SELECT * FROM group_warning_events')),1)

    async def test_concurrent_admin_commands_share_one_evidence_counter(self):
        await asyncio.gather(self.handle(self.event(id=100)),self.handle(self.event(id=101,sender=ADMIN)))
        self.assertEqual(self.count(),1)

    async def test_unwarn_and_reset_do_not_unban(self):
        await self.handle(self.event(id=100,reply_id=80))
        await self.handle(self.event(id=101,reply_id=81))
        await self.handle(self.event('сними варн',id=102,reply_id=81))
        self.assertEqual(self.count(),1)
        await self.handle(self.event('сбрось варны',id=103,reply_id=81))
        self.assertEqual(self.count(),0)
        self.assertEqual(len(self.bans()),0)

    async def test_counts_and_rules_survive_a_new_store(self):
        await self.configure(rules='Реклама запрещена')
        await self.handle(self.event())
        second=Store(self.store.directory)
        try:
            self.assertEqual(second.rows('SELECT rules FROM group_warning_policies')[0]['rules'],'Реклама запрещена')
            self.assertEqual(second.rows("SELECT COUNT(*) AS n FROM group_warning_events WHERE status='sent' AND active=1")[0]['n'],1)
        finally:
            second.close()

    async def test_uncertain_warning_delivery_is_not_counted_or_retried(self):
        self.client.send_message.side_effect=ConnectionError('lost acknowledgment')
        event=self.event()
        await self.handle(event)
        first=self.client.send_message.await_count
        await self.handle(event)
        self.assertEqual(self.count(),0)
        self.assertEqual(self.client.send_message.await_count,first)
        self.assertEqual(self.store.rows('SELECT status FROM group_warning_events')[0]['status'],'uncertain')
        self.assertEqual(len(self.bans()),0)

    async def test_foreign_warning_ack_never_counts_towards_a_ban(self):
        self.client.send_message.side_effect=None
        self.client.send_message.return_value=message(id=500,chat=10,sender=SELF,out=True)
        await self.handle(self.event())
        self.assertEqual(self.count(),0)
        self.assertEqual(len(self.bans()),0)

    async def test_ban_ack_loss_is_recorded_and_not_retried(self):
        await self.handle(self.event(id=100,reply_id=80))
        await self.handle(self.event(id=101,reply_id=81))
        self.client.side_effect=ConnectionError('lost ban acknowledgment')
        await self.handle(self.event(id=102,reply_id=82))
        await self.handle(self.event(id=103,reply_id=83))
        self.assertEqual(self.count(),3)
        self.assertEqual(len(self.bans()),1)
        self.assertEqual(self.store.rows('SELECT ban_status FROM group_warning_events ORDER BY id DESC LIMIT 1')[0]['ban_status'],'uncertain')

    async def test_admin_permission_revoked_after_third_warning_stops_ban(self):
        await self.handle(self.event(id=100,reply_id=80,sender=ADMIN))
        await self.handle(self.event(id=101,reply_id=81,sender=ADMIN))
        sent=self.client.send_message.side_effect
        async def revoke(chat,text,**kwargs):
            result=await sent(chat,text,**kwargs)
            if text.startswith('Варн 3/'):
                self.roles[ADMIN]=self.member
            return result
        self.client.send_message.side_effect=revoke
        await self.handle(self.event(id=102,reply_id=82,sender=ADMIN))
        self.assertEqual(self.count(),3)
        self.assertEqual(len(self.bans()),0)

    async def test_promoted_target_after_warning_is_not_banned(self):
        await self.handle(self.event(id=100,reply_id=80))
        await self.handle(self.event(id=101,reply_id=81))
        sent=self.client.send_message.side_effect
        async def promote(chat,text,**kwargs):
            result=await sent(chat,text,**kwargs)
            if text.startswith('Варн 3/'):
                self.roles[20]=self.administrator
            return result
        self.client.send_message.side_effect=promote
        await self.handle(self.event(id=102,reply_id=82))
        self.assertEqual(self.count(),3)
        self.assertEqual(len(self.bans()),0)

    async def test_no_rules_means_no_automatic_assumptions_or_model_calls(self):
        event=self.event('купи рекламу',sender=20,reply_id=None)
        self.assertFalse(await self.warnings.consider(event,self.users[20]))
        self.rt.model.completion.assert_not_awaited()
        self.assertEqual(self.count(),0)

    async def test_owner_rules_are_saved_and_extended_by_chat_commands(self):
        await self.handle(self.event('запомни правила: Реклама запрещена',id=100,reply_id=None))
        await self.handle(self.event('за флуд выдавай варн',id=101,reply_id=None))
        rules=self.warnings.policy(GROUP)['rules']
        self.assertIn('Реклама запрещена',rules)
        self.assertIn('за флуд выдавай варн',rules)
        self.rt.model.completion.assert_not_awaited()

    async def test_automatic_and_manual_warnings_share_the_three_warning_ban(self):
        await self.configure(rules='Реклама запрещена')
        await self.handle(self.event(id=100,reply_id=80))
        self.age_warnings()
        event=self.event('Купите подписку по ссылке',sender=20,id=81,reply_id=None)
        self.rt.model.completion.return_value=self.decision(event)
        self.assertTrue(await self.warnings.consider(event,self.users[20]))
        await self.handle(self.event(id=102,reply_id=82,sender=ADMIN))
        self.assertEqual(self.count(),3)
        self.assertEqual(len(self.bans()),1)
        self.assertEqual([r['automatic'] for r in self.store.rows('SELECT automatic FROM group_warning_events ORDER BY id')],[0,1,0])

    async def test_malformed_or_invented_rule_decisions_never_warn(self):
        await self.configure(rules='Реклама запрещена')
        event=self.event('обычная беседа',sender=20,reply_id=None)
        for answer in [{'content':'not JSON'},self.decision(event,quote='Запрещён мат'),
                       self.decision(event,violation=False),{'tool_calls':[{'function':{'name':'warn'}}]},
                       {'content':json.dumps({'violation':True,'rule_quote':'Реклама запрещена','reason':'x','message_id':1})}]:
            self.rt.model.completion.return_value=answer
            self.assertFalse(await self.warnings.consider(event,self.users[20]))
        self.assertEqual(self.count(),0)
        self.client.send_message.assert_not_awaited()

    async def test_rule_change_while_model_is_thinking_cancels_auto_warning(self):
        await self.configure(rules='Реклама запрещена')
        event=self.event('Купите подписку',sender=20,reply_id=None)
        async def changed(*args,**kwargs):
            self.store.execute('UPDATE group_warning_policies SET enabled=0,revision=revision+1')
            return self.decision(event)
        self.rt.model.completion.side_effect=changed
        self.assertFalse(await self.warnings.consider(event,self.users[20]))
        self.assertEqual(self.count(),0)

    async def test_auto_warning_interval_gives_participant_time_to_stop(self):
        await self.configure(rules='Реклама запрещена')
        first=self.event('Купите подписку',sender=20,id=80,reply_id=None)
        self.rt.model.completion.return_value=self.decision(first)
        self.assertTrue(await self.warnings.consider(first,self.users[20]))
        second=self.event('Купите подписку ещё',sender=20,id=81,reply_id=None)
        self.assertFalse(await self.warnings.consider(second,self.users[20]))
        self.assertEqual(self.count(),1)
        self.rt.model.completion.assert_awaited_once()

    async def test_auto_review_is_scheduled_without_waiting_for_the_model(self):
        await self.configure(rules='Реклама запрещена')
        event=self.event('Привет',sender=20,reply_id=None)
        blocked=asyncio.Event()
        async def wait(*args,**kwargs):
            await blocked.wait()
            return self.decision(event,violation=False)
        self.rt.model.completion.side_effect=wait
        self.warnings.schedule(event,self.users[20])
        self.assertEqual(len(self.warnings.pending),1)
        self.warnings.schedule(event,self.users[20])
        self.assertEqual(len(self.warnings.pending),1)
        blocked.set()
        await asyncio.gather(*list(self.rt.work))
        self.assertEqual(self.warnings.pending,set())

    async def test_restart_marks_inflight_warning_and_ban_uncertain(self):
        await self.handle(self.event())
        self.store.execute("UPDATE group_warning_events SET status='sending',ban_status='sending'")
        second=Store(self.store.directory)
        try:
            row=second.rows('SELECT status,ban_status FROM group_warning_events')[0]
            self.assertEqual(row,{'status':'uncertain','ban_status':'uncertain'})
        finally:
            second.close()

    async def test_parsers_do_not_enable_rules_from_questions_or_negation(self):
        for text in ['через 3 варна','через 3 варна бан?','не выдавай варн','он сказал через 3 варна бан','варн @x']:
            self.assertIsNone(parse_warning_command(text),text)
        self.assertEqual(parse_warning_command('/warn @validname за рекламу')['user'],'@validname')
        self.assertEqual(parse_warning_command('сними варн 20')['user'],'20')

    async def test_policy_changes_do_not_erase_existing_counts(self):
        await self.handle(self.event())
        await self.configure(rules='Реклама запрещена',threshold=3)
        self.assertEqual(self.count(),1)
        with self.assertRaises(ValueError):
            AdminOperation(action='configure_warnings',threshold=0).checked()
