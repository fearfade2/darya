import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

from app.runtime import Runtime
from app.store import Store
from app.voice import CallUnavailable

OWNER = 1000000001
GROUP = -123


class OwnerVoiceCommandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name))
        self.store.update_settings({'owner_id': OWNER, 'calls_enabled': True, 'call_voice_volume': 1})
        for i in (OWNER, 10):
            self.store.upsert_contact(i, str(i))
        self.store.upsert_contact(GROUP, 'Current group', kind='group')
        self.store.message(OWNER, 'user', 'Скажи в звонке Привет!', 1, sender_id=OWNER)
        self.rt = Runtime(self.store)
        self.calls = self.rt.calls
        self.calls.active = {'chat_id': GROUP, 'name': 'Current group', 'kind': 'group', 'mode': 'listen',
                             'started': time.monotonic(), 'speaking': False, 'admin_muted': False}
        self.sdk = NS(mute=AsyncMock(), unmute=AsyncMock(), send_frame=AsyncMock(), leave_call=AsyncMock())
        self.calls.sdk = self.sdk
        self.calls.voice.synthesize = AsyncMock(return_value=bytes(1920))

    async def asyncTearDown(self):
        self.calls.active = None
        self.calls.sdk = None
        await self.rt.stop()
        self.store.close()
        self.tmp.cleanup()

    async def test_owner_speech_temporarily_enables_microphone_and_restores_listen_mode(self):
        result = await self.rt.control.action(OWNER, 1, 'speak_in_call', {'text': 'Привет!', 'volume': 2}, trusted=True)
        self.assertTrue(result['spoken'])
        self.calls.voice.synthesize.assert_awaited_once_with('Привет!', self.store.settings()['call_voice_speed'], 2)
        self.sdk.send_frame.assert_awaited_once()
        self.assertEqual(self.calls.active['mode'], 'listen')
        self.assertEqual(self.store.settings()['call_voice_volume'], 1)
        await self.rt.control.action(OWNER, 1, 'speak_in_call', {'text': 'Привет!'}, trusted=True)
        self.sdk.send_frame.assert_awaited_once()

    async def test_speech_requires_permission_and_matching_current_group(self):
        with self.assertRaises(ValueError):
            await self.rt.action(10, 1, 'speak_in_call', {'text': 'Привет!'}, owner_command=True)
        with self.assertRaises(CallUnavailable):
            await self.calls.say('Привет!', Mock(), chat_id=-999)
        self.assertFalse(self.calls.interrupt.is_set())
        self.sdk.send_frame.assert_not_awaited()

    async def test_admin_mute_and_disappearing_call_never_claim_successful_speech(self):
        self.calls.active['admin_muted'] = True
        with self.assertRaisesRegex(CallUnavailable, 'Администратор'):
            await self.calls.say('Привет!', Mock())
        self.calls.active['admin_muted'] = False
        async def removed(*args):
            self.calls.active = None
            return bytes(1920)
        self.calls.voice.synthesize.side_effect = removed
        with self.assertRaisesRegex(CallUnavailable, 'прервана'):
            await self.calls.say('Привет!', Mock())
        self.sdk.send_frame.assert_not_awaited()

    async def test_revoked_owner_permission_during_synthesis_prevents_audio_dispatch(self):
        async def revoke(*args):
            self.store.update_settings({'owner_id': 20})
            return bytes(1920)
        self.calls.voice.synthesize.side_effect = revoke
        with self.assertRaisesRegex(ValueError, 'владельцу'):
            await self.rt.control.action(OWNER, 1, 'speak_in_call', {'text': 'Привет!'}, trusted=True)
        self.sdk.send_frame.assert_not_awaited()

    async def test_two_audio_streams_do_not_interleave(self):
        self.calls.active['mode'] = 'talk'
        await asyncio.gather(self.calls.speak(b'a' * 3840), self.calls.speak(b'b' * 3840))
        chunks = [call.args[2] for call in self.sdk.send_frame.await_args_list]
        self.assertEqual(chunks, [b'a' * 1920, b'a' * 1920, b'b' * 1920, b'b' * 1920])

    def test_model_context_has_live_voicechat_state_without_leaking_other_chat(self):
        self.assertIn('"connected_here": true', self.rt.model.context(GROUP)[0]['content'])
        self.assertIn('"mode": "listen"', self.rt.model.context(OWNER)[0]['content'])
        other = self.rt.model.context(10)[0]['content']
        self.assertIn('"connected_here": false', other)
        self.assertNotIn('Current group', other)
