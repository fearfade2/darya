from __future__ import annotations

import asyncio
import time
import random
import csv
import io
import os
import re
import secrets
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path
from urllib.parse import urlsplit

from telethon import errors

from .ai import Model, ModelUnavailable
from .brightdata import CloudUnavailable,CloudPending
from .avito import filters_from_url, validate_filters
from .dialogs import current_dialog
from .control import OwnerControl, OWNER_NAMES, owner_request, controller_request
from .telegram import Telegram
from .voice_messages import reply_format, spoken_text, voice_note, VoiceRenderUnavailable
from .gifts import Gifts
from .hermes import Hermes
from .behavior import REACTIONS, chunks, delay
from .websites import Websites, hermes_request, validate_public_url
from .privacy import has_location, redact_locations
from .image_generation import ImageGenerator, parse_generation_request


class Deferred(ValueError):
    def __init__(self, message, until):
        super().__init__(message)
        self.until = until


def allowed_time(now, settings):
    zone = ZoneInfo(settings["timezone"])
    local = datetime.fromtimestamp(now, zone)
    start, end = settings["quiet_start"], settings["quiet_end"]
    quiet = start != end and (start <= local.hour < end if start < end else local.hour >= start or local.hour < end)
    if not quiet:
        return now
    target = local.replace(hour=end, minute=0, second=0, microsecond=0)
    if target <= local:
        target += timedelta(days=1)
    return target.timestamp()


def due_time(value):
    try:
        when = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if when.tzinfo is None:
            raise ValueError()
        due = when.timestamp()
        if not time.time() - 2 <= due <= time.time() + 365 * 86400:
            raise ValueError()
        return due
    except (ValueError, AttributeError, OverflowError):
        raise ValueError("Укажите будущее время с часовым поясом, не дальше года") from None


def website_messages(text, limit=3800):
    while len(text) > limit:
        end = text.rfind("\n", 0, limit)
        if end <= 0:
            end = limit
        yield text[:end]
        text = text[end:].lstrip("\n")
    if text:
        yield text


