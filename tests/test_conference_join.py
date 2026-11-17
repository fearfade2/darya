import asyncio
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch

from telethon import TelegramClient, errors, functions, types
from telethon.sessions import MemorySession
from pytgcalls.methods.internal.connect_call import ConnectCall
from pytgcalls.mtproto import MtProtoClient
from pytgcalls.types import Cache

from app.calls import Calls, join_conference
from app.voice import CallUnavailable


class ConferenceJoinTests(unittest.IsolatedAsyncioTestCase):
    """Exercise the installed SDK's real connection, MTProto adapter and cache."""

    async def asyncSetUp(self):
        self.client = TelegramClient(MemorySession(), 1, 'test')
        self.call = types.GroupCall(9000, 123, 1, 0, 1, conference=True)
        self.metadata = AsyncMock(return_value=NS(call=self.call))
        self.sdk = ConnectCall()
        self.sdk._app = MtProtoClient(3600, self.client)
        self.adapter = self.sdk._app._bind_client
        self.adapter._invoke = AsyncMock(side_effect=self.rpc)
        self.sdk._my_id = 42
        self.sdk.loop = asyncio.get_running_loop()
        self.sdk._cache_local_peer = types.InputPeerSelf()
        self.sdk._cache_user_peer = Cache()
        self.sdk._binding = NS(
            create_p2p_call=AsyncMock(), stop=AsyncMock(),
            init_conference=AsyncMock(return_value=NS(
                public_key=bytes(8), payload='{}', block=b'join-block')),
            set_stream_sources=AsyncMock(),
            get_state=AsyncMock(return_value=NS(video_stopped=True, presentation_stopped=True)),
            connect=AsyncMock(side_effect=self.connected))
        self.sdk._join_presentation = AsyncMock()
        self.sdk._update_sources = AsyncMock()
        self.stream = Calls.media(None)

    async def asyncTearDown(self):
        await self.client.disconnect()

    def blocks(self, blocks, call=None, subchain=0):
        return NS(updates=[types.UpdateGroupCallChainBlocks(
            call or types.InputGroupCallSlug('abcdef'), subchain, blocks, 0)])

    async def rpc(self, request, **kwargs):
        # Before joining, Telegram requires the invitation, not merely an id/hash.
        if not isinstance(request.call, types.InputGroupCallSlug):
            raise errors.GroupcallForbiddenError(request)
        self.assertEqual(request.call.slug, 'abcdef')
        if isinstance(request, functions.phone.GetGroupCallChainBlocksRequest):
            return self.blocks([b'chain'])
        if isinstance(request, functions.phone.JoinGroupCallRequest):
            self.assertEqual(request.block, b'join-block')
            return NS(updates=[types.UpdateGroupCall(self.call),
                              types.UpdateGroupCallConnection(types.DataJSON('{}'))])
        self.fail(f'Unexpected RPC: {type(request).__name__}')

    async def connected(self, chat_id, *args):
        self.sdk._wait_connect[chat_id].set_result(None)

    async def join(self, guard=None):
        return await join_conference(self.sdk, self.metadata, 'abcdef', self.stream, guard or Mock())

    async def test_invitation_survives_chain_fetch_and_real_sdk_join(self):
        self.assertEqual(await self.join(), 9000)
        requests = [call.args[0] for call in self.adapter._invoke.await_args_list]
        self.assertEqual([type(r) for r in requests], [
            functions.phone.GetGroupCallChainBlocksRequest, functions.phone.JoinGroupCallRequest])
        self.sdk._binding.create_p2p_call.assert_awaited_once_with(9000)
        self.sdk._binding.init_conference.assert_awaited_once_with(9000, 42, b'chain')
        cached = await self.adapter.get_input_call(9000)
        self.assertIsInstance(cached, types.InputGroupCall)
        self.assertEqual(cached.id, 9000)
        self.assertFalse(self.sdk._wait_connect)

    async def test_empty_chain_is_retried_before_joining_once(self):
        count = 0
        async def delayed(request, **kwargs):
            nonlocal count
            if isinstance(request, functions.phone.GetGroupCallChainBlocksRequest):
                count += 1
                if count == 1:
                    return self.blocks([])
            return await self.rpc(request, **kwargs)
        self.adapter._invoke.side_effect = delayed
        await self.join()
        self.assertEqual(count, 2)
        self.sdk._binding.create_p2p_call.assert_awaited_once()

    async def test_flood_and_access_errors_are_not_hidden_as_missing_keys(self):
        for error in [errors.FloodWaitError(None, capture=30),
                      errors.GroupcallForbiddenError(None), errors.GroupcallInvalidError(None)]:
            with self.subTest(error=type(error).__name__):
                self.adapter._invoke.reset_mock()
                self.adapter._invoke.side_effect = error
                with self.assertRaises(type(error)):
                    await self.join()
                self.adapter._invoke.assert_awaited_once()
                self.sdk._binding.create_p2p_call.assert_not_awaited()
                self.assertIsNone(await self.adapter.get_input_call(9000))

    async def test_unrelated_or_empty_blocks_never_start_native_connection(self):
        for response in [self.blocks([]), self.blocks([b'']),
                         self.blocks([b'wrong'], subchain=1),
                         self.blocks([b'wrong'], call=types.InputGroupCall(999, 123)),
                         self.blocks([b'wrong'], call=types.InputGroupCallSlug('other'))]:
            with self.subTest(response=response), patch('app.calls.CONFERENCE_CHAIN_TIMEOUT', .01):
                self.adapter._invoke.side_effect = None
                self.adapter._invoke.return_value = response
                with self.assertRaisesRegex(CallUnavailable, 'не готовы'):
                    await self.join()
                self.sdk._binding.create_p2p_call.assert_not_awaited()

    async def test_chain_response_can_identify_call_by_id(self):
        async def by_id(request, **kwargs):
            if isinstance(request, functions.phone.GetGroupCallChainBlocksRequest):
                return self.blocks([b'chain'], call=types.InputGroupCall(9000, 123))
            return await self.rpc(request, **kwargs)
        self.adapter._invoke.side_effect = by_id
        self.assertEqual(await self.join(), 9000)

    async def test_owner_authorization_is_rechecked_after_fetch(self):
        guard = Mock(side_effect=ValueError('owner changed'))
        with self.assertRaisesRegex(ValueError, 'owner changed'):
            await self.join(guard)
        self.sdk._binding.create_p2p_call.assert_not_awaited()
        self.assertIsNone(await self.adapter.get_input_call(9000))

    async def test_connection_timeout_cleans_native_call_and_cache(self):
        self.sdk._binding.connect.side_effect = lambda *args: None
        with patch('app.calls.CONFERENCE_CONNECT_TIMEOUT', .01), self.assertRaises(TimeoutError):
            await self.join()
        self.sdk._binding.stop.assert_awaited_with(9000)
        self.assertIsNone(await self.adapter.get_input_call(9000))
        self.assertFalse(self.sdk._wait_connect)

    async def test_cancellation_during_native_creation_cleans_up(self):
        started = asyncio.Event()
        async def creating(*args):
            started.set()
            await asyncio.Event().wait()
        self.sdk._binding.create_p2p_call.side_effect = creating
        task = asyncio.create_task(self.join())
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.sdk._binding.stop.assert_awaited_once_with(9000)
        self.assertIsNone(await self.adapter.get_input_call(9000))
