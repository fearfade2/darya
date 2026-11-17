"""One owner-controlled Telegram audio call, with local ephemeral voice processing."""
from __future__ import annotations

import asyncio
import time
from collections import deque
from importlib.metadata import version

from telethon import TelegramClient, functions, types, utils

from .groups import Groups, target
from .voice import CallUnavailable, Utterance, VoiceEngine


CONFERENCE_CHAIN_TIMEOUT = 10
CONFERENCE_CONNECT_TIMEOUT = 40


async def conference_last_block(adapter, invitation, call_id):
    """Read the main chain through the invitation without hiding Telegram errors."""
    try:
        async with asyncio.timeout(CONFERENCE_CHAIN_TIMEOUT):
            while True:
                result = await adapter._invoke(functions.phone.GetGroupCallChainBlocksRequest(
                    call=invitation, sub_chain_id=0, offset=-1, limit=1),
                    chat_id=call_id, sleep_threshold=0)
                for update in result.updates:
                    if not isinstance(update, types.UpdateGroupCallChainBlocks) or update.sub_chain_id != 0:
                        continue
                    same_call = (isinstance(update.call, types.InputGroupCall) and update.call.id == call_id
                                 or isinstance(update.call, types.InputGroupCallSlug)
                                 and update.call.slug == invitation.slug)
                    if same_call and update.blocks and update.blocks[-1]:
                        return update.blocks[-1]
                await asyncio.sleep(0.5)
    except TimeoutError:
        raise CallUnavailable('Данные подключения к конференции ещё не готовы. '
                              'Попробуй подключиться новой командой чуть позже') from None


async def join_conference(sdk, client, slug, stream, guard):
    """3.0.0 adapter: a link is a conference chain, never a user to dial.

    Public play(conference=True) creates a NEW call. Join the existing chain with
    its last block through NTgCalls. Keep the invitation until Telegram confirms
    membership; an id/access_hash alone does not authorize joining via a link.
    """
    from pytgcalls.types import CallConfig
    from pytgcalls.methods.utilities.stream_params import StreamParams
    invitation = types.InputGroupCallSlug(slug)
    result = await client(functions.phone.GetGroupCallRequest(invitation, limit=1))
    call = result.call
    if not isinstance(call, types.GroupCall) or not call.conference:
        raise CallUnavailable('Ссылка не ведёт в действующий конференц-звонок')
    chat_id = call.id
    adapter = sdk._app._bind_client
    native_started = False
    try:
        block = await conference_last_block(adapter, invitation, chat_id)
        params = await StreamParams.get_stream_params(stream)
        guard()
        adapter._cache.set_cache(chat_id, invitation)
        async with asyncio.timeout(CONFERENCE_CONNECT_TIMEOUT):
            native_started = True
            await sdk._binding.create_p2p_call(chat_id)
            await sdk._connect_call(chat_id, params, CallConfig(
                timeout=CONFERENCE_CONNECT_TIMEOUT, conference=True), None, block)
    except BaseException:
        if native_started:
            try:
                await sdk._binding.stop(chat_id)
            except Exception:
                pass
        adapter._cache.drop_cache(chat_id)
        raise
    return chat_id


