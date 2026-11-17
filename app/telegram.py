from __future__ import annotations

import asyncio
from contextlib import nullcontext
import os
import random
import time
from pathlib import Path

from telethon import TelegramClient, events, errors, utils
from telethon.network.connection import ConnectionTcpFull
from telethon.tl import functions, types

from .behavior import addressed, delay
from .vision import MAX_BYTES, save_visual
from .session_guard import SessionGuard, GuardPolicyError, SECURITY_PEER, security_message

REPLY_PRESENCE_REFRESH_SECONDS = 2


def connection_on(port):
    class Connection(ConnectionTcpFull):
        def __init__(self, ip, ignored_port, *args, **kwargs):
            super().__init__(ip, port, *args, **kwargs)
    return Connection


class Telegram:
    def __init__(self, store, receive):
        self.store, self.receive = store, receive
        self.session_guard = SessionGuard(store)
        self.before_drop = None
        self.group_admin = None
        self.group_conversation = None
        self.client = None
        self.state = "disconnected"
        self.name = ""
        self.phone = ""
        self.phone_hash = ""
        self.code_delivery = {}
        self.qr = None
        self.qr_waiter = None
        self.qr_svg = ""
        self.qr_expires = 0
        self.qr_version = 0
        self.qr_error = ""
        self.lock = asyncio.Lock()
        self.account_id = None
        self.account_username = ''
        self.online = False
        self.online_until = 0
        self.reply_presence_count = 0
        self.last_presence_update = 0
        self.reply_idle_until = 0
        self.presence_expiry = None
        self.presence_heartbeat = None
        self.presence_lock = asyncio.Lock()
        self.reply_presence_activated = False
        self.status_updates_seen = 0
        self.reply_typing = {}
        self.typing_locks = {}
        self.last_typing_start = {}
        self.next_presence = time.time() + 30 * 60

    async def drop(self):
        await self.session_guard.stop()
        if self.before_drop:
            await self.before_drop()
        typing = list(self.reply_typing.values())
        self.reply_typing.clear()
        for state in typing:
            state['task'].cancel()
        await asyncio.gather(*(state['task'] for state in typing),return_exceptions=True)
        if self.presence_heartbeat:
            self.presence_heartbeat.cancel()
            await asyncio.gather(self.presence_heartbeat, return_exceptions=True)
            self.presence_heartbeat = None
        if self.presence_expiry:
            self.presence_expiry.cancel()
            await asyncio.gather(self.presence_expiry, return_exceptions=True)
            self.presence_expiry = None
        self.reply_idle_until = 0
        self.reply_presence_activated = False
        self.reply_presence_count = 0
        await self.cancel_qr_worker()
        client, self.client = self.client, None
        self.online = False
        self.last_presence_update = 0
        if client:
            try:
                await asyncio.wait_for(client.disconnect(), 5)
            except (Exception, asyncio.CancelledError):
                pass

    async def connect(self):
        if self.client and self.client.is_connected():
            return
        cfg = self.store.secrets()
        if not cfg.get("telegram_api_id") or not cfg.get("telegram_api_hash"):
            raise ValueError("Добавьте API ID и API Hash Telegram")
        if self.client:
            await self.drop()
        self.client = TelegramClient(str(self.store.directory / "account"),
                int(cfg["telegram_api_id"]), cfg["telegram_api_hash"],
                connection=connection_on(int(os.getenv("TELEGRAM_PORT", "5222"))),
                flood_sleep_threshold=0, request_retries=0, connection_retries=2,
                timeout=8, raise_last_call_error=True)
        self.client.add_event_handler(self.on_message, events.NewMessage(incoming=True))
        self.client.add_event_handler(self.on_group_outgoing, events.NewMessage(outgoing=True))
        self.client.add_event_handler(self.on_status, events.Raw(types=types.UpdateUserStatus))
        self.client.add_event_handler(self.session_guard.on_authorization, events.Raw(types=types.UpdateNewAuthorization))
        try:
            await asyncio.wait_for(self.client.connect(), 20)
        except Exception:
            await self.drop()
            raise ValueError("Не удалось подключиться к Telegram. Повторите через минуту") from None
        path = self.store.directory / "account.session"
        if path.exists():
            path.chmod(0o600)

    async def rpc(self, operation):
        for attempt in range(3):
            try:
                return await asyncio.wait_for(operation(), 35)
            except (errors.PhoneMigrateError, errors.NetworkMigrateError, errors.UserMigrateError):
                if attempt == 2:
                    raise ValueError("Telegram сменил дата-центр. Повторите вход") from None
            except errors.FloodWaitError as error:
                raise ValueError(f"Telegram просит подождать {error.seconds} секунд") from None
            except errors.PhoneCodeInvalidError:
                raise ValueError("Неверный код Telegram") from None
            except errors.PhoneCodeExpiredError:
                self.phone_hash = ""
                self.code_delivery = {}
                self.state = "disconnected"
                raise ValueError("Код истёк. Запросите новый") from None
            except errors.PasswordHashInvalidError:
                raise ValueError("Неверный пароль двухэтапной проверки") from None
            except errors.PhoneNumberInvalidError:
                raise ValueError("Неверный номер телефона") from None
            except errors.ApiIdInvalidError:
                raise ValueError("Неверные API ID или API Hash Telegram") from None
            except errors.PhoneNumberBannedError:
                raise ValueError("Telegram заблокировал вход для этого номера") from None
            except (errors.PhoneNumberFloodError, errors.PhonePasswordFloodError):
                raise ValueError("Слишком много попыток входа. Telegram временно ограничил запросы кода") from None
            except errors.SendCodeUnavailableError:
                raise ValueError("Telegram не разрешает повторную отправку сейчас. Проверьте уже полученный код или повторите вход позже") from None
            except errors.SmsCodeCreateFailedError:
                raise ValueError("Telegram не смог отправить SMS. Повторите вход позже") from None
            except (asyncio.TimeoutError, OSError):
                raise ValueError("Telegram не ответил. Повторите запрос") from None

    async def authorized(self):
        me = await self.rpc(self.client.get_me)
        self.account_id = me.id
        self.account_username = getattr(me,'username','') or ''
        try:
            await self.session_guard.start(self.client, self.account_id)
        except GuardPolicyError:
            await self.drop()
            self.state = "disconnected"
            raise
        self.name = " ".join(x for x in (me.first_name, me.last_name) if x)
        self.state = "connected"
        self.phone_hash = ""
        self.code_delivery = {}
        self.clear_qr()
        self.store.event("Telegram-аккаунт подключён")

    async def start(self):
        async with self.lock:
            if not self.store.secrets().get("telegram_api_id") or self.state in {"code", "password", "qr"}:
                return
            try:
                await self.connect()
                if await self.rpc(self.client.is_user_authorized):
                    await self.authorized()
            except Exception as error:
                self.store.event("Подключение Telegram: " + type(error).__name__, "error")
                self.state = "disconnected"

    def login_hash(self, api_id, api_hash):
        if api_hash:
            return api_hash
        saved = self.store.secrets()
        if saved.get("telegram_api_id") != api_id or not saved.get("telegram_api_hash"):
            raise ValueError("Введите API Hash для этого API ID")
        return saved["telegram_api_hash"]

    async def request_code(self, api_id, api_hash, phone):
        async with self.lock:
            if self.state == "connected":
                raise ValueError("Аккаунт уже подключён. Сначала отключите его")
            api_hash = self.login_hash(api_id, api_hash)
            await self.drop()
            self.state, self.phone_hash, self.code_delivery = "disconnected", "", {}
            self.store.update_secrets(dict(telegram_api_id=api_id, telegram_api_hash=api_hash))
            self.phone = phone
            self.store.event("Запрос кода входа Telegram")
            try:
                await self.connect()
                if await self.rpc(self.client.is_user_authorized):
                    await self.authorized()
                    return {"state": self.state, "name": self.name}
                sent = await self.rpc(lambda: self.client.send_code_request(phone))
                return await self.accept_code(sent)
            except Exception as error:
                self.store.event("Запрос кода Telegram не завершён: " + (str(error) if isinstance(error, ValueError) else type(error).__name__), "error")
                raise

    def login_info(self):
        return dict(self.code_delivery) if self.state in {"code", "password"} else {}

    async def accept_code(self, sent):
        if isinstance(sent, types.auth.SentCodeSuccess):
            await self.authorized()
            return {"state": self.state, "name": self.name}
        self.state, self.phone_hash, self.code_delivery = "disconnected", "", {}
        if not isinstance(sent, types.auth.SentCode):
            self.store.event("Telegram вернул способ входа без кода: " + type(sent).__name__, "error")
            raise ValueError("Telegram не выдал код для этого способа входа. Используйте официальный клиент и повторите запрос позже")
        kind = type(sent.type).__name__
        self.store.event("Telegram принял запрос кода: " + kind)
        if kind == "SentCodeTypeSetUpEmailRequired":
            raise ValueError("Telegram требует настроить email для входа. Сделайте это в официальном приложении Telegram и повторите запрос")
        if kind == "SentCodeTypeFirebaseSms":
            raise ValueError("Telegram требует подтверждение SMS через официальное приложение. Войдите в него и повторите запрос из панели позже")
        if not sent.phone_code_hash:
            raise ValueError("Telegram не выдал данные для ввода кода. Повторите запрос позже")
        channel = {
            "SentCodeTypeApp": "telegram", "SentCodeTypeSms": "sms",
            "SentCodeTypeCall": "call", "SentCodeTypeFlashCall": "flash_call",
            "SentCodeTypeMissedCall": "missed_call", "SentCodeTypeEmailCode": "email",
            "SentCodeTypeSmsWord": "sms_word", "SentCodeTypeSmsPhrase": "sms_phrase",
            "SentCodeTypeFragmentSms": "fragment",
        }.get(kind, "unknown")
        email = str(getattr(sent.type, "email_pattern", "") or "")[:200]
        if email and "*" not in email:
            email = "***@" + email.rsplit("@", 1)[-1] if "@" in email else "***"
        self.phone_hash = sent.phone_code_hash
        self.state = "code"
        self.code_delivery = {
            "channel": channel, "phone_hint": "+••••" + self.phone[-4:],
            "email_hint": email,
            "code_length": getattr(sent.type, "length", None),
            "resend_at": time.time() + max(30, sent.timeout or 60),
        }
        return {"state": "code", "login": self.login_info()}

    async def resend_code(self):
        async with self.lock:
            if self.state != "code" or not self.phone_hash or not self.phone:
                raise ValueError("Сначала запросите код Telegram")
            remaining = self.code_delivery.get("resend_at", 0) - time.time()
            if remaining > 0:
                raise ValueError(f"Повторный код можно запросить через {int(remaining) + 1} секунд")
            self.store.event("Повторный запрос кода входа Telegram")
            try:
                await self.connect()
                sent = await self.rpc(lambda: self.client(functions.auth.ResendCodeRequest(
                    phone_number=self.phone.lstrip("+"), phone_code_hash=self.phone_hash)))
                return await self.accept_code(sent)
            except Exception as error:
                self.store.event("Повторный запрос кода Telegram не завершён: " + (str(error) if isinstance(error, ValueError) else type(error).__name__), "error")
                raise

    def clear_qr(self):
        self.qr, self.qr_svg, self.qr_expires, self.qr_error = None, "", 0, ""

    async def cancel_qr_worker(self):
        task, self.qr_waiter = self.qr_waiter, None
        self.clear_qr()
        if task and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def qr_info(self):
        return {"expires_at": self.qr_expires, "version": self.qr_version, "error": self.qr_error} if self.state == "qr" else {}

    async def request_qr(self, api_id, api_hash):
        import qrcode
        from qrcode.image.svg import SvgPathImage
        async with self.lock:
            if self.state == "connected":
                raise ValueError("Аккаунт уже подключён. Сначала отключите его")
            api_hash = self.login_hash(api_id, api_hash)
            await self.drop()
            self.state, self.phone_hash, self.code_delivery = "disconnected", "", {}
            self.store.update_secrets(dict(telegram_api_id=api_id, telegram_api_hash=api_hash))
            self.store.event("Запрос входа Telegram через QR-код")
            try:
                await self.connect()
                if await self.rpc(self.client.is_user_authorized):
                    await self.authorized()
                    return {"state": self.state}
                qr = await self.rpc(self.client.qr_login)
                self.qr_svg = qrcode.make(qr.url, image_factory=SvgPathImage, border=4).to_string(encoding="unicode")
                self.qr, self.qr_expires, self.state = qr, qr.expires.timestamp(), "qr"
                self.qr_version += 1
                self.qr_waiter = asyncio.create_task(self.wait_qr(qr))
                # Register the update listener before returning the scannable image.
                await asyncio.sleep(0)
                self.store.event("Telegram выдал QR-код для входа")
                return {"state": "qr", "qr": self.qr_info()}
            except Exception as error:
                self.clear_qr()
                self.state = "disconnected"
                self.store.event("Запрос QR-кода Telegram не завершён: " + type(error).__name__, "error")
                if isinstance(error, ValueError):
                    raise
                raise ValueError("Не удалось получить QR-код Telegram. Повторите позже") from None

    async def wait_qr(self, qr):
        try:
            await qr.wait()
            async with self.lock:
                if self.qr is qr:
                    await self.authorized()
        except errors.SessionPasswordNeededError:
            async with self.lock:
                if self.qr is qr:
                    self.clear_qr()
                    self.state = "password"
                    self.store.event("QR-код Telegram подтверждён; требуется пароль двухэтапной проверки")
        except asyncio.TimeoutError:
            async with self.lock:
                if self.qr is qr:
                    self.qr_svg = ""
                    self.qr_error = "QR-код истёк. Нажми «Обновить QR-код»."
                    self.store.event("QR-код входа Telegram истёк")
        except asyncio.CancelledError:
            raise
        except Exception as error:
            async with self.lock:
                if self.qr is qr:
                    self.qr_svg = ""
                    self.qr_error = "Не удалось завершить вход. Обнови QR-код и повтори сканирование."
                    self.store.event("Вход Telegram по QR не завершён: " + type(error).__name__, "error")
        finally:
            if self.qr_waiter is asyncio.current_task():
                self.qr_waiter = None

    async def cancel_login(self):
        async with self.lock:
            if self.state == "connected":
                raise ValueError("Аккаунт уже подключён")
            await self.drop()
            self.state, self.phone_hash, self.code_delivery = "disconnected", "", {}
            self.store.event("Ожидание входа Telegram остановлено в панели")
            return {"state": "disconnected"}

    async def submit_code(self, code):
        async with self.lock:
            if self.state != "code" or not self.phone_hash:
                raise ValueError("Сначала запросите код Telegram")
            try:
                await self.rpc(lambda: self.client.sign_in(phone=self.phone, code=code, phone_code_hash=self.phone_hash))
            except errors.SessionPasswordNeededError:
                self.state = "password"
                return {"state": "password"}
            await self.authorized()
            return {"state": self.state, "name": self.name}

    async def submit_password(self, password):
        async with self.lock:
            if self.state != "password":
                raise ValueError("Сначала введите код Telegram")
            await self.rpc(lambda: self.client.sign_in(password=password))
            await self.authorized()
            return {"state": self.state, "name": self.name}

    async def logout(self):
        async with self.lock:
            self.store.update_settings({"enabled": False})
            if self.client and self.state == "connected":
                await self.rpc(self.client.log_out)
            await self.drop()
            for suffix in ("", "-journal", "-wal", "-shm"):
                (self.store.directory / ("account.session" + suffix)).unlink(missing_ok=True)
            self.state, self.name, self.phone_hash = "disconnected", "", ""
            self.code_delivery = {}

    def ready(self):
        return bool(self.state == "connected" and self.client and self.client.is_connected())

    async def resolve(self, handle):
        if not self.ready():
            raise ValueError("Сначала подключите Telegram")
        user = await self.rpc(lambda: self.client.get_entity(int(handle) if handle.lstrip("-").isdigit() else handle))
        if isinstance(user, types.User) and not user.bot and user.id != self.account_id:
            return self.store.upsert_contact(user.id, " ".join(x for x in (user.first_name, user.last_name) if x), user.username or "")
        if isinstance(user, types.Chat) or (isinstance(user, types.Channel) and user.megagroup):
            return self.store.upsert_contact(utils.get_peer_id(user), user.title, getattr(user, "username", "") or "", "group")
        raise ValueError("Выберите личный аккаунт или группу")

    async def dialogs(self):
        if not self.ready():
            raise ValueError("Сначала подключите Telegram")
        dialogs = await self.rpc(lambda: self.client.get_dialogs(limit=100))
        return [dict(id=d.id, name=d.name, username=getattr(d.entity, "username", "") or "", kind="private" if d.is_user else "group")
                for d in dialogs if (d.is_user and not d.entity.bot and d.entity.id not in {self.account_id, SECURITY_PEER}) or d.is_group]

    async def on_message(self, event):
        if security_message(event):
            self.session_guard.wake.set()
            return
        if (self.session_guard.path.exists() or self.session_guard.marker.exists()) and not self.session_guard.identity_validated:
            return
        settings = self.store.settings()
        held_presence = False
        received_at = time.monotonic()
        typing_scheduled = False
        handed_over = False
        if not (event.is_private or event.is_group):
            return
        try:
            sender = await event.get_sender()
            if sender and sender.id == SECURITY_PEER:
                self.session_guard.wake.set()
                return
            if not sender or sender.id == self.account_id:
                return
            if event.is_group and self.group_conversation:
                await self.group_conversation.prepare(event,sender)
            if event.is_group and self.group_admin:
                if await self.group_admin.dispatch(event, sender):
                    return
                self.group_admin.warnings.schedule(event,sender)
            if self.store.user_blocked(sender.id):
                return
            if getattr(sender, 'bot', False) or isinstance(sender, (types.Channel, types.Chat)):
                return
            if event.is_group and not settings['group_enabled']:
                return
            forwarded = bool(getattr(event.message, 'fwd_from', None) or getattr(event.message, 'via_bot_id', None)
                             or getattr(event.message, 'edit_date', None)
                             or any(isinstance(e, (types.MessageEntityBlockquote, types.MessageEntityPre, types.MessageEntityCode))
                                    for e in (getattr(event.message, 'entities', None) or [])))
            from .authority import authorized
            owner = bool(event.is_private and authorized(self.store, sender.id) and not forwarded)
            if not settings["enabled"] and not owner:
                return
            sender_name = " ".join(x for x in (getattr(sender, "first_name", ""), getattr(sender, "last_name", "")) if x) or "Участник"
            contact_id = sender.id
            if event.is_group:
                from .group_conversation import topic_root_reply, reply_message_id
                reply = await event.get_reply_message() if event.is_reply and not topic_root_reply(event.message) else None
                direct = ((settings["group_names_enabled"] and addressed(event.raw_text or "", settings["group_aliases"])) or
                        (settings["group_mentions_enabled"] and event.mentioned) or
                        (settings["group_replies_enabled"] and reply and reply.sender_id == self.account_id))
                if not direct and (not settings['group_auto_reply_enabled'] or not self.group_conversation):
                    return
                chat = await event.get_chat()
                contact_id = event.chat_id
                self.store.upsert_contact(contact_id, getattr(chat, "title", "Группа"), getattr(chat, "username", "") or "", "group")
            else:
                self.store.upsert_contact(sender.id, sender_name, sender.username or "")
            if not self.store.contact(contact_id)["enabled"] and not owner:
                return
            if getattr(self,'media_assets',None):
                self.media_assets.capture(event.message,contact_id)
            typing_scheduled = bool(not event.is_group and self.schedule_reply_typing(contact_id,event.id,owner=owner,received_at=received_at))
            if not event.is_group and settings["online_during_reply"] and settings["reply_online_immediate"]:
                try:
                    held_presence = await self.begin_reply_presence()
                except Exception:
                    pass
            text = event.raw_text or ""
            image_path = ""
            document = event.message.document
            mime = getattr(document,'mime_type','') or 'image/jpeg'
            attributes = getattr(document,'attributes',[]) or []
            is_sticker = bool(getattr(event.message,'sticker',None) or any(isinstance(attr,(types.DocumentAttributeSticker,types.DocumentAttributeCustomEmoji)) for attr in attributes))
            is_gif = bool(getattr(event.message,'gif',None) or mime=='image/gif' or any(isinstance(attr,types.DocumentAttributeAnimated) for attr in attributes))
            image_kind = 'sticker' if is_sticker else 'gif' if is_gif else 'photo'
            is_image = bool(event.message.photo or is_sticker or is_gif or (document and mime.startswith("image/")))
            if event.is_group and is_image and not direct and not await self.group_conversation.should_reply(event,sender):
                return
            if is_image and (settings["vision_enabled"] or owner):
                try:
                    if event.message.file and (event.message.file.size or 0) > MAX_BYTES:
                        raise ValueError("Фото больше 12 МБ: пришлите уменьшенную версию")
                    async with asyncio.timeout(25):
                        raw = await self.client.download_media(event.message, file=bytes)
                    image_path = await asyncio.to_thread(save_visual, self.store.directory, raw,image_kind,mime)
                    default_text = {'photo':'Посмотри на фото и ответь по ситуации','gif':'Посмотри GIF и ответь по ситуации','sticker':'Посмотри стикер и ответь по ситуации'}
                    text = text or default_text[image_kind]
                except Exception as error:
                    text += "\n[Фото не удалось загрузить: " + (str(error) if isinstance(error, ValueError) else "ошибка загрузки") + ". Не выдумывай его содержимое.]"
            elif is_image:
                text += "\n[Просмотр фото выключен в панели. Содержимое изображения неизвестно.]"
            accepted = self.receive(contact_id, text or "[Вложение: содержимое не распознано]", event.id,
                         **({'group_direct':bool(direct)} if event.is_group else {}),
                         sender_id=sender.id, sender_name=sender_name, image_path=image_path,image_kind=image_kind,
                         reply_to_telegram_id=(reply_message_id(event.message) if event.is_group else getattr(event.message,'reply_to_msg_id',None)), forwarded=forwarded)
            handed_over = accepted is not False
        except Exception as error:
            self.store.event("Входящее сообщение: " + type(error).__name__, "error")
        finally:
            if typing_scheduled and not handed_over:
                await self.stop_reply_typing(contact_id,event.id)
            if held_presence:
                try:
                    await self.end_reply_presence()
                except Exception:
                    pass

    async def on_group_outgoing(self, event):
        if event.is_group and self.group_conversation:
            try:
                self.group_conversation.remember(event.chat_id,event.message)
            except Exception as error:
                self.store.event('Контекст исходящего сообщения группы: '+type(error).__name__,'error')

    def require_unblocked(self, contact_id):
        if self.store.dialog_blocked(contact_id):
            raise ValueError('Собеседник заблокирован; отправка остановлена')

    async def send(self, contact_id, text, *, manual=False, immediate=False, before_dispatch=None, reply_to=None):
        if not self.ready():
            raise ValueError("Telegram не подключён")
        self.require_unblocked(contact_id)
        settings = self.store.settings()
        natural = settings["typing_enabled"] and not immediate
        seconds = min(settings["typing_max_seconds"], max(settings["typing_min_seconds"],
                      len(text) / settings["typing_cps"] * delay(settings, "typing_variation"))) if natural else 0
        early = self.reply_typing.get(contact_id)
        if natural and early and not early['task'].done():
            await early['ready'].wait()
            if early['started_at'] is not None:
                natural = False
                seconds = max(0,settings['typing_min_seconds']-(time.monotonic()-early['started_at']))
        if natural and settings["typing_pause_enabled"] and seconds > settings["typing_pause_threshold"]:
            async with self.client.action(contact_id, "typing"):
                await asyncio.sleep(seconds * 0.45)
            await asyncio.sleep(delay(settings, "typing_pause"))
            seconds *= 0.55
        async with (self.client.action(contact_id, "typing") if natural else nullcontext()):
            if seconds:
                await asyncio.sleep(seconds)
            from .authority import authorized
            if (not self.store.contact(contact_id)["enabled"] and not (manual and authorized(self.store, contact_id))) or (not manual and settings["enabled"] and not self.store.settings()["enabled"]):
                raise ValueError("Отправка отключена во время печатания")
            # Never automatically retry a send: delivery may succeed before a connection fails.
            self.require_unblocked(contact_id)
            if before_dispatch:
                before_dispatch()
            sent = await self.client.send_message(contact_id, text, parse_mode=None, link_preview=False,
                                                  **({'reply_to': reply_to} if reply_to is not None else {}))
        return sent.id

    def schedule_reply_typing(self, contact_id, telegram_id, *, owner=False, received_at=None):
        settings = self.store.settings()
        if not telegram_id or not settings['typing_enabled'] or not settings['typing_during_generation']:
            return None
        previous = self.reply_typing.get(contact_id)
        if previous and previous['telegram_id']==telegram_id and not previous['task'].done():
            return previous
        if previous:
            previous['task'].cancel()
        state = dict(contact_id=contact_id,telegram_id=telegram_id,owner=owner,
            received_at=time.monotonic() if received_at is None else received_at,
            started_at=None,ready=asyncio.Event(),cancel_previous=bool(previous and previous['started_at'] is not None))
        self.reply_typing[contact_id] = state
        state['task'] = asyncio.create_task(self.run_reply_typing(state))
        return state

    async def typing_request(self, contact_id, action):
        async with self.typing_locks.setdefault(contact_id,asyncio.Lock()):
            if self.ready():
                await self.client(functions.messages.SetTypingRequest(peer=contact_id,action=action))

    async def run_reply_typing(self, state):
        contact_id = state['contact_id']
        try:
            if state['cancel_previous']:
                await self.typing_request(contact_id,types.SendMessageCancelAction())
            due = state['received_at']+self.store.settings()['typing_start_delay']
            await asyncio.sleep(max(0,due-time.monotonic()))
            while self.reply_typing.get(contact_id) is state:
                settings = self.store.settings()
                if not settings['typing_enabled'] or not settings['typing_during_generation'] or (
                    (not settings['enabled'] or not self.store.contact(contact_id)['enabled']) and not state['owner']):
                    return
                if not self.ready():
                    return
                await self.typing_request(contact_id,types.SendMessageTypingAction())
                if state['started_at'] is None:
                    state['started_at'] = time.monotonic()
                    self.last_typing_start = dict(contact_id=contact_id,telegram_id=state['telegram_id'],
                        delay_seconds=state['started_at']-state['received_at'],time=time.time())
                state['ready'].set()
                await asyncio.sleep(4)
        except asyncio.CancelledError:
            pass
        except Exception as error:
            if not isinstance(error, (errors.ChatWriteForbiddenError, errors.UserBannedInChannelError, errors.ChannelPrivateError)):
                self.store.event('Не удалось обновить индикатор печатания','error')
        finally:
            state['ready'].set()

    async def stop_reply_typing(self, contact_id, telegram_id):
        state = self.reply_typing.get(contact_id)
        if not state or state['telegram_id']!=telegram_id:
            return
        state['task'].cancel()
        await asyncio.gather(state['task'],return_exceptions=True)
        if self.reply_typing.get(contact_id) is state:
            self.reply_typing.pop(contact_id,None)
            if state['started_at'] is not None:
                try:
                    await self.typing_request(contact_id,types.SendMessageCancelAction())
                except Exception:
                    pass

    async def set_profile_photo(self, photo, before_dispatch=None):
        if not self.ready():
            raise ValueError("Telegram не подключён")
        uploaded = await self.client.upload_file(photo)
        if before_dispatch:
            before_dispatch()
        # Profile mutations run once, without the read-only RPC retry wrapper.
        await self.client(functions.photos.UploadProfilePhotoRequest(file=uploaded))

    async def set_profile_details(self, values):
        if not self.ready():
            raise ValueError("Telegram не подключён")
        await self.client(functions.account.UpdateProfileRequest(**values))
        if "first_name" in values or "last_name" in values:
            self.name = " ".join(value for value in (values.get("first_name", self.name), values.get("last_name", "")) if value)

    async def read(self, contact_id, message_id):
        if self.ready() and message_id:
            await self.client.send_read_acknowledge(contact_id, max_id=message_id)

    async def presence_info(self):
        if not self.ready():
            raise ValueError("Telegram не подключён")
        me = await self.rpc(self.client.get_me)
        status = getattr(me, "status", None)
        return dict(online=self.online, processing=self.reply_presence_count > 0,
                    server_status=type(status).__name__,
                    server_online=isinstance(status, types.UserStatusOnline),
                    idle_until=self.reply_idle_until, status_updates_seen=self.status_updates_seen,
                    typing_contacts=list(self.reply_typing),last_typing_start=self.last_typing_start,
                    heartbeat_running=bool(self.presence_heartbeat and not self.presence_heartbeat.done()))

    def keep_reply_online(self):
        return bool((self.reply_presence_count and self.reply_presence_activated) or
                    self.reply_idle_until > time.time())

    async def on_status(self, update):
        if update.user_id != self.account_id:
            return
        self.status_updates_seen += 1
        self.online = isinstance(update.status, types.UserStatusOnline)
        if not self.online and self.keep_reply_online():
            try:
                await self.presence(60, force=True, for_reply=True)
            except Exception:
                self.store.event("Не удалось восстановить онлайн во время переписки", "error")

    async def presence(self, seconds, force=False, for_reply=False):
        async with self.presence_lock:
            if not self.ready() or (for_reply and not self.keep_reply_online()):
                return
            now = time.time()
            if force or not self.online or now - self.last_presence_update >= 40:
                await self.client(functions.account.UpdateStatusRequest(offline=False))
                self.online = True
                self.last_presence_update = now
            self.online_until = max(self.online_until, now + seconds)
            if self.reply_presence_count:
                self.reply_presence_activated = True

    async def begin_reply_presence(self, activate=True):
        if not self.ready():
            return False
        if not self.reply_presence_count:
            self.reply_presence_activated = self.online
        self.reply_presence_count += 1
        if self.presence_expiry:
            self.presence_expiry.cancel()
            self.presence_expiry = None
        self.reply_idle_until = 0
        try:
            if activate:
                await self.presence(60, force=self.reply_presence_count == 1)
        except Exception:
            self.reply_presence_count -= 1
            raise
        if not self.presence_heartbeat or self.presence_heartbeat.done():
            self.presence_heartbeat = asyncio.create_task(self.refresh_reply_presence())
        return True

    async def refresh_reply_presence(self):
        try:
            while self.ready() and (self.reply_presence_count or self.reply_idle_until > time.time()):
                await asyncio.sleep(REPLY_PRESENCE_REFRESH_SECONDS)
                if self.keep_reply_online():
                    try:
                        await self.presence(60, force=True, for_reply=True)
                    except errors.FloodWaitError as error:
                        await asyncio.sleep(error.seconds)
                    except Exception:
                        self.store.event("Не удалось продлить онлайн во время переписки", "error")
        finally:
            if self.presence_heartbeat is asyncio.current_task():
                self.presence_heartbeat = None

    async def end_reply_presence(self):
        self.reply_presence_count = max(0, self.reply_presence_count - 1)
        if not self.reply_presence_count and self.ready() and (self.online or self.reply_presence_activated):
            self.reply_idle_until = time.time() + self.store.settings()["reply_idle_seconds"]
            self.next_presence = max(self.next_presence, time.time() + delay(self.store.settings(), "presence_interval") * 60)
            if self.presence_expiry:
                self.presence_expiry.cancel()
            if self.reply_idle_until <= time.time():
                await self.expire_reply_presence(self.reply_idle_until)
            else:
                self.presence_expiry = asyncio.create_task(self.expire_reply_presence(self.reply_idle_until))

    async def expire_reply_presence(self, until):
        try:
            await asyncio.sleep(max(0, until - time.time()))
            async with self.presence_lock:
                if self.reply_presence_count or self.reply_idle_until != until or not self.ready():
                    return
                self.reply_idle_until = 0
                self.reply_presence_activated = False
                if self.online:
                    await self.client(functions.account.UpdateStatusRequest(offline=True))
                    self.online = False
                    self.online_until = 0
        except Exception:
            self.store.event("Не удалось обновить статус Telegram после ответа", "error")

    async def periodic_presence(self, active):
        settings = self.store.settings()
        if not self.ready():
            return
        if self.reply_presence_count:
            if self.reply_presence_activated:
                await self.presence(60, for_reply=True)
            return
        if self.reply_idle_until > time.time():
            return
        now = time.time()
        if self.online and (not active or now >= self.online_until):
            async with self.presence_lock:
                if self.reply_presence_count or self.reply_idle_until > time.time():
                    return
                await self.client(functions.account.UpdateStatusRequest(offline=True))
                self.online = False
        if active and not self.reply_presence_count and self.reply_idle_until <= time.time() and now >= self.next_presence:
            await self.presence(delay(settings, "presence_duration"))
            self.next_presence = now + delay(settings, "presence_interval") * 60

    async def react(self, contact_id, message_id, emoji):
        if not self.ready() or not message_id:
            raise ValueError("Нет входящего сообщения для реакции")
        self.require_unblocked(contact_id)
        try:
            await self.client(functions.messages.SendReactionRequest(peer=contact_id, msg_id=message_id,
                        reaction=[types.ReactionEmoji(emoticon=emoji)], big=False, add_to_recent=True))
        except errors.RPCError:
            raise ValueError("Telegram не разрешил эту реакцию в данном чате") from None

    async def gift_catalog(self):
        if not self.ready():
            raise ValueError("Сначала подключите Telegram, чтобы загрузить подарки")
        catalog = await self.rpc(lambda: self.client(functions.payments.GetStarGiftsRequest(hash=0)))
        return [dict(id=str(gift.id), stars=gift.stars,
                     title=getattr(gift, "title", None) or next((getattr(a, "alt", "") for a in gift.sticker.attributes if getattr(a, "alt", "")), "Подарок"),
                     available=not bool(getattr(gift, "sold_out", False) or getattr(gift, "auction", False)
                       or getattr(gift, "require_premium", False) or (getattr(gift, "limited_per_user", False) and not getattr(gift, "per_user_remains", 0))))
                for gift in getattr(catalog, "gifts", [])]

    async def stars_balance(self):
        if not self.ready():
            raise ValueError("Telegram не подключён")
        result = await self.rpc(lambda: self.client(functions.payments.GetStarsStatusRequest(peer=types.InputPeerSelf())))
        balance = result.balance
        return int(getattr(balance, "amount", balance))

    async def send_gift(self, contact_id, gift_id, message, expected_stars, before_payment):
        from .gifts import PurchaseBlocked
        if not self.ready():
            raise PurchaseBlocked("Telegram не подключён")
        try:
            peer = await self.client.get_input_entity(contact_id)
            invoice = types.InputInvoiceStarGift(peer=peer, gift_id=gift_id,
                        message=types.TextWithEntities(text=message, entities=[]))
            form = await self.rpc(lambda: self.client(functions.payments.GetPaymentFormRequest(invoice=invoice)))
            if form.invoice.currency != "XTR" or sum(price.amount for price in form.invoice.prices) != expected_stars:
                raise PurchaseBlocked("Цена подарка изменилась. Обновите каталог")
            before_payment()
        except PurchaseBlocked:
            raise
        except Exception:
            raise PurchaseBlocked("Telegram не подготовил подарок. Покупка не выполнялась; повторите позже") from None
        # Payment is sent once. Retrying a purchase after a lost response could charge twice.
        result = await self.client(functions.payments.SendStarsFormRequest(form_id=form.form_id, invoice=invoice))
        if not isinstance(result, types.payments.PaymentResult):
            raise ValueError("Telegram не подтвердил завершённую оплату")

    async def send_voice(self, contact_id, path, duration, *, manual=False, reply_to=None, before_dispatch=None):
        if not self.ready():
            raise ValueError('Telegram не подключён')
        self.require_unblocked(contact_id)
        async with self.client.action(contact_id, 'record-audio'):
            # Uploading a temporary file is safe to cancel. Mark dispatch only
            # when the final Telegram SendMedia request is about to be made.
            uploaded = await self.client.upload_file(str(path))
            settings = self.store.settings()
            from .authority import authorized
            allowed_owner = manual and authorized(self.store, contact_id)
            if (not self.store.contact(contact_id)['enabled'] and not allowed_owner) or (not settings['enabled'] and not allowed_owner):
                raise ValueError('Отправка отключена для контакта')
            if not settings['voice_messages_enabled']:
                raise ValueError('Голосовые ответы выключены')
            self.require_unblocked(contact_id)
            if before_dispatch:
                before_dispatch()
            sent = await self.client.send_file(contact_id, uploaded, voice_note=True,
                attributes=[types.DocumentAttributeAudio(duration=duration, voice=True)],
                reply_to=reply_to, parse_mode=None)
        return sent.id

    async def send_media(self, contact_id, path, caption, before_dispatch=None, reply_to=None):
        if not self.ready():
            raise ValueError("Telegram не подключён")
        self.require_unblocked(contact_id)
        payload = str(path)
        if before_dispatch:
            payload = await self.client.upload_file(payload)
            before_dispatch()
            self.require_unblocked(contact_id)
        sent = await self.client.send_file(contact_id, payload, caption=caption, parse_mode=None,
            **({'reply_to':reply_to} if reply_to else {}))
        return sent.id

    async def edit(self, contact_id, message_id, text):
        if not self.ready():
            raise ValueError("Telegram не подключён")
        await self.client.edit_message(contact_id, message_id, text, parse_mode=None)
