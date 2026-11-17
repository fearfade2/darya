import asyncio
import io
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

from PIL import Image
from telethon import errors, functions, types, utils

from app.ai import OWNER_TOOLS, TOOLS
from app.dialogs import current_dialog
from app.runtime import Runtime
from app.store import Store
from app.telegram_account import ACCOUNT_NAMES

OWNER = 1000000001
SELF = 1000000002
GROUP = -123
NOW = datetime.now(timezone.utc)


class TelegramAccountTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name))
        self.store.update_settings({'owner_id': OWNER})
        self.rt = Runtime(self.store)
        self.tg = self.rt.telegram
        self.tg.ready = Mock(return_value=True)
        self.tg.account_id = SELF
        self.users = {i: types.User(i, first_name=f'User{i}', username=f'user{i}', access_hash=123,
                                    phone='PRIVATE_PHONE') for i in (OWNER, SELF, 10, 20)}
        self.group = types.Chat(123, 'Group', types.ChatPhotoEmpty(), 3, NOW, 1)
        for i, user in self.users.items():
            self.store.upsert_contact(i, user.first_name, user.username)
        self.store.upsert_contact(GROUP, 'Group', kind='group')
        self.client = AsyncMock()
        self.client.get_entity.side_effect = self.entity
        self.client.get_me.return_value = self.users[SELF]
        self.client.get_input_entity.side_effect = lambda user: utils.get_input_peer(user)
        self.client.side_effect = self.rpc
        self.photo = types.Photo(101, 202, b'private-file-reference', NOW, [], 2)
        self.client.get_profile_photos.return_value = [self.photo]
        self.client.get_messages.side_effect = self.messages
        self.client.edit_message.side_effect = lambda entity, msg, text, **kw: self.message(msg, utils.get_peer_id(entity), True, text)
        self.client.delete_messages.return_value = [types.messages.AffectedMessages(1, 1)]
        self.client.send_read_acknowledge.return_value = True
        self.client.forward_messages.return_value = [self.message(800, 20, True, 'Original')]
        self.tg.client = self.client
        self.rules = [types.PrivacyValueAllowUsers([20]), types.PrivacyValueAllowContacts(), types.PrivacyValueDisallowAll()]
        self.rt.model.inspect_image = AsyncMock(return_value='На фото красный квадрат.')
        self.request_id = 0
        self.request()

    async def asyncTearDown(self):
        self.tg.client = None
        await self.rt.stop()
        self.store.close()
        self.tmp.cleanup()

    def request(self, sender=OWNER, forwarded=False):
        self.request_id += 1
        self.store.message(sender, 'user', 'Текущее поручение', self.request_id, sender_id=sender, forwarded=forwarded)
        return self.request_id

    def entity(self, value):
        if value in {'me', 'self'}:
            return self.users[SELF]
        if value == GROUP:
            return self.group
        if isinstance(value, str) and value.lstrip('@').startswith('user'):
            value = int(value.lstrip('@')[4:])
        return self.users[value]

    def rpc(self, request):
        if isinstance(request, functions.users.GetFullUserRequest):
            return NS(full_user=NS(about='Публичное био', birthday=None, blocked=False, personal_channel_id=None))
        if isinstance(request, functions.account.GetPrivacyRequest):
            return NS(rules=self.rules)
        if isinstance(request, functions.account.SetPrivacyRequest):
            return NS(rules=[types.PrivacyValueAllowContacts(), types.PrivacyValueDisallowAll()])
        if isinstance(request, functions.account.UpdateUsernameRequest):
            self.users[SELF].username = request.username
            return self.users[SELF]
        return True

    def message(self, message_id=501, chat_id=10, outgoing=True, text='Original'):
        peer = types.PeerUser(chat_id) if chat_id > 0 else types.PeerChat(-chat_id)
        return types.Message(message_id, peer, NOW, message=text, out=outgoing,
                             from_id=types.PeerUser(SELF if outgoing else 10))

    def messages(self, entity, ids=None, **kwargs):
        peer_id = utils.get_peer_id(entity)
        return [self.message(i, peer_id) for i in (ids or [501])]

    async def act(self, name, args, **kwargs):
        return await self.rt.action(OWNER, self.request_id, name, args, owner_command=True, **kwargs)

    def mutations(self, cls):
        return [call.args[0] for call in self.client.await_args_list if isinstance(call.args[0], cls)]

    async def test_account_tools_require_owner_permission_and_private_unforwarded_command(self):
        self.request(10)
        for name in ACCOUNT_NAMES | {'speak_in_call'}:
            with self.subTest(name=name):
                for chat, trusted in [(10, True), (OWNER, False), (GROUP, True)]:
                    with self.assertRaisesRegex(ValueError, 'владельцу'):
                        await self.rt.action(chat, self.request_id, name, {}, owner_command=trusted)
        self.request(OWNER, forwarded=True)
        with self.assertRaisesRegex(ValueError, 'владельцу'):
            await self.act('manage_profile', {'action': 'username', 'username': 'new_name'})
        self.client.get_entity.assert_not_awaited()
        self.client.assert_not_awaited()

    async def test_profile_returns_public_fields_and_photo_ids_without_credentials_or_phone(self):
        result = await self.act('get_telegram_profile', {'recipient': '@user10'})
        self.assertEqual(result['id'], 10)
        self.assertEqual(result['bio'], 'Публичное био')
        self.assertEqual(result['photos'][0]['photo_id'], 101)
        encoded = json.dumps(result)
        for secret in ['PRIVATE_PHONE', 'access_hash', 'file_reference']:
            self.assertNotIn(secret, encoded)
        self.assertFalse(self.store.rows('SELECT * FROM owner_actions'))

    async def test_profile_avatar_is_actually_inspected_without_persisting_image(self):
        output = io.BytesIO()
        Image.new('RGB', (64, 64), 'red').save(output, 'PNG')
        self.client.download_profile_photo.return_value = output.getvalue()
        self.store.update_settings({'vision_enabled': True})
        result = await self.act('get_telegram_profile', {'recipient': '@user10', 'include_photo': True})
        self.assertEqual(result['photo_description'], 'На фото красный квадрат.')
        self.assertTrue(self.rt.model.inspect_image.call_args.args[0].startswith('data:image/jpeg;base64,'))
        self.assertNotIn('data:image', json.dumps(result))
        self.assertFalse((self.store.directory / 'incoming').exists())

    async def test_normal_profile_tool_binds_to_current_group_author_and_rejects_arbitrary_recipient(self):
        self.store.message(GROUP, 'user', 'Привет', 2, sender_id=20)
        token = current_dialog.set(self.store.dialog_scope(GROUP, 20))
        try:
            result = await self.rt.action(GROUP, 2, 'view_profile', {})
            self.assertEqual(result['id'], 20)
            with self.assertRaises(ValueError):
                await self.rt.action(GROUP, 2, 'view_profile', {'recipient': '@user10'})
        finally:
            current_dialog.reset(token)

    async def test_service_chat_is_inaccessible_for_profile_history_and_mutations(self):
        for name, args in [('get_telegram_profile', {'recipient': '777000'}),
                           ('telegram_history', {'recipient': '777000'}),
                           ('manage_dialog', {'recipient': '777000', 'action': 'archive', 'enabled': True})]:
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'Служебный'):
                await self.act(name, args)
        self.client.get_entity.assert_not_awaited()

    async def test_username_changes_once_and_reports_occupied_name_before_reservation(self):
        result = await self.act('manage_profile', {'action': 'username', 'username': '@new_name'})
        self.assertTrue(result['changed'])
        await self.act('manage_profile', {'action': 'username', 'username': 'new_name'})
        self.assertEqual(len(self.mutations(functions.account.UpdateUsernameRequest)), 1)
        self.request()
        old = len(self.store.rows('SELECT * FROM owner_actions'))
        self.client.side_effect = lambda req: False if isinstance(req, functions.account.CheckUsernameRequest) else self.rpc(req)
        with self.assertRaisesRegex(ValueError, 'недоступен'):
            await self.act('manage_profile', {'action': 'username', 'username': 'taken_name'})
        self.assertEqual(len(self.store.rows('SELECT * FROM owner_actions')), old)

    async def test_profile_changes_reject_invalid_dates_and_mixed_operation_fields(self):
        for args in [{'action': 'birthday', 'day': 31, 'month': 2},
                     {'action': 'birthday', 'clear': True, 'day': 1},
                     {'action': 'username', 'username': 'good_name', 'day': 1},
                     {'action': 'emoji_status', 'document_id': True},
                     {'action': 'username', 'username': 'https://bad.example'}]:
            with self.subTest(args=args), self.assertRaises(ValueError):
                await self.act('manage_profile', args)
        self.client.assert_not_awaited()

    async def test_birthday_emoji_and_avatar_restore_use_selected_self_profile_values(self):
        for args, cls in [({'action': 'birthday', 'day': 29, 'month': 2}, functions.account.UpdateBirthdayRequest),
                          ({'action': 'emoji_status', 'clear': True}, functions.account.UpdateEmojiStatusRequest),
                          ({'action': 'restore_photo', 'photo_id': 101}, functions.photos.UpdateProfilePhotoRequest)]:
            self.request()
            result = await self.act('manage_profile', args)
            self.assertTrue(result['changed'])
            self.assertEqual(len(self.mutations(cls)), 1)
        request = self.mutations(functions.photos.UpdateProfilePhotoRequest)[0]
        self.assertEqual(request.id.id, 101)
        with self.assertRaisesRegex(ValueError, 'своих аватаров'):
            await self.act('manage_profile', {'action': 'restore_photo', 'photo_id': 999})

    async def test_delete_current_avatar_does_not_delete_next_avatar_on_retry(self):
        await self.act('manage_profile', {'action': 'delete_photo'})
        self.client.get_profile_photos.return_value = [types.Photo(102, 203, b'ref', NOW, [], 2)]
        result = await self.act('manage_profile', {'action': 'delete_photo'})
        self.assertTrue(result['already_completed'])
        self.assertEqual(len(self.mutations(functions.photos.DeletePhotosRequest)), 1)

    async def test_privacy_change_preserves_user_exceptions_and_changes_only_selected_key(self):
        result = await self.act('manage_privacy', {'field': 'bio', 'action': 'set', 'scope': 'nobody'})
        req = self.mutations(functions.account.SetPrivacyRequest)[0]
        self.assertIsInstance(req.key, types.InputPrivacyKeyAbout)
        allowed = next(rule for rule in req.rules if isinstance(rule, types.InputPrivacyValueAllowUsers))
        self.assertEqual(allowed.users[0].user_id, 20)
        self.assertIsInstance(req.rules[-1], types.InputPrivacyValueDisallowAll)
        self.assertIn('rules', result)

    async def test_explicit_empty_privacy_exception_list_clears_only_that_list(self):
        await self.act('manage_privacy', {'field': 'profile_photo', 'action': 'set', 'scope': 'everyone', 'allow_users': []})
        req = self.mutations(functions.account.SetPrivacyRequest)[0]
        self.assertFalse(any(isinstance(rule, types.InputPrivacyValueAllowUsers) for rule in req.rules))
        self.assertIsInstance(req.rules[-1], types.InputPrivacyValueAllowAll)

    async def test_dialog_changes_use_target_peer_and_mute_preserves_other_notification_fields(self):
        for args, cls in [({'action': 'archive', 'enabled': True}, functions.folders.EditPeerFoldersRequest),
                          ({'action': 'pin', 'enabled': False}, functions.messages.ToggleDialogPinRequest),
                          ({'action': 'mute', 'enabled': True, 'seconds': 3600}, functions.account.UpdateNotifySettingsRequest),
                          ({'action': 'mark_unread'}, functions.messages.MarkDialogUnreadRequest),
                          ({'action': 'auto_delete', 'seconds': 0}, functions.messages.SetHistoryTTLRequest)]:
            self.request()
            await self.act('manage_dialog', args | {'recipient': '@user10'})
            self.assertEqual(len(self.mutations(cls)), 1)
        req = self.mutations(functions.account.UpdateNotifySettingsRequest)[0]
        self.assertEqual(req.peer.peer.user_id, 10)
        self.assertIsNone(req.settings.show_previews)
        self.assertIsNone(req.settings.silent)
        self.assertGreater(req.settings.mute_until.timestamp(), NOW.timestamp())

    async def test_message_ids_from_other_chat_and_incoming_edit_are_rejected(self):
        self.client.get_messages.return_value = None
        self.client.get_messages.side_effect = lambda *a, **k: [self.message(501, 20)]
        with self.assertRaisesRegex(ValueError, 'другому чату'):
            await self.act('manage_messages', {'recipient': '@user10', 'action': 'delete', 'message_ids': [501]})
        self.client.get_messages.side_effect = lambda *a, **k: [self.message(501, 10, False)]
        with self.assertRaisesRegex(ValueError, 'свои сообщения'):
            await self.act('manage_messages', {'recipient': '@user10', 'action': 'edit', 'message_ids': [501], 'text': 'New'})
        self.client.delete_messages.assert_not_awaited()
        self.client.edit_message.assert_not_awaited()

    async def test_delete_defaults_to_self_and_retry_of_last_selection_does_not_delete_next_message(self):
        args = {'recipient': '@user10', 'action': 'delete', 'selection': 'last_outgoing'}
        await self.act('manage_messages', args)
        self.client.get_messages.side_effect = lambda *a, **k: [self.message(502, 10)]
        result = await self.act('manage_messages', args)
        self.assertTrue(result['already_completed'])
        self.client.delete_messages.assert_awaited_once()
        self.assertFalse(self.client.delete_messages.call_args.kwargs['revoke'])

    async def test_edit_updates_stored_own_message_only_after_confirmed_telegram_result(self):
        self.store.message(10, 'assistant', 'Original', 501)
        await self.act('manage_messages', {'recipient': '@user10', 'action': 'edit', 'message_ids': [501], 'text': 'New'})
        self.assertEqual(self.store.rows('SELECT text FROM messages WHERE contact_id=10')[0]['text'], 'New')

    async def test_forward_records_real_message_and_respects_blocked_destination(self):
        args = {'recipient': '@user10', 'action': 'forward', 'message_ids': [501], 'destination': '@user20'}
        await self.act('manage_messages', args)
        await self.act('manage_messages', args)
        self.client.forward_messages.assert_awaited_once()
        self.assertEqual(self.store.rows('SELECT telegram_id FROM messages WHERE contact_id=20')[0]['telegram_id'], 800)
        self.request()
        self.store.execute('INSERT INTO contact_blocks VALUES (?,?,?,?,?,?,?)', (20, 'blocked', 'owner', OWNER, 0, 0, 0))
        with self.assertRaisesRegex(ValueError, 'заблокирован'):
            await self.act('manage_messages', args)
        self.client.forward_messages.assert_awaited_once()

    async def test_history_filters_forwarded_telegram_login_messages(self):
        secret = self.message(500, 10, False, 'LOGIN_SECRET')
        secret.fwd_from = types.MessageFwdHeader(NOW, from_id=types.PeerUser(777000))
        self.client.get_messages.side_effect = lambda *a, **k: [self.message(501), secret]
        result = await self.act('telegram_history', {'recipient': '@user10'})
        self.assertEqual(len(result['messages']), 1)
        self.assertNotIn('LOGIN_SECRET', json.dumps(result))

    async def test_dialog_list_omits_security_chat_and_keeps_actual_archive_unread_state(self):
        self.client.get_dialogs.return_value = [
            NS(id=10, name='Friend', entity=self.users[10], is_user=True, is_group=False, unread_count=3, pinned=True, archived=True),
            NS(id=777000, name='Telegram', entity=self.users[20], is_user=True, is_group=False, unread_count=1)]
        result = await self.act('telegram_dialogs', {})
        self.assertEqual(len(result['dialogs']), 1)
        self.assertTrue(result['dialogs'][0]['archived'])
        self.assertEqual(result['dialogs'][0]['unread_count'], 3)

    async def test_owner_block_cannot_target_owner_or_self_and_synchronizes_real_contact_block(self):
        for recipient in [str(OWNER), 'me']:
            with self.assertRaisesRegex(ValueError, 'блокировать нельзя'):
                await self.act('manage_contact', {'action': 'block', 'recipient': recipient})
        await self.act('manage_contact', {'action': 'block', 'recipient': '@user10'})
        self.assertTrue(self.store.user_blocked(10))
        self.assertEqual(len(self.mutations(functions.contacts.BlockRequest)), 1)

    async def test_contact_address_book_changes_do_not_erase_local_preferences(self):
        self.store.execute('UPDATE contacts SET gift_allowed=1,enabled=0 WHERE id=10')
        await self.act('manage_contact', {'action': 'add', 'recipient': '@user10', 'first_name': 'Друг'})
        self.request()
        await self.act('manage_contact', {'action': 'delete', 'recipient': '@user10'})
        self.assertEqual(self.store.contact(10)['gift_allowed'], 1)
        self.assertEqual(self.store.contact(10)['enabled'], 0)
        req = self.mutations(functions.contacts.AddContactRequest)[0]
        self.assertFalse(req.add_phone_privacy_exception)
        self.assertEqual(req.phone, '')

    async def test_clear_owner_provenance_during_peer_lookup_prevents_mutation(self):
        async def resolve_then_revoke(value):
            self.store.update_settings({'owner_id': 20})
            return self.users[10]
        self.client.get_entity.side_effect = resolve_then_revoke
        with self.assertRaisesRegex(ValueError, 'владельцу'):
            await self.act('manage_dialog', {'recipient': '@user10', 'action': 'archive', 'enabled': True})
        self.assertFalse(self.mutations(functions.folders.EditPeerFoldersRequest))

    def test_new_tools_are_visible_only_in_owner_catalog_except_scoped_profile_reader(self):
        self.assertTrue(ACCOUNT_NAMES <= {item['function']['name'] for item in OWNER_TOOLS})
        self.assertFalse(ACCOUNT_NAMES & {item['function']['name'] for item in TOOLS})
        self.assertIn('view_profile', {item['function']['name'] for item in TOOLS})

    def test_common_account_commands_parse_without_provider(self):
        for text, name in [('покажи профиль @user10', 'get_telegram_profile'), ('покажи свой аватар', 'get_telegram_profile'),
                           ('покажи чаты', 'telegram_dialogs'), ('смени username на @new_name', 'manage_profile'),
                           ('архивируй @user10', 'manage_dialog'), ('выключи уведомления у @user10', 'manage_dialog'),
                           ('удали мое последнее сообщение у @user10', 'manage_messages')]:
            with self.subTest(text=text):
                self.assertEqual(self.rt.control.parse([text])[0]['name'], name)