class Calls:
    def __init__(self, runtime):
        self.runtime = runtime
        self.telegram = runtime.telegram
        self.store = runtime.store
        self.groups = Groups(self.telegram)
        self.voice = VoiceEngine(self.store.directory)
        self.sdk = None
        self.client = None
        self.sdk_handlers = []
        self.lock = asyncio.Lock()
        self.speech_lock = asyncio.Lock()
        self.start_lock = asyncio.Lock()
        self.active = None
        self.pending = {}
        self.history = {}
        self.speakers = {}
        self.queue = asyncio.Queue(maxsize=3)
        self.worker = None
        self.monitor = None
        self.interrupt = asyncio.Event()
        self.error = ''
        self.spoken = 0
        self.heard = 0
        self.next_connection_check = 0
        self.last_activity = 0
        self.last_voice_end = 0
        self.next_initiative = 0
        self.initiative_attempts = 0
        self.initiating = False
        self.own_replies = deque(maxlen=4)

    async def start(self):
        async with self.start_lock:
            return await self._start()

    async def _start(self):
        if not self.store.settings()['calls_enabled']:
            raise CallUnavailable('Звонки выключены в настройках')
        if not self.telegram.ready():
            raise CallUnavailable('Сначала подключи Telegram')
        if self.sdk and self.client is self.telegram.client:
            return self.sdk
        if self.sdk:
            await self.close()
        if version('py-tgcalls') != '3.0.0' or version('ntgcalls') != '3.0.6':
            raise CallUnavailable('Версия библиотеки звонков отличается от проверенной')
        from pytgcalls import PyTgCalls
        client = self.telegram.client
        if not isinstance(client, TelegramClient):
            raise CallUnavailable('Звонки требуют подключённый клиент Telethon')
        before = {callback for callback, _ in client.list_event_handlers()}
        sdk = PyTgCalls(client, workers=2)
        self.sdk_handlers = [callback for callback, _ in client.list_event_handlers() if callback not in before]
        self.sdk, self.client = sdk, client
        sdk.on_update()(self.on_update)
        try:
            await asyncio.wait_for(sdk.start(), 20)
        except BaseException:
            await self.close()
            raise
        return sdk

    def media(self):
        from pytgcalls.types import ExternalMedia, MediaStream
        from pytgcalls.types.raw import AudioParameters
        return MediaStream(ExternalMedia.AUDIO, audio_parameters=AudioParameters(48000, 1),
                           video_flags=MediaStream.Flags.IGNORE)

    async def join(self, value, mode, guard):
        parsed = target(value, calls=True)
        sdk = await self.start()
        async with self.lock:
            if self.active:
                raise CallUnavailable('Я уже в звонке. Сначала попроси выйти из него')
            await self.voice.prepare()
            self.validate_mode(mode)
            guard()
            chat_id = None
            try:
                if parsed['kind'] == 'conference':
                    chat_id = await join_conference(sdk, self.client, parsed['value'], self.media(), guard)
                    name = 'конференция Telegram'
                    kind = 'conference'
                else:
                    from pytgcalls.types import GroupCallConfig
                    entity = await self.groups.resolve(parsed)
                    chat_id = utils.get_peer_id(entity)
                    name = entity.title
                    kind = 'group'
                    guard()
                    from pytgcalls.exceptions import NoActiveGroupCall
                    try:
                        await sdk.play(chat_id, self.media(), config=GroupCallConfig(
                            auto_start=False, invite_hash=parsed.get('invite_hash')))
                    except NoActiveGroupCall:
                        raise CallUnavailable('В этой группе сейчас нет действующего голосового чата') from None
                await self.activate(chat_id, name, kind, mode)
            except BaseException:
                self.active = None
                self.history.clear()
                self.speakers.clear()
                if chat_id is not None:
                    await self.cleanup_native(chat_id)
                raise
            return self.status()

    async def dial(self, recipient, mode, guard):
        entity, _, chat_id = await self.runtime.account.resolve(recipient, guard)
        if not isinstance(entity, types.User) or entity.bot or entity.deleted or chat_id in {self.telegram.account_id,777000}:
            raise CallUnavailable('Позвонить можно доступному пользователю, кроме своего и служебного аккаунта')
        contact=self.store.upsert_contact(chat_id,utils.get_display_name(entity),entity.username or '')
        if not contact['enabled'] or not contact['calls_allowed'] or self.store.dialog_blocked(chat_id):
            raise CallUnavailable('Звонки этому собеседнику выключены')
        sdk=await self.start()
        async with self.lock:
            if self.active:
                raise CallUnavailable('Я уже в звонке. Сначала нужно завершить его')
            if chat_id in self.pending:
                raise CallUnavailable('От этого человека уже есть входящий звонок; его можно принять')
            await self.voice.prepare()
            self.validate_mode(mode)
            guard()
            if not self.store.settings()['calls_enabled'] or not self.store.contact(chat_id)['calls_allowed']:
                raise CallUnavailable('Звонки выключены во время подготовки')
            try:
                from pytgcalls.types import CallConfig
                await asyncio.wait_for(sdk.play(chat_id,self.media(),config=CallConfig(timeout=35)),60)
                await self.activate(chat_id,contact['name'],'private',mode)
                self.active['outgoing']=True
            except BaseException:
                self.active=None
                self.history.clear(); self.speakers.clear()
                await self.cleanup_native(chat_id)
                raise
            return self.status()

    async def accept(self, recipient, mode, guard):
        sdk = await self.start()
        async with self.lock:
            if self.active:
                raise CallUnavailable('Я уже в звонке. Сначала попроси выйти')
            if recipient:
                contact = await self.telegram.resolve(recipient)
                chat_id, name = contact['id'], contact['name']
            else:
                chat_id, name = self.store.settings()['owner_id'], 'владелец'
            if self.store.user_blocked(chat_id):
                raise CallUnavailable('Этот пользователь заблокирован')
            contacts=self.store.rows('SELECT calls_allowed,enabled FROM contacts WHERE id=?',(chat_id,))
            if contacts and (not contacts[0]['calls_allowed'] or not contacts[0]['enabled']):
                raise CallUnavailable('Звонки этому собеседнику выключены')
            if chat_id not in self.pending or self.pending[chat_id] + 90 < time.monotonic():
                raise CallUnavailable('От этого пользователя сейчас нет входящего звонка')
            await self.voice.prepare()
            self.validate_mode(mode)
            guard()
            incoming = sdk._p2p_configs.get(chat_id)
            if incoming is None or incoming.outgoing or chat_id not in self.pending:
                raise CallUnavailable('Входящий звонок уже завершился; исходящий не начинаю')
            try:
                from pytgcalls.types import CallConfig
                await sdk.play(chat_id, self.media(), config=CallConfig(timeout=35))
                await self.activate(chat_id, name, 'private', mode)
            except BaseException:
                self.active = None
                self.history.clear()
                self.speakers.clear()
                await self.cleanup_native(chat_id)
                raise
            self.pending.pop(chat_id, None)
            return self.status()

    @staticmethod
    def validate_mode(mode):
        if mode not in {'listen', 'talk'}:
            raise CallUnavailable('Режим звонка: listen или talk')

    async def activate(self, chat_id, name, kind, mode):
        from pytgcalls.types import RecordStream
        from pytgcalls.types.raw import AudioParameters
        self.active = dict(chat_id=chat_id, name=name, kind=kind, mode=mode,
                           started=time.monotonic(), admin_muted=False, speaking=False)
        self.error = ''
        self.spoken = self.heard = 0
        self.last_activity = time.monotonic()
        self.last_voice_end = self.last_activity
        self.next_initiative = self.last_activity + 3
        self.initiative_attempts = 0
        self.initiating = False
        self.own_replies.clear()
        self.history.clear()
        self.speakers.clear()
        self.interrupt.clear()
        await self.sdk.mute(chat_id)
        await self.sdk.record(chat_id, RecordStream(audio=True, audio_parameters=AudioParameters(16000, 1)))
        if kind != 'private':
            participants = (await self.sdk._app._bind_client._cache.get_participant_list(chat_id)
                            if kind == 'conference' else await self.sdk.get_participants(chat_id))
            own = next((participant for participant in (participants or []) if participant.user_id == self.telegram.account_id), None)
            self.active['admin_muted'] = bool(own and own.muted_by_admin)
        self.worker = asyncio.create_task(self.process())
        self.monitor = asyncio.create_task(self.watch())

    async def mode(self, mode):
        self.validate_mode(mode)
        if not self.active:
            raise CallUnavailable('Сейчас я не в звонке')
        changed = self.active['mode'] != mode
        self.active['mode'] = mode
        if changed and mode == 'talk':
            self.last_activity = time.monotonic()
            self.last_voice_end = self.last_activity
            self.next_initiative = self.last_activity + 3
        self.interrupt.set()
        await self.sdk.mute(self.active['chat_id'])
        return self.status()

    def status(self):
        active = self.active
        return {'connected': bool(active), 'name': active['name'] if active else '',
                'kind': active['kind'] if active else '', 'mode': active['mode'] if active else '',
                'speaking': bool(active and active['speaking']),
                'admin_muted': bool(active and active['admin_muted']),
                'duration_seconds': round(time.monotonic() - active['started']) if active else 0,
                'heard': self.heard, 'spoken': self.spoken, 'error': self.error,
                'pending_callers': [key for key, sent in self.pending.items() if sent + 90 > time.monotonic()]}

    def context_for(self, contact_id):
        active = self.active
        from .authority import authorized
        if not active or not authorized(self.store, contact_id) and active['chat_id'] != contact_id:
            return {'connected_here': False}
        return {'connected_here': True, **{key: self.status()[key] for key in
                ['name', 'kind', 'mode', 'speaking', 'admin_muted']}}

    async def say(self, text, guard, volume=None, chat_id=None):
        if not isinstance(text, str) or not 1 <= len(text.strip()) <= 1000:
            raise CallUnavailable('Укажи фразу до 1000 символов')
        if volume is not None and (type(volume) not in {int, float} or not 0.1 <= volume <= 2):
            raise CallUnavailable('Громкость: от 0,1 до 2')
        guard()
        active = self.active
        if not active or chat_id is not None and active['chat_id'] != chat_id:
            raise CallUnavailable('Сейчас я не в голосовом чате этой группы' if chat_id is not None else 'Сейчас я не в звонке')
        self.interrupt.set()
        async with self.speech_lock:
            active = self.active
            if not active or chat_id is not None and active['chat_id'] != chat_id:
                raise CallUnavailable('Сейчас я не в голосовом чате этой группы' if chat_id is not None else 'Сейчас я не в звонке')
            settings = self.store.settings()
            if not settings['calls_enabled'] or active['admin_muted']:
                raise CallUnavailable('Голос отключён в настройках' if not settings['calls_enabled'] else 'Администратор выключил мне микрофон')
            previous = active['mode']
            try:
                guard()
                if previous != 'talk':
                    await self.mode('talk')
                self.interrupt.clear()
                self.voice.speaker = settings['voice_speaker']
                audio = await self.voice.synthesize(text.strip(), settings['call_voice_speed'],
                                                    volume if volume is not None else settings['call_voice_volume'])
                guard()
                if self.active is not active or not await self._speak(audio):
                    raise CallUnavailable('Голосовая реплика прервана; полностью произнести её не удалось')
                self.spoken += 1
                self.own_replies.append({'role': 'assistant', 'content': text.strip()})
                return {'spoken': True, 'text': 'Произнесла фразу в голосовом чате.'}
            finally:
                if self.active is active and previous != 'talk':
                    await self.mode(previous)

    async def on_update(self, sdk, update):
        from pytgcalls.types import ChatUpdate, Device, Direction, StreamFrames, UpdatedGroupCallParticipant
        if isinstance(update, ChatUpdate):
            if update.status & ChatUpdate.Status.INCOMING_CALL:
                self.pending[update.chat_id] = time.monotonic()
                while len(self.pending) > 20:
                    self.pending.pop(next(iter(self.pending)))
                autonomy=getattr(self.runtime,'telegram_autonomy',None)
                if autonomy:
                    autonomy.incoming_call(update.chat_id)
            if update.status & ChatUpdate.Status.DISCARDED_CALL:
                self.pending.pop(update.chat_id, None)
            if self.active and update.chat_id == self.active['chat_id'] and update.status & ChatUpdate.Status.LEFT_CALL:
                self.interrupt.set()
                asyncio.create_task(self.leave(remote=False))
            return
        active = self.active
        if not active or update.chat_id != active['chat_id']:
            return
        if isinstance(update, UpdatedGroupCallParticipant) and update.participant.user_id == self.telegram.account_id:
            active['admin_muted'] = update.participant.muted_by_admin
            if active['admin_muted']:
                self.interrupt.set()
        if not isinstance(update, StreamFrames) or update.direction != Direction.INCOMING or update.device not in {Device.MICROPHONE, Device.SPEAKER}:
            return
        settings = self.store.settings()
        if not settings['calls_enabled']:
            self.interrupt.set()
            return
        for frame in update.frames:
            if frame.ssrc not in self.speakers:
                if len(self.speakers) >= 8:
                    continue
                self.speakers[frame.ssrc] = Utterance()
            utterance = self.speakers[frame.ssrc]
            pcm = utterance.feed(frame.frame, time.monotonic(), settings)
            if pcm or utterance.chunks and utterance.silence == 0:
                self.last_activity = time.monotonic()
                if self.initiating and settings['call_interrupt_on_speech']:
                    self.interrupt.set()
            if active['speaking'] and settings['call_interrupt_on_speech'] and (pcm or utterance.speech >= settings['call_min_speech_seconds']):
                self.interrupt.set()
            if pcm:
                self.enqueue(frame.ssrc, pcm)

    def enqueue(self, speaker, pcm):
        self.last_activity = time.monotonic()
        if self.queue.full():
            self.queue.get_nowait()
        self.queue.put_nowait((speaker, pcm))

    async def watch(self):
        while self.active:
            settings = self.store.settings()
            now = time.monotonic()
            if not self.telegram.ready() or not settings['calls_enabled'] or now - self.active['started'] >= settings['call_max_minutes'] * 60:
                asyncio.create_task(self.leave())
                return
            if now >= self.next_connection_check:
                self.next_connection_check = now + 2
                try:
                    native = await self.sdk._binding.calls()
                    if self.active and self.active['chat_id'] not in native:
                        asyncio.create_task(self.leave(remote=False))
                        return
                except Exception:
                    # A failed diagnostic is not proof that Telegram disconnected.
                    pass
            for speaker, utterance in list(self.speakers.items()):
                if utterance.chunks and now - utterance.last_frame >= settings['call_silence_seconds']:
                    pcm = utterance.finish(settings)
                    if pcm:
                        self.enqueue(speaker, pcm)
                if not utterance.chunks and now - utterance.last_frame > 30:
                    self.speakers.pop(speaker, None)
            await asyncio.sleep(0.1)

    async def process(self):
        while self.active:
            try:
                if not self.store.settings()['call_wait_for_silence'] and self.initiative_ready():
                    await self.initiate()
                    continue
                try:
                    speaker, pcm = await asyncio.wait_for(self.queue.get(), timeout=0.5)
                except TimeoutError:
                    await self.initiate()
                    continue
                active = self.active
                text = await self.voice.transcribe(pcm, self.store.settings()['call_language'])
                if not text or self.active is not active:
                    continue
                self.heard += 1
                self.last_activity = time.monotonic()
                self.initiative_attempts = 0
                if speaker not in self.history and len(self.history) >= 32:
                    self.history.pop(next(iter(self.history)))
                history = self.history.setdefault(speaker,deque(maxlen=12))
                history.append({'role': 'user', 'content': text})
                settings = self.store.settings()
                if not settings['calls_enabled'] or active['mode'] != 'talk' or active['admin_muted']:
                    continue
                if not settings['call_reply_to_all'] and not any(alias.strip().casefold() in text.casefold()
                         for alias in settings['group_aliases'].split(',') if alias.strip()):
                    continue
                self.interrupt.clear()
                result = await self.runtime.model.call_reply(list(history))
                if self.active is not active or active['mode'] != 'talk' or active['admin_muted']:
                    continue
                self.voice.speaker = settings['voice_speaker']
                audio = await self.voice.synthesize(result, settings['call_voice_speed'], settings['call_voice_volume'])
                if await self.speak(audio):
                    self.spoken += 1
                    history.append({'role': 'assistant', 'content': result})
                    self.own_replies.append({'role': 'assistant', 'content': result})
                    self.error = ''
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.error = 'Ошибка голосового ответа: ' + type(error).__name__
                self.store.event(self.error, 'error')
                await asyncio.sleep(1)

    def initiative_ready(self):
        active, settings = self.active, self.store.settings()
        quiet_seconds = 3 if self.spoken == 0 else settings['call_initiative_seconds']
        limit = settings['call_initiative_max_unanswered']
        wait_for_silence = settings['call_wait_for_silence']
        last_activity = self.last_activity if wait_for_silence else self.last_voice_end
        return bool(active and self.telegram.ready() and settings['calls_enabled']
                    and settings['call_initiative_enabled'] and active['mode'] == 'talk'
                    and not active['admin_muted'] and not active['speaking']
                    and (limit == 0 or self.initiative_attempts < limit)
                    and (not wait_for_silence or self.queue.empty()
                         and not any(u.chunks for u in self.speakers.values()))
                    and time.monotonic() >= max(self.next_initiative, last_activity + quiet_seconds))

    async def initiate(self):
        # This runs in the same worker as replies: only one voice generation at a time.
        if not self.initiative_ready():
            return
        active, settings = self.active, self.store.settings()
        self.initiative_attempts += 1
        self.next_initiative = time.monotonic() + settings['call_initiative_seconds']
        self.interrupt.clear()
        self.initiating = True
        try:
            result = await self.runtime.model.call_reply(
                list(self.own_replies), initiative='greeting' if self.spoken == 0 else 'pause')
            if not self.can_initiate_speech(active):
                return
            self.voice.speaker = settings['voice_speaker']
            audio = await self.voice.synthesize(result, settings['call_voice_speed'], settings['call_voice_volume'])
            if not self.can_initiate_speech(active):
                return
            if await self.speak(audio):
                self.spoken += 1
                self.own_replies.append({'role': 'assistant', 'content': result})
                self.error = ''
        except Exception:
            # Frequent conversation must not turn a provider outage into rapid retries.
            self.next_initiative = time.monotonic() + max(30, settings['call_initiative_seconds'])
            raise
        finally:
            self.initiating = False

    def can_initiate_speech(self, active):
        settings = self.store.settings()
        return bool(self.active is active and active and active['mode'] == 'talk'
                    and not active['admin_muted'] and not self.interrupt.is_set()
                    and settings['calls_enabled'] and settings['call_initiative_enabled']
                    and (not settings['call_wait_for_silence'] or self.queue.empty()
                         and not any(u.chunks for u in self.speakers.values())))

    async def speak(self, audio):
        async with self.speech_lock:
            return await self._speak(audio)

    async def _speak(self, audio):
        from pytgcalls.types import Device
        active = self.active
        if not active or not self.store.settings()['calls_enabled'] or active['mode'] != 'talk' or active['admin_muted'] or self.interrupt.is_set():
            return False
        active['speaking'] = True
        try:
            await self.sdk.unmute(active['chat_id'])
            started = time.monotonic()
            # 20 ms packets, paced to prevent flooding the native microphone.
            for offset in range(0, len(audio), 1920):
                if self.initiating and not self.store.settings()['call_initiative_enabled']:
                    return False
                if self.active is not active or not self.store.settings()['calls_enabled'] or active['mode'] != 'talk' or active['admin_muted'] or self.interrupt.is_set():
                    return False
                await self.sdk.send_frame(active['chat_id'], Device.MICROPHONE, audio[offset:offset + 1920].ljust(1920, b'\0'))
                await asyncio.sleep(max(0, started + (offset / 96000) + 0.02 - time.monotonic()))
            return True
        finally:
            active['speaking'] = False
            self.last_activity = time.monotonic()
            self.last_voice_end = self.last_activity
            try:
                await self.sdk.mute(active['chat_id'])
            except Exception:
                pass

    async def cleanup_native(self, chat_id):
        try:
            await asyncio.wait_for(self.sdk.leave_call(chat_id), 10)
        except Exception:
            try:
                await self.sdk._binding.stop(chat_id)
            except Exception:
                pass

    async def leave(self, remote=True):
        async with self.lock:
            active, self.active = self.active, None
            self.interrupt.set()
            tasks = [task for task in (self.worker, self.monitor) if task and task is not asyncio.current_task()]
            self.worker = self.monitor = None
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            try:
                if active and self.sdk and remote:
                    await asyncio.wait_for(self.sdk.leave_call(active['chat_id']), 10)
            except Exception:
                if active and self.sdk:
                    await self.cleanup_native(active['chat_id'])
                raise
            finally:
                self.history.clear()
                self.own_replies.clear()
                self.speakers.clear()
                while not self.queue.empty():
                    self.queue.get_nowait()
                await self.voice.release()
            return {'left': bool(active)}

    async def release_idle_voice(self):
        async with self.lock:
            if not self.active:
                await self.voice.release()

    async def close(self):
        try:
            await self.leave()
        except Exception as error:
            self.store.event('Отключение звонка: ' + type(error).__name__, 'error')
        finally:
            sdk, self.sdk = self.sdk, None
            client, self.client = self.client, None
            if sdk:
                try:
                    for chat_id in list(await sdk._binding.calls()):
                        await sdk._binding.stop(chat_id)
                except Exception:
                    pass
                # Break native callback references to this loop and SDK instance.
                for name in ['on_stream_end', 'on_upgrade', 'on_connection_change',
                             'on_update_emojis', 'on_signaling_data', 'on_frames',
                             'on_request_broadcast_part', 'on_request_broadcast_timestamp',
                             'on_outbound_block', 'on_request_participants', 'on_subchain_request']:
                    getattr(sdk._binding, name)(lambda *args: None)
                sdk._app._bind_client.remove_handler(sdk._handle_mtproto_updates)
                sdk.executor.shutdown(wait=False, cancel_futures=True)
                sdk._is_running = False
            if client:
                for callback in self.sdk_handlers:
                    client.remove_event_handler(callback)
            self.sdk_handlers.clear()
            self.pending.clear()
