import asyncio
import json
import tempfile
import time
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch

from telethon import errors, functions, types

from app.ai import Model, TOOLS
from app.control import OWNER_NAMES
from app.dialogs import current_dialog
from app.runtime import Runtime
from app.store import Store

OWNER = 1000000001


class ContactBlockTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name))
        self.store.update_settings({'owner_id': OWNER, 'enabled': True, 'natural_enabled': False, 'typing_enabled': False,
                                    'min_interval': 0, 'online_during_reply': False, 'group_enabled': True})
        for user in [10, 20, OWNER]:
            self.store.upsert_contact(user, str(user))
        self.store.upsert_contact(-100, 'group', kind='group')
        self.rt = Runtime(self.store)
        self.rt.telegram.ready = Mock(return_value=True)
        self.rt.telegram.account_id = 30
        self.client = AsyncMock(return_value=True)
        self.client.get_input_entity.side_effect = lambda user: types.InputPeerUser(user, 123)
        self.rt.telegram.client = self.client
        self.rt.telegram.send = AsyncMock(return_value=900)
        self.rt.telegram.read = AsyncMock()
        self.rt.telegram.resolve = AsyncMock(return_value={'id': 10, 'name': '10', 'kind': 'private'})

    async def asyncTearDown(self):
        await self.rt.stop()
        self.store.close()
        self.tmp.cleanup()

    def request(self, chat=10, user=10, msg=1, warned=True, **kw):
        self.store.message(chat, 'user', 'Новая реплика', msg, sender_id=user, **kw)
        self.rt.reply_states[chat] = {'telegram_id': msg, 'superseded': False}
        if warned:
            self.store.execute('INSERT OR IGNORE INTO contact_warnings VALUES (?,?,?,?,?,?)',
                               (user, chat, 0, 999, time.time() - 60, 'harassment'))

    async def test_first_violation_warns_and_only_later_violation_blocks(self):
        from app.contact_blocks import WARNING
        self.request(warned=False)
        self.rt.reply_states.clear()
        self.rt.model.completion = AsyncMock(return_value={'tool_calls': [
            {'id': 'b', 'function': {'name': 'block_current_user', 'arguments': '{"reason":"insults"}'}}]})
        await self.rt.reply(10, telegram_id=1, prepared=True)
        self.client.assert_not_awaited()
        self.rt.telegram.send.assert_awaited_once()
        self.assertEqual(self.rt.telegram.send.call_args.args[1], WARNING)
        self.assertTrue(self.store.rows('SELECT * FROM contact_warnings WHERE user_id=10'))
        self.request(msg=2, warned=False)
        self.assertTrue((await self.block(msg=2))['blocked'])

    async def test_undelivered_warning_never_authorizes_blocking(self):
        self.request(warned=False)
        result = await self.block()
        self.assertTrue(result['warning_required'])
        self.assertFalse(self.store.rows('SELECT * FROM contact_warnings'))
        self.request(msg=2, warned=False)
        result = await self.block(msg=2)
        self.assertTrue(result['warning_required'])
        self.client.assert_not_awaited()

    async def test_message_before_warning_delivery_is_not_a_second_violation(self):
        self.request(warned=False)
        self.store.execute('INSERT INTO contact_warnings VALUES (?,?,?,?,?,?)', (10, 10, 0, 999, time.time() + 1, 'insults'))
        self.assertTrue((await self.block())['warning_required'])
        self.client.assert_not_awaited()

    async def test_expired_warning_requires_a_new_warning(self):
        self.request()
        self.store.execute('UPDATE contact_warnings SET warned_at=?',(time.time()-86401,))
        self.assertTrue((await self.block())['warning_required'])
        self.client.assert_not_awaited()

    async def test_normal_reply_after_warning_does_not_block(self):
        self.request()
        self.rt.reply_states.clear()
        self.rt.model.completion=AsyncMock(return_value={'content':'Хорошо, продолжим спокойно.'})
        await self.rt.reply(10,telegram_id=1,prepared=True)
        self.client.assert_not_awaited()
        self.assertFalse(self.store.user_blocked(10))

    async def test_voice_warning_render_failure_is_retryable_and_not_a_delivered_warning(self):
        from app.voice_messages import VoiceRenderUnavailable
        self.request(warned=False)
        self.rt.reply_states.clear()
        self.store.set_reply_mode(10,10,'voice')
        self.rt.model.completion=AsyncMock(return_value={'tool_calls':[
            {'id':'b','function':{'name':'block_current_user','arguments':'{"reason":"insults"}'}}]})
        @asynccontextmanager
        async def unavailable(*args,**kwargs):
            raise VoiceRenderUnavailable('temporary')
            yield
        with patch('app.runtime.voice_note',unavailable):
            await self.rt.reply(10,telegram_id=1,prepared=True)
        self.client.assert_not_awaited()
        self.assertFalse(self.store.rows('SELECT * FROM contact_warnings'))
        self.assertEqual(self.store.rows('SELECT status FROM pending_replies')[0]['status'],'pending')

    async def test_failed_text_warning_delivery_does_not_allow_a_block(self):
        self.request(warned=False)
        self.rt.reply_states.clear()
        self.rt.telegram.send.side_effect=ConnectionError('unknown')
        self.rt.model.completion=AsyncMock(return_value={'tool_calls':[
            {'id':'b','function':{'name':'block_current_user','arguments':'{"reason":"insults"}'}}]})
        await self.rt.reply(10,telegram_id=1,prepared=True)
        self.request(msg=2,warned=False)
        self.assertTrue((await self.block(msg=2))['warning_required'])
        self.client.assert_not_awaited()

    async def test_delivered_voice_warning_is_recorded_before_any_later_block(self):
        from app.contact_blocks import WARNING
        self.request(warned=False)
        self.rt.reply_states.clear()
        self.store.set_reply_mode(10,10,'voice')
        self.rt.telegram.send_voice=AsyncMock(return_value=901)
        self.rt.model.completion=AsyncMock(return_value={'tool_calls':[
            {'id':'b','function':{'name':'block_current_user','arguments':'{"reason":"insults"}'}}]})
        @asynccontextmanager
        async def rendered(*args,**kwargs):
            yield Path('voice.ogg'),2
        with patch('app.runtime.voice_note',rendered):
            await self.rt.reply(10,telegram_id=1,prepared=True)
        self.assertEqual(self.store.history(10)[-1]['text'],WARNING)
        self.assertEqual(self.store.rows('SELECT sent_message_id FROM contact_warnings')[0]['sent_message_id'],901)
        self.client.assert_not_awaited()
        self.request(msg=2,warned=False)
        self.assertTrue((await self.block(msg=2))['blocked'])

    async def block(self, chat=10, msg=1):
        return await self.rt.action(chat, msg, 'block_current_user', {'reason': 'harassment'})

    async def test_real_block_rpc_targets_current_user_and_stops_further_contact(self):
        self.request()
        reminder = self.store.schedule(10, 'Старое напоминание', time.time() + 100)
        before = self.store.contact(10)
        result = await self.block()
        self.assertTrue(result['blocked'])
        rpc = self.client.call_args.args[0]
        self.assertIsInstance(rpc, functions.contacts.BlockRequest)
        self.assertEqual(rpc.id.user_id, 10)
        self.assertTrue(self.store.user_blocked(10))
        self.assertFalse(self.rt.receive(10, 'Ещё сообщение', 2, sender_id=10))
        with self.assertRaises(ValueError):
            await self.rt.send(10, 'Не должно отправиться')
        self.assertEqual(self.store.rows('SELECT status FROM tasks WHERE id=?', (reminder,))[0]['status'], 'cancelled')
        self.assertEqual(self.store.contact(10)['enabled'], before['enabled'])

    async def test_owner_is_never_blocked_in_private_or_group(self):
        for chat in [OWNER, -100]:
            self.request(chat, OWNER)
            with self.assertRaises(ValueError):
                await self.block(chat)
        self.client.assert_not_awaited()
        self.assertFalse(self.store.user_blocked(OWNER))

    async def test_cannot_block_arbitrary_target_forward_or_wrong_private_sender(self):
        self.request()
        with self.assertRaises(ValueError):
            await self.rt.action(10, 1, 'block_current_user', {'reason': 'insults', 'user_id': OWNER})
        self.request(10, 10, 2, forwarded=True)
        with self.assertRaises(ValueError):
            await self.block(10, 2)
        self.request(10, 20, 3)
        with self.assertRaises(ValueError):
            await self.block(10, 3)
        self.client.assert_not_awaited()

    async def test_stale_archived_superseded_and_proactive_requests_are_rejected(self):
        self.request(now=time.time() - 301)
        with self.assertRaises(ValueError):
            await self.block()
        self.request(msg=2)
        self.rt.reply_states[10]['superseded'] = True
        with self.assertRaises(ValueError):
            await self.block(10, 2)
        self.rt.reply_states.clear()
        with self.assertRaises(ValueError):
            await self.block(10, 2)
        self.request(msg=3)
        self.store.archive_contexts()
        with self.assertRaises(ValueError):
            await self.block(10, 3)
        self.client.assert_not_awaited()

    async def test_rechecks_owner_and_new_message_after_resolving_peer(self):
        self.request()
        async def changed(user):
            self.store.update_settings({'owner_id': 10})
            return types.InputPeerUser(user, 123)
        self.client.get_input_entity.side_effect = changed
        with self.assertRaises(ValueError):
            await self.block()
        self.store.update_settings({'owner_id': OWNER})
        async def newer(user):
            self.store.message(10, 'user', 'Извини', 2, sender_id=10)
            return types.InputPeerUser(user, 123)
        self.client.get_input_entity.side_effect = newer
        with self.assertRaises(ValueError):
            await self.block()
        self.client.assert_not_awaited()

    async def test_group_block_targets_author_and_leaves_others_unaffected(self):
        self.request(-100, 10)
        await self.block(-100)
        self.assertEqual(self.client.call_args.args[0].id.user_id, 10)
        self.assertFalse(self.rt.receive(-100, 'Даша!', 2, sender_id=10))
        token = current_dialog.set(self.store.dialog_scope(-100, 20))
        try:
            await self.rt.send(-100, 'Ответ другому участнику', reply_to=3)
        finally:
            current_dialog.reset(token)
        self.rt.telegram.send.assert_awaited_once()
        self.assertFalse(self.store.user_blocked(20))

    async def test_block_is_deduplicated_and_survives_restart(self):
        self.request()
        await self.block()
        await self.block()
        self.client.assert_awaited_once()
        other = Store(self.store.directory)
        try:
            self.assertTrue(other.user_blocked(10))
        finally:
            other.close()

    async def test_uncertain_block_is_not_retried_and_suppresses_replies(self):
        self.request()
        self.client.side_effect = ConnectionError('lost')
        result = await self.block()
        self.assertEqual(result['status'], 'uncertain')
        self.assertTrue(result['stop_reply'])
        self.assertTrue(self.store.user_blocked(10))
        await self.block()
        self.client.assert_awaited_once()

    async def test_rpc_rejection_does_not_claim_or_record_success(self):
        self.request()
        self.client.side_effect = errors.UserIdInvalidError(None)
        with self.assertRaises(ValueError):
            await self.block()
        self.assertFalse(self.store.user_blocked(10))
        self.assertEqual(self.store.rows('SELECT status FROM contact_blocks')[0]['status'], 'failed')

    async def test_setting_disables_new_blocks_but_preserves_existing_ones(self):
        self.request()
        self.store.update_settings({'self_protection_enabled': False})
        with self.assertRaises(ValueError):
            await self.block()
        self.store.update_settings({'self_protection_enabled': True})
        await self.block()
        self.store.update_settings({'self_protection_enabled': False})
        self.assertTrue(self.store.user_blocked(10))

    async def test_model_stops_after_block_without_farewell_or_later_tools(self):
        self.request()
        self.rt.reply_states.clear()
        self.rt.model.completion = AsyncMock(return_value={'tool_calls': [
            {'id': 'b', 'function': {'name': 'block_current_user', 'arguments': '{"reason":"insults"}'}},
            {'id': 'r', 'function': {'name': 'react', 'arguments': '{"emoji":"❤"}'}}]})
        await self.rt.reply(10, telegram_id=1, prepared=True)
        self.assertTrue(self.store.user_blocked(10))
        self.rt.model.completion.assert_awaited_once()
        self.rt.telegram.send.assert_not_awaited()
        self.client.assert_awaited_once()

    async def test_owner_can_list_and_unblock_without_changing_contact_preferences(self):
        self.request()
        await self.block()
        self.store.message(OWNER, 'user', 'Разблокируй 10', 5, sender_id=OWNER)
        result = await self.rt.control.action(OWNER, 5, 'list_blocked_users', {}, trusted=True)
        self.assertEqual(result['blocked'][0]['user_id'], 10)
        before = self.store.contact(10)
        result = await self.rt.control.action(OWNER, 5, 'unblock_user', {'recipient': '10'}, trusted=True)
        self.assertTrue(result['unblocked'])
        self.assertIsInstance(self.client.call_args.args[0], functions.contacts.UnblockRequest)
        self.assertFalse(self.store.user_blocked(10))
        self.assertEqual(self.store.contact(10)['enabled'], before['enabled'])
        self.assertEqual((await self.block())['status'], 'unblocked')
        self.assertEqual(self.client.await_count, 2)

    async def test_unblock_tools_are_owner_only_and_parse_without_model(self):
        self.assertTrue({'unblock_user', 'list_blocked_users'} <= OWNER_NAMES)
        self.assertEqual(self.rt.control.parse(['разблокируй @example']), [{'name': 'unblock_user', 'args': {'recipient': '@example'}}])
        self.assertEqual(self.rt.control.parse(['покажи заблокированных']), [{'name': 'list_blocked_users', 'args': {}}])
        self.request()
        for name, args in [('list_blocked_users', {}), ('unblock_user', {'recipient': '10'})]:
            with self.assertRaises(ValueError):
                await self.rt.control.action(10, 1, name, args, trusted=True)
        self.client.assert_not_awaited()

    async def test_false_unblock_receipt_reconciles_confirmed_unblocked_user(self):
        self.request()
        await self.block()
        self.store.message(OWNER, 'user', 'Разблокируй 10', 5, sender_id=OWNER)
        self.client.reset_mock()
        self.client.side_effect = lambda request: (
            False if isinstance(request, functions.contacts.UnblockRequest)
            else NS(full_user=NS(blocked=False)))
        result = await self.rt.control.action(OWNER, 5, 'unblock_user', {'recipient': '10'}, trusted=True)
        self.assertTrue(result['unblocked'])
        self.assertFalse(self.store.user_blocked(10))
        self.assertFalse(self.store.rows('SELECT * FROM contact_warnings WHERE user_id=10'))
        self.assertEqual(self.store.rows('SELECT status FROM owner_actions')[0]['status'], 'sent')
        requests = [call.args[0] for call in self.client.await_args_list]
        self.assertEqual(sum(isinstance(r, functions.contacts.UnblockRequest) for r in requests), 1)
        self.assertIsInstance(requests[1], functions.users.GetFullUserRequest)
        self.assertEqual(requests[1].id.user_id, 10)

    async def test_false_unblock_receipt_keeps_block_when_telegram_still_blocks_user(self):
        self.request()
        await self.block()
        self.store.message(OWNER, 'user', 'Разблокируй 10', 5, sender_id=OWNER)
        self.client.side_effect = lambda request: (
            False if isinstance(request, functions.contacts.UnblockRequest)
            else NS(full_user=NS(blocked=True)))
        with self.assertRaisesRegex(ValueError, 'всё ещё заблокирован'):
            await self.rt.control.action(OWNER, 5, 'unblock_user', {'recipient': '10'}, trusted=True)
        self.assertTrue(self.store.user_blocked(10))
        action = self.store.rows('SELECT * FROM owner_actions')[0]
        self.assertEqual(action['status'], 'uncertain')
        self.assertIn('всё ещё заблокирован', json.loads(action['result'])['error'])

    async def test_failed_unblock_verification_keeps_block_and_does_not_retry_mutation(self):
        self.request()
        await self.block()
        self.client.reset_mock()
        async def response(request):
            if isinstance(request, functions.contacts.UnblockRequest):
                return False
            raise ConnectionError('lost')
        self.client.side_effect = response
        with self.assertRaises(ConnectionError):
            await self.rt.contact_blocks.unblock('10', lambda: None)
        self.assertTrue(self.store.user_blocked(10))
        self.assertEqual(self.client.await_count, 2)

    async def test_blocked_voice_upload_does_not_dispatch_after_block(self):
        self.request()
        @asynccontextmanager
        async def action(*args):
            yield
        self.client.action = action
        async def uploaded(*args):
            await self.block()
            return 'file'
        self.client.upload_file.side_effect = uploaded
        with self.assertRaises(ValueError):
            await self.rt.telegram.send_voice(10, Path('voice.ogg'), 1)
        self.client.send_file.assert_not_awaited()

    async def test_prompts_allow_boundaries_without_sharing_owner_data(self):
        ordinary = str(self.rt.model.context(10))
        owner = str(self.rt.model.context(OWNER))
        proactive = str(self.rt.model.context(10, proactive=True))
        self.assertIn('Личная блокировка текущего собеседника разрешена: True', ordinary)
        self.assertIn('Личная блокировка текущего собеседника разрешена: False', owner)
        self.assertIn('Личная блокировка текущего собеседника разрешена: False', proactive)
        schema = next(tool['function']['parameters'] for tool in TOOLS if tool['function']['name'] == 'block_current_user')
        self.assertEqual(set(schema['properties']), {'reason'})
        self.assertFalse(schema['additionalProperties'])