class Runtime:
    def __init__(self, store):
        self.store = store
        self.model = Model(store)
        self.image_generator = ImageGenerator(self)
        self.telegram = Telegram(store, self.receive)
        self.gifts = Gifts(store, self.telegram)
        self.hermes = Hermes(store)
        self.websites = Websites(store)
        self.websites.match_listings = self.model.match_listings
        self.work = set()
        self.contact_locks = {}
        self.send_lock = asyncio.Lock()
        self.scheduler = None
        self.startup = None
        self.busy = set()
        self.flood_until = 0
        self.write_forbidden_until = {}
        self.next_reconnect = 0
        self.next_catalog = 0
        self.incoming = {}
        self.group_incoming = {}
        self.group_pending = {}
        self.reply_states = {}
        self.pending_batches = {}
        self.reply_retries = {}
        self.website_workers = {}
        from .calls import Calls
        self.calls = Calls(self)
        self.model.telegram_call_context = self.calls.context_for
        self.telegram.before_drop = self.calls.close
        self.control = OwnerControl(self)
        from .group_admin import GroupAdmin
        self.group_admin = GroupAdmin(self)
        from .authority import Authority
        self.authority = Authority(self)
        self.telegram.group_admin = self.group_admin
        from .group_conversation import GroupConversation
        self.group_conversation = GroupConversation(self)
        self.telegram.group_conversation = self.group_conversation
        self.model.group_conversation = self.group_conversation
        from .group_initiative import GroupInitiative
        self.group_initiative = GroupInitiative(self)
        from .notebook import Notebook
        self.notebook = Notebook(self)
        self.model.conversation_notebook = self.notebook
        from .contact_blocks import ContactBlocks
        self.contact_blocks = ContactBlocks(self)
        from .telegram_account import TelegramAccount
        self.account = TelegramAccount(self)
        from .telegram_media import TelegramMedia
        self.telegram_media = TelegramMedia(self)
        self.model.telegram_media = self.telegram_media
        self.telegram.media_assets = self.telegram_media
        from .telegram_actions import TelegramActions
        self.telegram_actions = TelegramActions(self)
        from .telegram_autonomy import TelegramAutonomy
        self.telegram_autonomy = TelegramAutonomy(self)
        from .private_initiative import PrivateInitiative
        self.private_initiative = PrivateInitiative(self)
        from .research import WebResearch
        from .publications import Publications
        self.research = WebResearch(self)
        self.publications = Publications(self)

    def spawn(self, coroutine):
        from .control import group_request
        token=group_request.set(None)
        try:task = asyncio.create_task(coroutine)
        finally:group_request.reset(token)
        self.work.add(task)
        task.add_done_callback(self.work.discard)
        return task

    async def start(self):
        self.startup = self.spawn(self.start_telegram())
        self.scheduler = self.spawn(self.loop())
        for row in self.store.rows("SELECT contact_id FROM pending_replies WHERE status='pending'"):
            self.start_reply_retry(row["contact_id"])
        for row in self.store.rows("SELECT * FROM jobs WHERE provider='brightdata' AND status='pending'"):
            try:
                parameters = json.loads(row['request_details'] or row['details'])
                self.start_website(row['id'],parameters['url'],parameters['max_items'],parameters['pages'],row['contact_id'] or 0,
                    filters=parameters.get('filters'))
            except (ValueError,KeyError,TypeError):
                self.store.execute("UPDATE jobs SET status='failed',result='Не удалось восстановить параметры сбора' WHERE id=?",(row['id'],))

    async def start_telegram(self):
        await self.telegram.start()
        if self.telegram.ready() and self.store.settings()['calls_enabled']:
            try:
                await self.calls.start()
            except Exception as error:
                self.store.event('Подключение библиотеки звонков: ' + type(error).__name__, 'error')

    async def stop(self):
        for task in list(self.work):
            task.cancel()
        await asyncio.gather(*list(self.work), return_exceptions=True)
        await self.calls.close()
        await self.telegram.drop()
        await self.model.close()
        await self.hermes.close()
        await self.websites.close()

    def media(self, contact_id=None):
        directory = self.store.directory / "media"
        directory.mkdir(exist_ok=True, mode=0o700)
        exports = {row["artifact"]: self.store.record_scope(row) for row in self.store.rows("SELECT * FROM jobs WHERE artifact!=''")}
        exports.update({row['filename']:self.store.record_scope(row) for row in self.store.rows('SELECT * FROM dialog_artifacts')})
        scope = self.store.dialog_scope(contact_id) if contact_id is not None else None
        owner = self.store.settings()['owner_id']
        def allowed(name):
            return contact_id is None or exports.get(name,(owner,0,0)) == scope
        return [{"filename": path.name, "bytes": path.stat().st_size} for path in sorted(directory.iterdir()) if path.is_file() and not path.is_symlink() and allowed(path.name)]

    async def submit_job(self, task, contact_id=0, *, actor_id=None):
        settings = self.store.settings()
        if not settings["hermes_enabled"]:
            raise ValueError("Hermes отключён в панели")
        if contact_id and (actor_id or contact_id) != settings["owner_id"] and not self.store.contact(contact_id)["hermes_allowed"]:
            raise ValueError("Задания Hermes доступны только разрешённым владельцем собеседникам")
        if not isinstance(task, str) or not 1 <= len(task) <= 12000:
            raise ValueError("Задание должно содержать 1–12000 символов")
        running = self.store.rows("SELECT COUNT(*) AS n FROM jobs WHERE status IN ('pending','running')")[0]["n"]
        if running >= 3:
            raise ValueError("Дождитесь завершения текущих заданий Hermes")
        now = time.time()
        scope = self.store.dialog_scope(contact_id)
        job_id = self.store.execute("INSERT INTO jobs(contact_id,task,created,updated,context_sender_id,context_generation) VALUES (?,?,?,?,?,?)", (contact_id or None, task, now, now,*scope[1:])).lastrowid
        self.spawn(self.run_job(job_id, task, contact_id))
        return {"job_id": job_id, "status": "pending"}

    async def parse_website(self, url, max_items=20, pages=1, contact_id=0, export=False, filters=None):
        await validate_public_url(url)
        cloud = self.websites.cloud.configured()
        host = urlsplit(url).hostname
        if cloud and (host == "avito.ru" or host.endswith(".avito.ru")):
            return await self.submit_website(url, max_items, pages, contact_id, filters=filters)
        result = await self.websites.parse(url, max_items, pages, export=export, filters=filters)
        if result.get('filename'):
            self.store.execute('INSERT OR REPLACE INTO dialog_artifacts VALUES (?,?,?,?,?)',
                               (result['filename'],*self.store.dialog_scope(contact_id),time.time()))
        if result["blocked"] and cloud:
            return await self.submit_website(url, max_items, pages, contact_id, filters=filters)
        return result

    async def submit_website(self, url, max_items=20, pages=1, contact_id=0, filters=None):
        await validate_public_url(url)
        if not self.websites.cloud.configured():
            raise ValueError("Bright Data не подключён")
        if not isinstance(max_items, int) or not 1 <= max_items <= 50 or not isinstance(pages, int) or not 1 <= pages <= 3:
            raise ValueError("Выберите 1–50 записей и 1–3 страницы")
        if contact_id and contact_id != self.store.settings()["owner_id"] and not self.store.contact(contact_id)["hermes_allowed"]:
            raise ValueError("Сбор сайтов доступен только разрешённым владельцем собеседникам")
        filters = validate_filters(filters_from_url(url) | (filters or {}))
        parameters = json.dumps(dict(url=url, max_items=max_items, pages=pages, filters=filters), sort_keys=True)
        scope = self.store.dialog_scope(contact_id)
        rows = self.store.rows("SELECT id FROM jobs WHERE provider='brightdata' AND status IN ('pending','running') AND COALESCE(contact_id,0)=? AND details=? AND context_sender_id=? AND context_generation=?", (contact_id, parameters,*scope[1:]))
        if rows:
            return {"job_id": rows[0]["id"], "status": "running", "provider": "brightdata", "message": "Сайт уже собирается; результат и CSV придут после завершения"}
        running = self.store.rows("SELECT COUNT(*) AS n FROM jobs WHERE status IN ('pending','running')")[0]["n"]
        if running >= 3:
            raise ValueError("Дождитесь завершения текущих заданий")
        now = time.time()
        job_id = self.store.execute("INSERT INTO jobs(contact_id,task,created,updated,provider,details,request_details,context_sender_id,context_generation) VALUES (?,?,?,?,?,?,?,?,?)",
            (contact_id or None, "Собрать объявления: " + url, now, now, "brightdata", parameters,parameters,*scope[1:])).lastrowid
        self.start_website(job_id,url,max_items,pages,contact_id,filters=filters)
        return {"job_id": job_id, "status": "pending", "provider": "brightdata", "message": "Начат сбор сайта; результат и CSV придут после завершения. Повторять запрос не нужно"}

    def start_website(self,job_id,*args,**kwargs):
        previous = self.website_workers.get(job_id)
        if previous and not previous.done():
            return previous
        task = self.spawn(self.run_website(job_id,*args,**kwargs))
        self.website_workers[job_id] = task
        def finished(done):
            if self.website_workers.get(job_id) is done:
                self.website_workers.pop(job_id,None)
        task.add_done_callback(finished)
        return task

    async def run_website(self, job_id, url, max_items, pages, contact_id, filters=None):
        await self.scoped_job(job_id,self._run_website(job_id,url,max_items,pages,contact_id,filters))

    async def scoped_job(self, job_id, coroutine):
        row = self.store.rows('SELECT * FROM jobs WHERE id=?',(job_id,))[0]
        if not self.store.context_record_active(row):
            coroutine.close()
            return
        token = current_dialog.set(self.store.record_scope(row))
        try:
            return await coroutine
        finally:
            current_dialog.reset(token)

    async def _run_website(self, job_id, url, max_items, pages, contact_id, filters=None):
        while True:
            rows = self.store.rows('SELECT status,retry_due,attempts,created FROM jobs WHERE id=?',(job_id,))
            if not rows or rows[0]['status'] not in {'pending','running'}:
                return
            saved = rows[0]
            await asyncio.sleep(max(0,saved['retry_due']-time.time()))
            try:
                with self.websites.cloud.job(job_id):
                    await self.collect_website(job_id,url,max_items,pages,contact_id,filters=filters)
                return
            except (CloudUnavailable,ModelUnavailable) as error:
                source = 'model' if isinstance(error,ModelUnavailable) else 'cloud'
                self.store.execute('UPDATE jobs SET last_error=?,error_source=? WHERE id=?',(str(error)[:500],source,job_id))
                attempts = saved['attempts']+(0 if isinstance(error,CloudPending) else 1)
                if attempts>=6 or time.time()-saved['created']>1800:
                    message = ('Проверка объявлений моделью пока недоступна. Полученные страницы и успешные проверки сохранены; поиск можно продолжить позже.' if source=='model' else
                               'Сервис сбора пока недоступен. Параметры и номера заданий сохранены; повтор можно запустить позже.')
                    self.store.execute("UPDATE jobs SET status='failed',result=?,attempts=?,updated=? WHERE id=?",
                        (message,attempts,time.time(),job_id))
                    self.store.event(f'Сбор сайта {job_id}: повторные проверки исчерпаны','error')
                    if contact_id and self.telegram.ready():
                        try:await self.send(contact_id,message,automated=True)
                        except Exception:pass
                    return
                due = time.time()+max(error.retry_after,min(120,15*2**max(0,attempts-1)))
                message = 'Сбор продолжается; проверка сохранённого задания будет повторена автоматически.'
                self.store.execute("UPDATE jobs SET status='pending',result=?,attempts=?,retry_due=?,updated=? WHERE id=?",
                    (message,attempts,due,time.time(),job_id))
                self.store.event(f'Сбор сайта {job_id}: {source}: {str(error)[:160]}; сохранён для следующей проверки')

    async def collect_website(self, job_id, url, max_items, pages, contact_id, filters=None):
        self.store.execute("UPDATE jobs SET status='running',updated=? WHERE id=?", (time.time(), job_id))
        try:
            result = await self.websites.parse(url, max_items, pages, export=True, cloud=True, filters=filters)
            if result["blocked"]:
                raise ValueError("Сайт ограничил доступ даже через Bright Data; объявления не получены")
        except (CloudUnavailable,ModelUnavailable):
            raise
        except Exception as error:
            message = str(error)[:500] if isinstance(error, ValueError) else type(error).__name__
            self.store.execute("UPDATE jobs SET status='failed',result=?,updated=? WHERE id=?", (message, time.time(), job_id))
            self.store.event("Сбор сайта: " + message, "error")
            if contact_id and self.telegram.ready():
                try:
                    await self.send(contact_id, "Собрать сайт не удалось: " + message, automated=True)
                except Exception:
                    pass
            return
        summary = f"Собрано {len(result['items'])} объявлений · {result['pages']} стр.\n"
        region = (filters or {}).get("city_slug")
        if region:
            summary += "Регион: " + ("вся Россия" if region == "all" else region) + "\n"
        if (filters or {}).get("description"):
            summary += ("Допускается: " if filters.get("description_optional") else "Обязательное условие: ") + filters["description"] + "\n"
        if result.get("warning"):
            summary += result["warning"] + "\n"
        for index, item in enumerate(result["items"][:10], 1):
            summary += f"\n{index}. {item['title']} — {item['price']} — {item['location']}\n{item['url']}\n"
        if not result["items"]:
            summary += "Не найдено объявлений, подтверждающих все условия поиска." if result.get("filters") else result["text"][:3000] or "На странице не найдено объявлений"
        elif len(result["items"]) < max_items:
            summary += f"\nПо заданным условиям получено {len(result['items'])} из запрошенных {max_items}."
        artifact = result.get("filename", "")
        self.store.execute("UPDATE jobs SET status='completed',last_error='',error_source='',result=?,artifact=?,details=?,updated=? WHERE id=?",
            (summary[:24000], artifact, json.dumps(result, ensure_ascii=False), time.time(), job_id))
        self.websites.cloud.clear_cache(job_id)
        self.store.event(f"Сбор сайта {job_id} завершён: {len(result['items'])} объявлений")
        if contact_id and self.startup is not None:
            # Cached searches can finish before the saved Telegram session reconnects.
            await asyncio.shield(self.startup)
        if contact_id and self.telegram.ready():
            try:
                for part in website_messages(summary):
                    await self.send(contact_id, part, automated=True)
                if artifact:
                    await self.send_file(contact_id, artifact, "Объявления · CSV", automated=True)
            except Exception:
                self.store.event(f"Результат сбора {job_id} сохранён в панели; доставка в Telegram не подтверждена", "error")
        elif contact_id:
            self.store.event(f"Результат сбора {job_id} сохранён в панели; Telegram ещё не подключён", "error")

    async def run_job(self, job_id, task, contact_id):
        await self.scoped_job(job_id,self._run_job(job_id,task,contact_id))

    async def _run_job(self, job_id, task, contact_id):
        started = time.time()
        self.store.execute("UPDATE jobs SET status='running',updated=? WHERE id=?", (time.time(), job_id))
        try:
            result = await self.hermes.run(task, contact_id)
        except Exception as error:
            self.store.execute("UPDATE jobs SET status='failed',result=?,updated=? WHERE id=?", (str(error)[:500] if isinstance(error, ValueError) else type(error).__name__, time.time(), job_id))
            return
        artifact, result = self.import_artifact(job_id, result, started)
        self.store.execute("UPDATE jobs SET status='completed',result=?,artifact=?,updated=? WHERE id=?", (result, artifact, time.time(), job_id))
        self.store.event(f"Hermes завершил задание {job_id}")
        if contact_id and self.telegram.ready():
            try:
                await self.send(contact_id, result[:4000] or "Задание завершено", automated=True)
                if artifact:
                    await self.send_file(contact_id, artifact, "Собранные данные · CSV", automated=True)
            except Exception:
                self.store.event(f"Результат Hermes {job_id} сохранён в панели; доставка в Telegram не подтверждена", "error")

    def import_artifact(self, job_id, result, started):
        workspace = self.hermes.workspace()
        match = re.search(re.escape(str(workspace)) + r"/[^\s`\"<>]*\.csv", result)
        if not match:
            return "", result
        path = Path(match[0])
        try:
            resolved = path.resolve()
            if path.is_symlink() or not resolved.is_relative_to(workspace.resolve()) or not path.is_file():
                raise ValueError("Недопустимый путь CSV")
            info = path.stat()
            if info.st_size > 3 * 1024 * 1024 or info.st_mtime < started - 10:
                raise ValueError("CSV слишком большой или относится к предыдущему заданию")
            content = path.read_text(encoding="utf-8-sig")
            rows = list(csv.reader(io.StringIO(content)))
            if not rows or len(rows) > 1001:
                raise ValueError("CSV не содержит допустимую таблицу")
            output = io.StringIO()
            writer = csv.writer(output)
            for row in rows:
                writer.writerow(["'" + value if value.lstrip().startswith(("=", "+", "-", "@")) else value for value in row])
            directory = self.store.directory / "media"
            directory.mkdir(exist_ok=True, mode=0o700)
            filename = f"hermes-{job_id}-{secrets.token_hex(4)}.csv"
            destination = directory / filename
            destination.write_text(output.getvalue(), encoding="utf-8-sig")
            destination.chmod(0o600)
            return filename, result.replace(match[0], "CSV доступен для скачивания в панели")
        except (OSError, ValueError, csv.Error, UnicodeError):
            self.store.event(f"CSV Hermes {job_id} не перенесён в медиатеку", "error")
            return "", result.replace(match[0], "CSV создан; перенос в панель не удался")

    async def send_file(self, contact_id, filename, caption, automated=True, before_dispatch=None):
        root = (self.store.directory / "media").resolve()
        if not isinstance(filename, str) or Path(filename).name != filename:
            raise ValueError("Выберите файл из медиатеки")
        path = (root / filename).resolve()
        if path.parent != root or not path.is_file() or (root / filename).is_symlink():
            raise ValueError("Файл не найден в медиатеке")
        if automated and filename not in {file["filename"] for file in self.media(contact_id)}:
            raise ValueError("Файл не разрешён для этого диалога")
        async with self.send_lock:
            self.require_active_dialog(contact_id)
            if not self.store.contact(contact_id)["enabled"] or (automated and not self.store.settings()["enabled"]):
                raise ValueError("Отправка отключена")
            self.rate_check(contact_id, time.time(), self.store.settings())
            if before_dispatch:
                before_dispatch()
            from .control import group_request
            request=group_request.get()
            topic=self.store.group_topic(contact_id,request['request']['telegram_id']) if request and request['request']['contact_id']==contact_id else 0
            thread={'reply_to':topic} if topic else {}
            if before_dispatch:
                message_id = await self.telegram.send_media(contact_id, path, caption[:1000], before_dispatch=before_dispatch,**thread)
            else:
                message_id = await self.telegram.send_media(contact_id, path, caption[:1000],**thread)
            self.store.message(contact_id, "assistant", f"[Файл: {filename}]\n{caption[:1000]}", message_id, "media")
        return {"sent": True, "telegram_id": message_id}

    def require_active_dialog(self, contact_id):
        from .control import group_request
        external=group_request.get()
        if external and external['request']['contact_id']==contact_id:
            external['guard']()
        scope = current_dialog.get()
        if scope and scope[0] == contact_id and not self.store.scope_active(scope):
            raise ValueError('Этот контекст уже перемещён в архив')
        if self.store.dialog_blocked(contact_id):
            raise ValueError('Собеседник заблокирован; сначала разблокируй его')

    def receive(self, contact_id, text, telegram_id, *, group_direct=False, **metadata):
        if self.store.dialog_blocked(contact_id, metadata.get('sender_id')):
            return False
        from .authority import authorized
        owner = (authorized(self.store, contact_id) and metadata.get("sender_id") == contact_id
                 and self.store.contact(contact_id)["kind"] == "private" and not metadata.get("forwarded"))
        if (not self.store.settings()["enabled"] or not self.store.contact(contact_id)["enabled"]) and not owner:
            return False
        message_id = self.store.message(contact_id, "user", text, telegram_id, **metadata)
        if not message_id:
            if metadata.get("image_path"):
                (self.store.directory / metadata["image_path"]).unlink(missing_ok=True)
            return False
        if self.store.contact(contact_id)['kind'] == 'group':
            if self.write_forbidden_until.get(contact_id, 0) > time.time() and not (
                    authorized(self.store, metadata.get('sender_id'), contact_id) and not metadata.get('forwarded')):
                return False
            scope = self.store.record_scope(self.store.rows('SELECT * FROM messages WHERE id=?',(message_id,))[0])
            key = (contact_id,metadata.get('sender_id'),self.store.group_topic(contact_id,telegram_id),scope[2])
            active = self.reply_states.get(contact_id)
            cancellable = (active and active.get('group_key') == key and not active['committed'] and not active['parts_sent']
                           and active['phase'] in {'reading','generation','typing'})
            old = self.group_pending.get(key)
            first = min(message_id,old['first_message_id']) if old and (not active or active.get('group_key') != key or cancellable) else message_id
            if cancellable:
                active['superseded'] = True
                first = min(first,active['first_message_id'])
            self.group_pending[key] = dict(telegram_id=telegram_id,first_message_id=first,
                                          direct=bool(group_direct or old and old.get('direct') and first == old['first_message_id']))
            if key in self.group_incoming:
                self.group_incoming[key]['updated'] = time.monotonic()
            else:
                self.group_incoming[key] = dict(updated=time.monotonic(),started=time.monotonic())
                self.spawn(self.flush_group_incoming(key))
            return True
        self.telegram.schedule_reply_typing(contact_id,telegram_id,owner=owner)
        active = self.reply_states.get(contact_id)
        cancellable = (active and self.store.settings()["revise_reply_on_new_message"]
                       and active["phase"] in {"reading", "generation", "typing"}
                       and (not active["committed"] or active["phase"] == "typing"))
        first_message_id = (active["first_message_id"] or message_id) if cancellable and not active["committed"] and not active["parts_sent"] else message_id
        batch = self.pending_batches.get(contact_id)
        if batch and (not active or batch["telegram_id"] != active["telegram_id"]):
            first_message_id = min(first_message_id,batch["first_message_id"])
        self.pending_batches[contact_id] = dict(telegram_id=telegram_id,first_message_id=first_message_id)
        if active and self.store.settings()["revise_reply_on_new_message"] and (not active["committed"] or active["phase"] == "typing"):
            active["superseded"] = True
        if contact_id in self.incoming:
            self.incoming[contact_id].update(telegram_id=telegram_id, updated=time.monotonic())
            self.incoming[contact_id]["first_message_id"] = min(self.incoming[contact_id]["first_message_id"],first_message_id)
            self.replace_reply_retry(contact_id)
            return True
        self.incoming[contact_id] = dict(telegram_id=telegram_id, updated=time.monotonic(), first_message_id=first_message_id)
        self.spawn(self.flush_incoming(contact_id))
        self.replace_reply_retry(contact_id)
        return True

    async def flush_group_incoming(self, key):
        item = self.group_incoming[key]
        try:
            while True:
                remaining = min(self.store.settings()['group_burst_window']-(time.monotonic()-item['updated']),
                                8-(time.monotonic()-item['started']))
                if remaining <= 0:
                    break
                await asyncio.sleep(remaining)
            self.group_incoming.pop(key,None)
            batch = self.group_pending.get(key)
            if batch:
                await self.reply(key[0],telegram_id=batch['telegram_id'],request_start=batch['first_message_id'],group_key=key)
                current = self.group_pending.get(key)
                if current and current['telegram_id'] == batch['telegram_id'] and key not in self.group_incoming:
                    self.group_pending.pop(key,None)
        finally:
            if self.group_incoming.get(key) is item:
                self.group_incoming.pop(key,None)

    async def read_new_message(self, contact_id, telegram_id):
        try:
            if self.store.settings()["read_receipts_enabled"]:
                await self.telegram.read(contact_id,telegram_id)
        except Exception:
            pass

    def replace_reply_retry(self, contact_id):
        active = self.reply_states.get(contact_id)
        if active and active["phase"] in {"actions","dispatch"}:
            return
        self.store.execute("DELETE FROM pending_replies WHERE contact_id=? AND status IN ('pending','running','failed')",(contact_id,))
        task = self.reply_retries.get(contact_id)
        if task and not task.done():
            task.cancel()

    def start_reply_retry(self, contact_id):
        previous = self.reply_retries.get(contact_id)
        if previous and not previous.done() and not previous.cancelling():
            return previous
        task = self.spawn(self.retry_reply_worker(contact_id))
        self.reply_retries[contact_id] = task
        return task

    def defer_reply(self, contact_id, telegram_id, first_message_id, error):
        rows = self.store.rows("SELECT attempts FROM pending_replies WHERE contact_id=? AND telegram_id=?",(contact_id,telegram_id))
        attempts = (rows[0]["attempts"] if rows else 0) + 1
        status = 'pending' if attempts < 5 else 'failed'
        due = time.time() + max(error.retry_after,min(120,error.retry_after * 2 ** min(attempts-1,3)))
        self.store.execute("INSERT INTO pending_replies(contact_id,telegram_id,first_message_id,due,attempts,status,error) VALUES (?,?,?,?,?,?,?) ON CONFLICT(contact_id) DO UPDATE SET telegram_id=excluded.telegram_id,first_message_id=excluded.first_message_id,due=excluded.due,attempts=excluded.attempts,status=excluded.status,error=excluded.error",
            (contact_id,telegram_id,first_message_id,due,attempts,status,str(error)[:240]))
        if status == 'pending':
            self.start_reply_retry(contact_id)
        return status == 'pending'

    async def retry_reply_worker(self, contact_id):
        held_presence = False
        try:
            while True:
                rows = self.store.rows("SELECT * FROM pending_replies WHERE contact_id=? AND status='pending'",(contact_id,))
                if not rows:
                    return
                row = rows[0]
                settings = self.store.settings()
                owner = controller_request(self.store,contact_id,row["telegram_id"])
                if (not settings["enabled"] or not self.store.contact(contact_id)["enabled"]) and not owner:
                    self.store.execute("UPDATE pending_replies SET status='failed',error='Ответы отключены' WHERE contact_id=?",(contact_id,))
                    return
                if not self.telegram.ready():
                    await asyncio.sleep(1)
                    continue
                if settings["online_during_reply"] and not held_presence:
                    held_presence = await self.telegram.begin_reply_presence(activate=False)
                await asyncio.sleep(max(0,row["due"]-time.time()))
                claimed = self.store.execute("UPDATE pending_replies SET status='running' WHERE contact_id=? AND telegram_id=? AND status='pending'",(contact_id,row["telegram_id"]))
                if not claimed.rowcount:
                    return
                if held_presence:
                    await self.telegram.presence(60)
                await self.read_new_message(contact_id,row["telegram_id"])
                await self.reply(contact_id,telegram_id=row["telegram_id"],request_start=row["first_message_id"],prepared=True,retrying=True)
                self.store.execute("UPDATE pending_replies SET status='failed',error='Обработка заменена новым сообщением' WHERE contact_id=? AND telegram_id=? AND status='running'",(contact_id,row["telegram_id"]))
        finally:
            if held_presence:
                try:
                    await self.telegram.end_reply_presence()
                except Exception:
                    pass
            if self.reply_retries.get(contact_id) is asyncio.current_task():
                self.reply_retries.pop(contact_id,None)

    async def retry_reply(self, contact_id, telegram_id):
        contact = self.store.contact(contact_id)
        if contact["kind"] != 'private' or contact_id == self.store.settings()["owner_id"] or not contact["enabled"]:
            raise ValueError("Повтор доступен для обычной личной переписки с включённым контактом")
        rows = self.store.rows("SELECT * FROM messages WHERE contact_id=? AND role='user' ORDER BY id DESC LIMIT 1",(contact_id,))
        if not rows or rows[0]["telegram_id"] != telegram_id or rows[0]["sender_id"] != contact_id:
            raise ValueError("Нужно последнее подлинное входящее сообщение контакта")
        original = rows[0]
        if not self.store.context_record_active(original):
            raise ValueError('Сообщение находится в архиве; нужно новое входящее сообщение')
        if self.store.rows("SELECT id FROM messages WHERE contact_id=? AND role='assistant' AND id>?",(contact_id,original["id"])):
            raise ValueError("На это сообщение уже отправлен ответ")
        pending = self.store.rows("SELECT * FROM pending_replies WHERE contact_id=?",(contact_id,))
        if pending and pending[0]["status"] in {'uncertain','dispatch'}:
            raise ValueError("Доставка предыдущего ответа не подтверждена; автоматически не повторяю")
        if not pending or pending[0]["status"] not in {'pending','running'}:
            self.store.execute("INSERT INTO pending_replies(contact_id,telegram_id,first_message_id,due) VALUES (?,?,?,?) ON CONFLICT(contact_id) DO UPDATE SET telegram_id=excluded.telegram_id,first_message_id=excluded.first_message_id,due=excluded.due,attempts=0,status='pending',error=''",
                (contact_id,telegram_id,original["id"],time.time()))
            self.pending_batches[contact_id] = dict(telegram_id=telegram_id,first_message_id=original["id"])
        self.start_reply_retry(contact_id)
        return {"contact_id":contact_id,"telegram_id":telegram_id,"status":"pending"}

    async def flush_incoming(self, contact_id):
        held_presence = False
        item = None
        try:
            if self.store.settings()["online_during_reply"]:
                try:
                    held_presence = await self.telegram.begin_reply_presence(activate=self.store.settings()["reply_online_immediate"])
                except Exception:
                    pass
            active = self.reply_states.get(contact_id)
            if (active and active["superseded"] and (not active["committed"] or active["phase"] == "typing") and active["phase"] in {"reading","generation","typing"}
                    and active["task"] is not asyncio.current_task()):
                active["task"].cancel()
            started = time.monotonic()
            settings = self.store.settings()
            read_pause = delay(settings, "read_delay") if settings["natural_enabled"] else 0
            while True:
                window = self.store.settings()["burst_window"]
                idle = time.monotonic() - self.incoming[contact_id]["updated"]
                remaining = max(min(window - idle, 8 - (time.monotonic() - started)), read_pause - idle)
                if remaining <= 0:
                    break
                await asyncio.sleep(remaining)
            item = self.incoming.pop(contact_id)
            owner = controller_request(self.store, contact_id, item["telegram_id"])
            if (not self.store.settings()["enabled"] or not self.store.contact(contact_id)["enabled"]) and not owner:
                self.pending_batches.pop(contact_id,None)
                return
            if held_presence:
                await self.telegram.presence(60)
            await self.read_new_message(contact_id,item["telegram_id"])
            await self.reply(contact_id, telegram_id=item["telegram_id"], request_start=item["first_message_id"], prepared=True)
        finally:
            if item and contact_id not in self.reply_retries:
                await self.telegram.stop_reply_typing(contact_id,item['telegram_id'])
            if held_presence:
                try:
                    await self.telegram.end_reply_presence()
                except Exception:
                    pass

    async def reply(self, contact_id, text=None, telegram_id=None, proactive=False, request_start=None, prepared=False, retrying=False, group_key=None):
        if proactive and self.store.contact(contact_id)['kind'] == 'group':
            return await self.group_initiative.consider(contact_id, requested=True)
        lock = self.contact_locks.setdefault(contact_id, asyncio.Lock())
        async with lock:
            settings = self.store.settings()
            group_batch = self.group_pending.get(group_key) if group_key else None
            if group_key:
                if not group_batch or group_batch['telegram_id'] != telegram_id:
                    return
                request_start = group_batch['first_message_id']
            batch = self.pending_batches.get(contact_id)
            if not proactive and settings["revise_reply_on_new_message"] and batch:
                if batch["telegram_id"] != telegram_id:
                    return
                request_start = batch["first_message_id"]
            contact = self.store.contact(contact_id)
            request = controller_request(self.store, contact_id, telegram_id) if not proactive and text is None else None
            source = self.store.rows('SELECT sender_id FROM messages WHERE contact_id=? AND telegram_id=?', (contact_id, telegram_id)) if telegram_id else []
            if self.store.dialog_blocked(contact_id, source[0]['sender_id'] if source else None):
                return
            owner = bool(request)
            if (not contact["enabled"] or not settings["enabled"]) and not owner:
                return
            if contact["kind"] == "group" and (not settings["group_enabled"] or (self.write_forbidden_until.get(contact_id, 0) > time.time() and not owner)):
                return
            if proactive and (not settings["proactive_enabled"] or not contact["proactive"]):
                return
            if text is not None and not self.store.message(contact_id, "user", text, telegram_id):
                return
            if telegram_id and not proactive and not group_key:
                self.telegram.schedule_reply_typing(contact_id,telegram_id,owner=owner)
            self.busy.add(contact_id)
            held_presence = False
            latest = self.store.rows("SELECT * FROM messages WHERE contact_id=? AND telegram_id=?",(contact_id,telegram_id)) if telegram_id else []
            state = dict(task=asyncio.current_task(), phase="reading", committed=False, superseded=False, parts_sent=0,telegram_id=telegram_id,
                         group_key=group_key,
                         deferred=False,
                         first_message_id=request_start or (latest[0]["id"] if latest else 0))
            self.reply_states[contact_id] = state
            scope = self.store.record_scope(latest[0]) if latest else self.store.dialog_scope(contact_id)
            state['dialog_scope'] = scope
            scope_token = current_dialog.set(scope)
            def before_dispatch():
                if state["superseded"] or not self.store.scope_active(scope):
                    raise asyncio.CancelledError()
                state["phase"] = "dispatch"
                self.store.execute("UPDATE pending_replies SET status='dispatch' WHERE contact_id=? AND telegram_id=?",(contact_id,telegram_id))
            try:
                if not self.store.scope_active(scope):
                    return
                if group_key:
                    if not await self.group_conversation.should_join(contact_id,group_key[1],telegram_id,
                            first_message_id=state['first_message_id'],direct=group_batch['direct']):
                        return
                    if state['superseded']:
                        raise asyncio.CancelledError()
                    self.telegram.schedule_reply_typing(contact_id,telegram_id,owner=owner)
                if telegram_id and not prepared:
                    if settings["online_during_reply"]:
                        held_presence = await self.telegram.begin_reply_presence(activate=settings["reply_online_immediate"])
                    if settings["natural_enabled"]:
                        await asyncio.sleep(delay(settings, "read_delay"))
                    if (not self.store.settings()["enabled"] or not self.store.contact(contact_id)["enabled"]) and not owner:
                        return
                    try:
                        if held_presence:
                            await self.telegram.presence(60)
                        if self.store.settings()["read_receipts_enabled"]:
                            await self.telegram.read(contact_id, telegram_id)
                        if settings["presence_enabled"]:
                            await self.telegram.presence(120)
                    except Exception:
                        pass
                async def execute(name, args):
                    if state['superseded']:
                        raise asyncio.CancelledError()
                    self.require_active_dialog(contact_id)
                    if name in OWNER_NAMES:
                        raise ValueError("Команды управления выполняются только через разбор текущего поручения владельца")
                    if name in {'find_stickers','find_gifs','find_photos','media_collection','inspect_telegram_media','learn_sticker_pack'}:
                        result=await self.action(contact_id,telegram_id,name,args)
                        if state['superseded']:
                            raise asyncio.CancelledError()
                        return result
                    previously_committed = state['committed']
                    state.update(phase="actions",committed=True)
                    self.store.execute("UPDATE pending_replies SET status='uncertain' WHERE contact_id=? AND telegram_id=?",(contact_id,telegram_id))
                    result = await self.action(contact_id, telegram_id, name, args)
                    if name == 'block_current_user' and isinstance(result, dict) and result.get('warning_required'):
                        # Preparing a warning has no external effect. Rendering failure
                        # can still be retried, and a newer reply can supersede it.
                        state.update(phase='generation', committed=previously_committed)
                        if not previously_committed:
                            self.store.execute("UPDATE pending_replies SET status='pending' WHERE contact_id=? AND telegram_id=?", (contact_id,telegram_id))
                    return result
                current_messages = self.store.rows(
                    "SELECT * FROM messages WHERE contact_id=? AND role='user' AND id>=? AND id<=? ORDER BY id",
                    (contact_id, state['first_message_id'], latest[0]['id'])) if latest and not proactive else []
                current_messages = [row for row in current_messages if (row['context_sender_id'],row['context_generation']) == scope[1:]]
                format_ = (self.store.reply_mode(contact_id, contact_id)
                           if proactive and contact['kind'] == 'private' and settings['voice_messages_enabled']
                           else reply_format(self.store, contact_id, current_messages))
                kwargs = {"current_from":state['first_message_id'] or None}
                if contact['kind'] == 'group' and latest:
                    kwargs['current_to'] = latest[0]['id']
                if format_ == 'voice':
                    kwargs['voice_reply'] = True
                if retrying:
                    kwargs['require_text'] = True
                actions = None
                action_request_ids = None
                continuation = None
                control_answer = ''
                state["phase"] = "generation"
                image_prompt = parse_generation_request('\n'.join(row['text'] for row in current_messages if not row['forwarded'])) if not proactive else None
                if owner and image_prompt is None:
                    new_messages = self.store.rows("SELECT text,telegram_id FROM messages WHERE contact_id=? AND sender_id=? AND role='user' AND forwarded=0 AND id>=? AND id<=? ORDER BY id",
                                                   (contact_id, contact_id, request_start or request["id"], request["id"]))
                    kwargs["owner_context"] = {"request": [row["text"] for row in new_messages], "request_message_id": request["id"],
                                               "photos": self.control.photos(request), "allow_tools": False,
                                               "actor_id": request["sender_id"], "is_owner": request["sender_id"] == settings["owner_id"]}
                    texts = kwargs["owner_context"]["request"]
                    pending=self.control.pending(request)
                    actions = self.control.parse(texts)
                    if actions and len(new_messages) > 1:
                        separate = [self.control.parse([row['text']]) or [] for row in new_messages]
                        if actions == [action for parsed in separate for action in parsed]:
                            action_request_ids = [row['telegram_id'] for row, parsed in zip(new_messages, separate)
                                                  for action in parsed]
                    if actions is None:
                        actions = await self.model.plan_owner(texts, await self.control.command_view(request,texts))
                    if actions and action_request_ids is None and len(new_messages)>1 and not getattr(actions,'resume',False):
                        action_request_ids=[]
                        for action in actions:
                            source=action['args'].get('source') if action['name']=='manage_publications' else None
                            candidates=[row for row in new_messages if source and source in row['text']]
                            if action['args'].get('join'):
                                candidates=[row for row in candidates if re.search(r'вступ|зайди|подпиш|присоедин',row['text'],re.I)]
                            action_request_ids.append((candidates[-1] if candidates else new_messages[-1])['telegram_id'])
                    if getattr(actions,'resume',False):
                        if not pending:raise ValueError('Уточняемое поручение больше недоступно')
                        continuation=pending
                    if getattr(actions,'question',''):
                        control_answer=actions.question
                        self.control.ask(request,control_answer,texts,continuation)
                    else:
                        self.control.clear_pending(contact_id)
                        control_answer=getattr(actions,'unsupported','')
                        if getattr(actions,'cancel',False):
                            control_answer='Незавершённое поручение отменено.' if pending else 'Хорошо, не выполняю.'
                if state["superseded"]:
                    raise asyncio.CancelledError()
                if actions:
                    state.update(phase="actions",committed=True)
                    self.store.execute("UPDATE pending_replies SET status='uncertain' WHERE contact_id=? AND telegram_id=?",(contact_id,telegram_id))
                    batch = self.pending_batches.get(contact_id)
                    if batch and batch["telegram_id"] == telegram_id:
                        self.pending_batches.pop(contact_id,None)
                gift_handled, gift_answer = False, ''
                if not actions and not owner and not proactive and contact['kind'] == 'private' and settings['gifts_enabled'] and contact['gift_allowed']:
                    gift_handled, gift_answer = await self.requested_gift_reply(contact_id, telegram_id, state)
                if control_answer:
                    answer=control_answer
                elif image_prompt is not None:
                    try:
                        await execute('generate_image', {'prompt': image_prompt})
                        answer = ''
                    except ValueError as error:
                        answer = 'Не удалось создать изображение: '+str(error)[:300]
                elif gift_handled:
                    answer = gift_answer
                elif actions:
                    answer = await self.control.execute_plan(contact_id, telegram_id, actions, request_ids=action_request_ids, continuation=continuation)
                else:
                    answer = await self.model.run(contact_id, execute, proactive, **kwargs)
                if answer:
                    pending = self.pending_batches.get(contact_id)
                    if self.store.settings()["revise_reply_on_new_message"] and pending and pending["telegram_id"] != telegram_id:
                        raise asyncio.CancelledError()
                    # A speed change applies to this answer immediately.
                    settings = self.store.settings()
                    if settings["online_during_reply"] and telegram_id and not held_presence and not self.telegram.reply_presence_count:
                        try:
                            held_presence = await self.telegram.begin_reply_presence()
                        except Exception:
                            pass
                    if settings["natural_enabled"]:
                        state["phase"] = "typing"
                        await asyncio.sleep(delay(settings, "reply_delay"))
                    if format_ == 'voice':
                        state['phase'] = 'typing'
                        await self.send_voice_reply(contact_id, telegram_id, answer, owner=owner, proactive=proactive, before_dispatch=before_dispatch)
                        state['parts_sent'] += 1
                    for index, part in enumerate([] if format_ == 'voice' else chunks(answer, settings["split_replies"] and contact['kind'] != 'group', settings["split_max_parts"], settings["split_max_chars"])):
                        if state["superseded"]:
                            raise asyncio.CancelledError()
                        state["phase"] = "typing"
                        if index:
                            await asyncio.sleep(delay(settings, "reply_part_delay"))
                        if owner:
                            await self.owner_reply(contact_id, telegram_id, part, before_dispatch=before_dispatch)
                        else:
                            await self.send(contact_id, part, proactive=proactive, automated=True, before_dispatch=before_dispatch,
                                            **({'reply_to':telegram_id,
                                                'context_reply_to':telegram_id} if contact['kind'] == 'group' and not proactive else {}))
                        state["parts_sent"] += 1
                if state.get('conversation_sticker'):
                    try:
                        await self.telegram_media.send_conversation_sticker(contact_id, before_dispatch,
                            source=latest[0] if latest else None)
                    except Exception as error:
                        self.store.event('Дополнительный стикер не отправлен: '+type(error).__name__, 'error')
                self.store.execute("DELETE FROM pending_replies WHERE contact_id=? AND telegram_id=?",(contact_id,telegram_id))
            except ModelUnavailable as error:
                if telegram_id and not proactive and not state["committed"] and not state["parts_sent"] and not state["superseded"]:
                    state["deferred"] = self.defer_reply(contact_id,telegram_id,state["first_message_id"],error)
                else:
                    self.store.execute("UPDATE pending_replies SET status='uncertain',error=? WHERE contact_id=? AND telegram_id=?",(str(error)[:240],contact_id,telegram_id))
                self.store.event(f"Диалог с {contact['name']}: {str(error)[:240]}", "error")
            except Exception as error:
                if contact['kind'] == 'group' and isinstance(error, (errors.ChatWriteForbiddenError, errors.UserBannedInChannelError, errors.ChannelPrivateError)):
                    self.write_forbidden_until[contact_id] = time.time() + 1800
                message = str(error) if isinstance(error, ValueError) else type(error).__name__
                self.store.event(f"Диалог с {contact['name']}: {message[:240]}", "error")
                self.store.execute("UPDATE pending_replies SET status=CASE WHEN status='dispatch' THEN 'uncertain' ELSE 'failed' END,error=? WHERE contact_id=? AND telegram_id=?",(message[:240],contact_id,telegram_id))
                if owner and isinstance(error, ValueError):
                    try:
                        await self.owner_reply(contact_id, telegram_id, "Не удалось выполнить: " + message[:300])
                    except Exception:
                        pass
            finally:
                if telegram_id and not proactive:
                    await self.telegram.stop_reply_typing(contact_id,telegram_id)
                if held_presence:
                    try:
                        await self.telegram.end_reply_presence()
                    except Exception:
                        pass
                current_dialog.reset(scope_token)
                self.busy.discard(contact_id)
                if self.reply_states.get(contact_id) is state:
                    self.reply_states.pop(contact_id,None)
                if group_key:
                    pending_group = self.group_pending.get(group_key)
                    if pending_group and pending_group['telegram_id'] == telegram_id and not state['superseded']:
                        self.group_pending.pop(group_key,None)
                batch = self.pending_batches.get(contact_id)
                if batch and batch["telegram_id"] == telegram_id and not state["superseded"] and not state["deferred"]:
                    self.pending_batches.pop(contact_id,None)
                if proactive:
                    self.store.execute("UPDATE contacts SET next_proactive=? WHERE id=?",
                        (time.time() + settings["proactive_hours"] * 3600 * delay(settings, "proactive_variation"), contact_id))

    def rate_check(self, contact_id, now, settings):
        if self.flood_until > now:
            raise Deferred("Telegram попросил сделать паузу", self.flood_until)
        if settings["hourly_limit"]:
            rows = self.store.rows("SELECT created FROM messages WHERE role='assistant' AND kind!='preview' AND created>? ORDER BY created", (now - 3600,))
            if len(rows) >= settings["hourly_limit"]:
                raise Deferred("Часовой лимит сообщений", rows[0]["created"] + 3601)
        if settings["daily_contact_limit"]:
            today = datetime.fromtimestamp(now, ZoneInfo(settings["timezone"])).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
            total = self.store.rows("SELECT COUNT(*) AS n FROM messages WHERE contact_id=? AND role='assistant' AND created>=?", (contact_id, today))[0]["n"]
            if total >= settings["daily_contact_limit"]:
                tomorrow = datetime.fromtimestamp(today, ZoneInfo(settings["timezone"])) + timedelta(days=1)
                raise Deferred("Дневной лимит для контакта", tomorrow.timestamp())

    async def send(self, contact_id, text, proactive=False, automated=False, before_dispatch=None, *, reply_to=None, context_reply_to=None):
        async with self.send_lock:
            self.require_active_dialog(contact_id)
            contact = self.store.contact(contact_id)
            if not contact["enabled"]:
                raise ValueError("Ответы этому контакту отключены")
            if proactive and not contact["proactive"]:
                raise ValueError("Для этого контакта отключены самостоятельные сообщения")
            settings = self.store.settings()
            if automated and not settings["enabled"]:
                raise ValueError("Дарья на паузе")
            if automated and contact["kind"] == "group" and not settings["group_enabled"]:
                raise ValueError("Ответы в группах выключены")
            now = time.time()
            if proactive and allowed_time(now, settings) > now:
                raise Deferred("Тихие часы", allowed_time(now, settings))
            self.rate_check(contact_id, now, settings)
            previous = self.store.rows("SELECT MAX(created) AS t FROM messages WHERE contact_id=? AND role='assistant'", (contact_id,))[0]["t"]
            if previous:
                await asyncio.sleep(max(0, min(settings["min_interval"], previous + settings["min_interval"] - now)))
            # Check again immediately before sending, because settings can change during the delay.
            current = self.store.contact(contact_id)
            if not current["enabled"] or (proactive and not current["proactive"]):
                raise ValueError("Отправка отключена для контакта")
            if automated and not self.store.settings()["enabled"]:
                raise ValueError("Дарья на паузе")
            try:
                self.require_active_dialog(contact_id)
                telegram_id = await self.telegram.send(contact_id, text, **({"before_dispatch":before_dispatch} if before_dispatch else {}),
                                                        **({'reply_to': reply_to} if reply_to is not None else {}))
            except errors.FloodWaitError as error:
                self.flood_until = time.time() + error.seconds
                raise Deferred(f"Telegram просит подождать {error.seconds} секунд", self.flood_until) from None
            self.store.message(contact_id, "assistant", text, telegram_id, "proactive" if proactive else "chat",
                               reply_to_telegram_id=context_reply_to if context_reply_to is not None else reply_to)
            self.contact_blocks.confirm_warning(contact_id, telegram_id, text)
            self.store.event(f"Сообщение отправлено: {contact['name']}")
            return telegram_id

    async def send_voice_reply(self, contact_id, telegram_id, text, *, owner=False, proactive=False, before_dispatch=None, manual=False):
        settings = self.store.settings()
        self.calls.voice.speaker = settings['voice_speaker']
        spoken = spoken_text(text, settings['voice_message_max_chars'])
        try:
            async with voice_note(self.calls.voice, self.store.directory, spoken,
                                  settings['voice_message_speed'], settings['voice_message_volume']) as (path, duration):
                async with self.send_lock:
                    self.require_active_dialog(contact_id)
                    current = self.store.contact(contact_id)
                    if owner:
                        self.control.require(contact_id, telegram_id, True)
                    elif not current['enabled'] or not manual and not self.store.settings()['enabled']:
                        raise ValueError('Отправка отключена для контакта')
                    if current['kind'] == 'group' and not self.store.settings()['group_enabled']:
                        raise ValueError('Ответы в группах выключены')
                    now = time.time()
                    if proactive:
                        if not current['proactive'] or not self.store.settings()['proactive_enabled']:
                            raise ValueError('Самостоятельные сообщения этому контакту выключены')
                        if allowed_time(now, self.store.settings()) > now:
                            raise Deferred('Тихие часы', allowed_time(now, self.store.settings()))
                    self.rate_check(contact_id, now, self.store.settings())
                    previous = self.store.rows("SELECT MAX(created) AS t FROM messages WHERE contact_id=? AND role='assistant'", (contact_id,))[0]['t']
                    if previous:
                        await asyncio.sleep(max(0, min(settings['min_interval'], previous + settings['min_interval'] - now)))
                    def guard():
                        if proactive and (not self.store.contact(contact_id)['proactive'] or not self.store.settings()['proactive_enabled']):
                            raise ValueError('Самостоятельные сообщения выключены')
                        if self.store.contact(contact_id)['kind'] == 'group' and not self.store.settings()['group_enabled']:
                            raise ValueError('Ответы в группах выключены')
                        if owner:
                            self.control.require(contact_id, telegram_id, True)
                        if before_dispatch:
                            before_dispatch()
                    try:
                        message_id = await self.telegram.send_voice(contact_id, path, duration,
                            manual=owner, reply_to=telegram_id, before_dispatch=guard)
                    except errors.FloodWaitError as error:
                        self.flood_until = time.time() + error.seconds
                        raise Deferred(f'Telegram просит подождать {error.seconds} секунд', self.flood_until) from None
                    self.store.message(contact_id, 'assistant', spoken, message_id, 'voice', reply_to_telegram_id=telegram_id)
                    self.contact_blocks.confirm_warning(contact_id, message_id, spoken)
                    self.store.event('Голосовое отправлено: ' + current['name'])
                    return message_id
        except VoiceRenderUnavailable as error:
            raise ModelUnavailable(str(error)) from None
        finally:
            await self.calls.release_idle_voice()

    async def owner_reply(self, contact_id, telegram_id, text, before_dispatch=None):
        async with self.send_lock:
            self.control.require(contact_id, telegram_id, True)
            self.rate_check(contact_id, time.time(), self.store.settings())
            def check():
                self.control.require(contact_id, telegram_id, True)
                if before_dispatch:
                    before_dispatch()
            message_id = await self.telegram.send(contact_id, text, manual=True, before_dispatch=check)
            self.store.message(contact_id, "assistant", text, message_id)

    def current_gift_request(self, contact_id, telegram_id):
        self.require_active_dialog(contact_id)
        rows = self.store.rows("SELECT * FROM messages WHERE contact_id=? AND telegram_id=? AND role='user'", (contact_id, telegram_id))
        if not rows:
            raise ValueError('Нужна новая просьба о подарке; старые просьбы автоматически не выполняются')
        request = rows[0]
        author = self.store.dialog_scope(contact_id)[1] if self.store.contact(contact_id)['kind'] == 'group' else contact_id
        if request['forwarded'] or request['sender_id'] != author or not self.store.context_record_active(request) or not 0 <= time.time() - request['created'] <= 300:
            raise ValueError('Нужна текущая подлинная просьба собеседника о подарке')
        if request['created'] <= self.store.settings()['gift_requests_after']:
            raise ValueError('Подарки включены после этого сообщения; нужна новая просьба')
        newer = self.store.context_rows('messages', contact_id, limit=50)
        if any(row['role'] == 'user' and row['id'] > request['id'] for row in newer):
            raise ValueError('Пришло новое сообщение; прежняя просьба о подарке должна быть проверена заново')
        return request

    def gift_request_data(self, contact_id, request, start=None):
        start = start or self.reply_states.get(contact_id, {}).get('first_message_id') or request['id']
        rows = self.store.context_history(contact_id, 24)
        cutoff = self.store.settings()['gift_requests_after']
        texts = [row['text'] for row in rows if start <= row['id'] <= request['id'] and row['role'] == 'user'
                 and row['sender_id'] == request['sender_id'] and not row['forwarded']
                 and row['created'] > cutoff and 0 <= time.time() - row['created'] <= 300]
        cities = self.store.location_cities(contact_id)
        history = [{'role': row['role'], 'text': redact_locations(row['text'][:1000], cities)}
                   for row in rows if row['id'] < start][-12:]
        return texts, history

    async def requested_gift_reply(self, contact_id, telegram_id, state):
        try:
            request = self.current_gift_request(contact_id, telegram_id)
        except ValueError as error:
            self.store.event(f'Проверка подарка {contact_id}/{telegram_id}: {error}')
            return False, ''
        texts, history = self.gift_request_data(contact_id, request, state.get('first_message_id'))
        try:
            requested = await self.model.gift_requested(texts, history)
        except ModelUnavailable:
            raise
        except ValueError as error:
            # An invalid classifier response is not a negative intent decision.
            # Keep the reply retryable instead of letting old dialogue invent a refusal.
            raise ModelUnavailable(str(error)) from None
        self.store.event(f'Проверка подарка {contact_id}/{telegram_id}: ' +
                         ('новая просьба подтверждена' if requested else 'новой просьбы нет'))
        if not requested:
            return False, ''
        self.current_gift_request(contact_id, telegram_id)
        if state.get('superseded'):
            raise asyncio.CancelledError()
        previous_commit = state.get('committed', False)
        state.update(phase='actions', committed=True)
        self.store.execute("UPDATE pending_replies SET status='uncertain' WHERE contact_id=? AND telegram_id=?", (contact_id, telegram_id))
        try:
            result = await self.gifts.send_requested(contact_id, '\n'.join(texts), self.model,
                                                     lambda: self.current_gift_request(contact_id, telegram_id))
        except ModelUnavailable:
            # Gift selection happens before reservation or any payment request.
            state.update(phase='generation', committed=previous_commit)
            if not previous_commit:
                self.store.execute("UPDATE pending_replies SET status='pending' WHERE contact_id=? AND telegram_id=?", (contact_id, telegram_id))
            raise
        except ValueError as error:
            return True, 'Подарок не отправлен: ' + str(error)
        return True, f"Отправила настоящий Telegram-подарок ({result['stars']} Stars)."

    async def action(self, contact_id, telegram_id, name, args, *, owner_command=False, actor_id=None):
        if not isinstance(args, dict):
            raise ValueError("Параметры действия должны быть объектом")
        if name in OWNER_NAMES:
            return await self.control.action(contact_id, telegram_id, name, args, trusted=owner_command)
        if name == 'block_current_user':
            if set(args) != {'reason'} or not isinstance(args['reason'], str):
                raise ValueError('Укажи только причину; адресата определяет сервер')
            return await self.contact_blocks.block(contact_id, telegram_id, args['reason'])
        self.require_active_dialog(contact_id)
        if name == 'generate_image':
            def image_guard():
                self.require_active_dialog(contact_id)
                state = self.reply_states.get(contact_id)
                if state and state.get('superseded'):
                    raise ValueError('Сообщение уже заменено новым')
                if not self.store.contact(contact_id)['enabled'] or not self.store.settings()['enabled']:
                    raise ValueError('Общение выключено')
            sources = self.store.rows("SELECT * FROM messages WHERE contact_id=? AND telegram_id=? AND role='user' ORDER BY id DESC LIMIT 1",
                                      (contact_id, telegram_id)) if telegram_id else []
            return await self.image_generator.execute(contact_id, args, image_guard, source=sources[0] if sources else None)
        from .research import RESEARCH_NAMES
        if name in RESEARCH_NAMES:
            def research_guard():
                self.require_active_dialog(contact_id)
                if not self.store.settings()['enabled'] or not self.store.contact(contact_id)['enabled']:
                    raise ValueError('Даша на паузе')
            return await self.research.execute(name,args,research_guard)
        from .telegram_media import MEDIA_TOOLS
        if name in MEDIA_TOOLS:
            def media_guard():
                self.require_active_dialog(contact_id)
                state=self.reply_states.get(contact_id)
                if state and state.get('superseded'):
                    raise ValueError('Сообщение уже заменено новым')
                if not self.store.contact(contact_id)['enabled'] or not self.store.settings()['enabled']:
                    raise ValueError('Общение выключено')
            sources=self.store.rows('SELECT * FROM messages WHERE contact_id=? AND telegram_id=? AND role=\'user\' ORDER BY id DESC LIMIT 1',
                                    (contact_id,telegram_id)) if telegram_id else []
            return await self.telegram_media.execute(contact_id,name,args,media_guard,source=sources[0] if sources else None)
        if name == 'view_profile':
            try:
                return await self.account.current_profile(contact_id, args)
            except errors.FloodWaitError as error:
                self.flood_until = time.time() + error.seconds
                raise ValueError(f'Telegram просит подождать {error.seconds} секунд') from None
            except errors.RPCError:
                raise ValueError('Telegram не предоставил этот профиль') from None
            except asyncio.TimeoutError:
                raise ValueError('Telegram временно не ответил на запрос профиля') from None
        if name == "remember":
            if not self.store.settings()["memory_enabled"]:
                raise ValueError("Сохранение фактов выключено владельцем")
            key, value = args["key"], args["value"]
            if not isinstance(key, str) or not isinstance(value, str) or not 1 <= len(key) <= 80 or len(value) > 1000:
                raise ValueError("Слишком длинное воспоминание")
            if has_location(key + ' ' + value, self.store.location_cities(contact_id)):
                return {'saved':False,'reason':'Города и геоданные не записываются в память'}
            try:
                if await self.model.memory_has_location(key, value):
                    return {'saved':False,'reason':'Города и геоданные не записываются в память'}
            except ValueError:
                return {'saved':False,'reason':'Приватность факта не подтверждена; он не сохранён'}
            self.require_active_dialog(contact_id)
            self.store.remember(contact_id, key, value)
            return {"saved": True}
        if name == "search_history":
            query = str(args["query"])[:200]
            cities = self.store.location_cities(contact_id)
            if has_location(query, cities):
                return []
            return [{key:row[key] for key in ['role','text','created']} | {'text':redact_locations(row['text'],cities)} for row in reversed(self.store.context_history(contact_id,500)) if query.casefold() in row['text'].casefold()][:20]
        if name in {"read_website", "parse_website"}:
            settings = self.store.settings()
            if (actor_id or contact_id) != settings["owner_id"] and not self.store.contact(contact_id)["hermes_allowed"]:
                raise ValueError("Просмотр сайтов доступен собеседникам с разрешением Hermes")
            result = await self.parse_website(str(args["url"]), max_items=args.get("max_items", 20), pages=args.get("pages", 1), contact_id=contact_id)
            if result.get("job_id"):
                return result
            if result["blocked"] and settings["hermes_enabled"]:
                result["hermes_job"] = await self.submit_job(hermes_request(result["url"], args.get("max_items", 20)), contact_id)
            result["text"] = result["text"][:10000]
            return result
        if name == "list_gifts":
            return await self.gifts.inspect(contact_id)
        if name == "send_gift":
            requested = False
            guard = None
            if not self.store.settings()['gift_autonomous_enabled']:
                reason = self.gifts.policy_reason(contact_id)
                if reason:
                    raise ValueError(reason)
                guard = lambda: self.current_gift_request(contact_id, telegram_id)
                request = guard()
                texts, history = self.gift_request_data(contact_id, request)
                requested = await self.model.gift_requested(texts, history)
                if not requested:
                    raise ValueError('Подарки разрешены только по просьбе; в текущем сообщении просьба о покупке не подтверждена')
                guard()
            return await self.gifts.send(contact_id, str(args["gift_id"]), str(args["message"]), automatic=True,
                                         requested=requested, guard=guard)
        if name == "delegate_task":
            return await self.submit_job(args["task"], contact_id,actor_id=actor_id)
        if name == "list_media":
            return self.media(contact_id)
        if name == "send_media":
            return await self.send_file(contact_id, args["filename"], str(args["caption"]))
        if name == "edit_last_reply":
            text = str(args["text"])
            if not 1 <= len(text) <= 4000:
                raise ValueError("Недопустимая длина сообщения")
            rows = [row for row in reversed(self.store.context_history(contact_id,50)) if row['role']=='assistant' and row['telegram_id'] is not None][:1]
            if not rows:
                raise ValueError("Нет отправленного сообщения для исправления")
            await self.telegram.edit(contact_id, rows[0]["telegram_id"], text)
            self.store.execute("UPDATE messages SET text=? WHERE id=?", (text, rows[0]["id"]))
            return {"edited": True}
        if name == "forget":
            self.store.forget_memory(contact_id, args["key"])
            self.notebook.clear(contact_id,self.store.group_topic(contact_id,telegram_id))
            return {"forgotten": True}
        if name == 'forget_locations':
            if args:
                raise ValueError('Удаление геоданных относится только к текущему собеседнику')
            return {'forgotten':True,'removed':self.store.purge_location_memories(contact_id),'location_memory_enabled':False}
        if name == "schedule_message":
            manual_owner=actor_id if actor_id==self.store.settings()['owner_id'] else 0
            if not self.store.contact(contact_id)["proactive"] and not manual_owner:
                raise ValueError("В панели пока не разрешены самостоятельные сообщения этому контакту")
            text = args["text"]
            if not isinstance(text, str) or not 1 <= len(text) <= 4000:
                raise ValueError("Сообщение должно содержать 1–4000 символов")
            pending = len([row for row in self.store.context_rows('tasks',contact_id,limit=1000) if row['status']=='pending'])
            if pending >= 30:
                raise ValueError("Для контакта уже запланировано 30 сообщений")
            return {"task_id": self.store.schedule(contact_id, text, due_time(args["when"]),topic_id=self.store.group_topic(contact_id,telegram_id),manual_owner_id=manual_owner)}
        if name == "list_reminders":
            return [{key:row[key] for key in ['id','text','due']} for row in self.store.context_rows('tasks',contact_id,order='due ASC') if row['status']=='pending'][:30]
        if name == "cancel_reminder":
            changed = self.store.execute("UPDATE tasks SET status='cancelled' WHERE id=? AND contact_id=? AND context_sender_id=? AND context_generation=? AND status='pending'", (args["task_id"],*self.store.dialog_scope(contact_id))).rowcount
            return {"cancelled": bool(changed)}
        if name == "react":
            if not self.store.settings()["enabled"] or not self.store.contact(contact_id)["enabled"]:
                raise ValueError("Дарья на паузе")
            if not self.store.settings()["reactions_enabled"]:
                raise ValueError("Реакции выключены в панели")
            if args["emoji"] not in REACTIONS:
                raise ValueError("Неизвестная реакция")
            if self.store.settings()["natural_enabled"]:
                await asyncio.sleep(delay(self.store.settings(), "reaction_delay"))
            if not self.store.settings()["enabled"] or not self.store.contact(contact_id)["enabled"]:
                raise ValueError("Дарья на паузе")
            await self.telegram.react(contact_id, telegram_id, args["emoji"])
            return {"reacted": True}
        if name == "stop_proactive":
            self.store.execute("UPDATE contacts SET proactive=0 WHERE id=?", (contact_id,))
            self.store.execute("UPDATE tasks SET status='cancelled' WHERE contact_id=? AND status='pending'", (contact_id,))
            return {"proactive": False}
        raise ValueError("Неизвестное действие")

    async def tick(self):
        self.group_admin.join_requests.tick()
        self.group_admin.deleted_accounts.tick()
        settings = self.store.settings()
        if not settings["enabled"] or not self.telegram.ready() or not self.model.key():
            return
        now = time.time()
        for task in self.store.rows("SELECT * FROM tasks WHERE status='pending' AND due<=? ORDER BY due LIMIT 10", (now,)):
            if not self.store.context_record_active(task):
                self.store.execute("UPDATE tasks SET status='archived' WHERE id=?",(task['id'],))
                continue
            contact = self.store.contact(task["contact_id"])
            manual=task.get('manual_owner_id',0)
            if manual and manual!=settings['owner_id']:
                self.store.execute("UPDATE tasks SET status='cancelled',error='Владелец поручения изменился' WHERE id=?",(task['id'],))
                continue
            if not contact["enabled"] or (not contact["proactive"] and not manual):
                self.store.execute("UPDATE tasks SET status='cancelled',error='Самостоятельные сообщения отключены' WHERE id=?", (task["id"],))
                continue
            allowed = allowed_time(now, settings)
            if allowed > now:
                self.store.execute("UPDATE tasks SET due=? WHERE id=?", (allowed, task["id"]))
                continue
            if not self.store.claim_task(task["id"]):
                continue
            token = current_dialog.set(self.store.record_scope(task))
            try:
                await self.send(task["contact_id"], task["text"], proactive=True, automated=True,
                    **({'reply_to':task['topic_id']} if task.get('topic_id') else {}))
                self.store.execute("UPDATE tasks SET status='sent' WHERE id=?", (task["id"],))
                if task["repeat_hours"]:
                    due = time.time() + task["repeat_hours"] * 3600
                    next_id = self.store.schedule(task["contact_id"], task["text"], due,topic_id=task.get('topic_id',0),manual_owner_id=manual)
                    self.store.execute("UPDATE tasks SET repeat_hours=? WHERE id=?", (task["repeat_hours"], next_id))
            except Deferred as error:
                self.store.execute("UPDATE tasks SET status='pending',due=?,error=? WHERE id=?", (error.until, str(error), task["id"]))
            except Exception as error:
                self.store.execute("UPDATE tasks SET status='uncertain',error=? WHERE id=?", (type(error).__name__ + ': проверьте доставку вручную', task["id"]))
                self.store.event("Не удалось подтвердить доставку напоминания", "error")
            finally:
                current_dialog.reset(token)
        if allowed_time(now, settings) > now or self.flood_until > now:
            return
        self.publications.tick()
        self.private_initiative.tick()
        self.telegram_autonomy.tick()
        await self.gifts.tick(self.model)
        await self.notebook.tick(now)
        if not settings["proactive_enabled"]:
            return
        await self.group_initiative.tick(now)
        if settings['private_initiative_enabled']:
            return
        cutoff = now - settings["proactive_hours"] * 3600
        for contact in self.store.rows("SELECT * FROM contacts WHERE kind='private' AND enabled=1 AND proactive=1 AND last_activity<=? AND next_proactive<=?", (cutoff, now)):
            if contact["id"] in self.busy:
                continue
            try:
                self.rate_check(contact["id"], now, settings)
            except Deferred:
                break
            await self.reply(contact["id"], proactive=True)

    async def loop(self):
        while True:
            try:
                await self.reconnect()
                settings = self.store.settings()
                await self.telegram.periodic_presence(bool(settings["enabled"] and
                     settings["presence_enabled"] and allowed_time(time.time(), settings) <= time.time()))
                if time.time() >= self.next_catalog:
                    self.next_catalog = time.time() + 1800
                    await self.model.refresh_catalog()
                await self.tick()
            except Exception as error:
                self.store.event("Планировщик: " + type(error).__name__, "error")
            await asyncio.sleep(20)

    async def reconnect(self):
        if self.telegram.state in {"code", "password", "qr"} or self.telegram.ready():
            return
        if self.startup and not self.startup.done():
            return
        if not (self.store.directory / "account.session").exists() or time.time() < self.next_reconnect:
            return
        self.next_reconnect = time.time() + 60
        await self.start_telegram()
