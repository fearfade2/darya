"""Reusable Telegram stickers and GIFs, with real document references."""
from __future__ import annotations

import asyncio
import base64
import io
import json
import random
import re
import time
from pathlib import Path

from telethon import functions, types, utils
from telethon.extensions import BinaryReader

from .session_guard import SECURITY_PEER, security_message
from .privacy import redact_locations


MEDIA_TOOLS = frozenset({'find_stickers', 'find_gifs', 'find_photos', 'send_sticker', 'send_gif', 'send_photo',
                        'media_collection', 'save_telegram_media','inspect_telegram_media','learn_sticker_pack'})


def parse_media_command(text):
    """Only standalone requests; longer conversational intentions use the planner."""
    text = text.strip().rstrip('.!? ')
    noun = r'(?:этот\s+|эту\s+)?(стикер(?:ок)?|гиф(?:ку|ка|ки)?|gif|фото|фотку|картинку)'
    verb = r'(скинь|кинь|пришли|отправь|покажи|найди|сохрани|сохраняй)'
    if re.search(r'\bне\s+'+verb, text, re.I):
        return None
    match = re.fullmatch(verb+r'\s+'+noun+r'(?:\s+(.+))?', text, re.I)
    if match:
        operation, media, query = match.groups()
    else:
        match = re.fullmatch(noun+r'\s+'+verb+r'(?:\s+(.+))?', text, re.I)
        if not match:
            tail = re.fullmatch(noun+r'\s+(.+)\s+'+verb, text, re.I)
            if tail:
                media, query, operation = tail.groups()
            else:
                if text.casefold() in {'сохрани', 'сохрани это'}:
                    return {'action':'save_media'}
                return None
        else:
            media, operation, query = match.groups()
    kind = 'sticker' if media.casefold().startswith('стикер') else 'gif' if media.casefold().startswith(('гиф','gif')) else 'photo'
    query = re.sub(r'\s*(?:пожалуйста|пж|плиз)[,\s]*$', '', query or '', flags=re.I).strip()
    if query.casefold() in {'мне','себе','у себя','в избранное','в сохранённые','в сохраненные','из ответа','из reply'}:
        query = ''
    if re.search(r'@[A-Za-z]|\b(?:ему|ей|им|туда)\b|\bв\s+(?:чат|групп|канал)|\b[0-9]{8,20}\b',query,re.I):
        return None
    query = re.sub(r'^(?:с\s+|про\s+|по\s+теме\s+)', '', query or '', flags=re.I).strip()
    if query.casefold() in {'по смыслу','по настроению','по контексту','что-нибудь','любую','любое','любую по смыслу'}:
        return None
    if len(query)>100:
        return None
    if operation.casefold().startswith('сохран'):
        if kind == 'photo':
            return None
        return {'action':'save_media','kind':kind,**({'query':query} if query else {})}
    if kind == 'photo' and not query:
        return None
    action = ('find_' if operation.casefold()=='найди' else 'send_')+(
        {'sticker':'stickers','gif':'gifs','photo':'photos'}[kind] if operation.casefold()=='найди' else kind)
    return {'action':action,**({'query':query} if query else {})}


def media_tools(tool):
    lookup = {'query': {'type': 'string', 'maxLength': 100},
              'limit': {'type': 'integer', 'minimum': 1, 'maximum': 20}}
    return [
        tool('find_stickers', 'Найти настоящие стикеры: своя коллекция, избранное, недавние и поиск наборов Telegram. query — эмоция, эмодзи или название. Выбери asset_id из результата; ничего не угадывай.', lookup, []),
        tool('find_gifs', 'Найти GIF по настроению или ситуации: своя коллекция, сохранённые GIF и поиск через Telegram @gif. Выбирай asset_id из результата. Если GIF лучше передаёт смысл, можно отправить её самой.', lookup, []),
        tool('find_photos', 'Найти фото по смыслу через Telegram @pic (Yandex Image Search). query — конкретный предмет или сюжет, без личных данных переписки. Выбирай настоящий asset_id из результата. Это поиск изображений, а не генерация.', lookup, ['query']),
        tool('media_collection', 'Посмотреть стикеры и GIF, собранные из доступных сообщений других людей. Сохраняется само медиа, без текста личной переписки и имени отправителя.',
             lookup | {'kind': {'type': 'string', 'enum': ['sticker', 'gif', 'all']}}, []),
        tool('send_sticker', 'Отправить текущему собеседнику настоящий стикер по asset_id, полученному из поиска или коллекции. Можно самостоятельно выбрать уместный стикер; не отправляй несколько подряд и не скрывай результат важного действия.',
             {'asset_id': {'type': 'string', 'maxLength': 180}, 'query': lookup['query']}, []),
        tool('send_gif', 'Отправить текущему собеседнику настоящую GIF по asset_id из поиска или коллекции. Можно самой выбрать уместную анимацию. Не подменяй ею ответ на важный вопрос или результат выполненной команды.',
             {'asset_id': {'type': 'string', 'maxLength': 180}, 'query': lookup['query']}, []),
        tool('send_photo', 'Отправить одно фото через @pic по asset_id из find_photos либо конкретному query по смыслу разговора. Не выдавай найденное фото за собственное или за изображение собеседника.',
             {'asset_id': {'type': 'string', 'maxLength': 180}, 'query': lookup['query']}, []),
        tool('save_telegram_media', 'Добавить обнаруженный стикер в избранное Telegram или GIF в сохранённые. asset_id берётся из своей коллекции или поиска; не скачивает чужие фото и обычные видео.',
             {'asset_id': {'type': 'string', 'maxLength': 180}, 'remove': {'type': 'boolean'}, 'kind': {'type':'string','enum':['all','sticker','gif']}, 'query': lookup['query']}, []),
        tool('inspect_telegram_media','Посмотреть, что изображено и происходит в стикере или GIF из коллекции. Используй перед отправкой незнакомой GIF без описания. Источник — только само медиа, без подписи или личного разговора.',
             {'asset_id':{'type':'string','maxLength':180}},['asset_id']),
        tool('learn_sticker_pack','Взять остальные стикеры из набора стикера, найденного в доступной переписке или поиске. Сначала выбери настоящий asset_id стикера. Сохраняет набор в своей коллекции, не подписывает и не покупает ничего.',
             {'asset_id':{'type':'string','maxLength':180}},['asset_id']),
    ]


class TelegramMedia:
    # Explicit requests keep the full search and are independent of this cadence.
    CONVERSATION_STICKER_GAP = 2
    CONVERSATION_STICKER_SECONDS = 45
    CONVERSATION_STICKER_DELAY_MIN = 2
    CONVERSATION_STICKER_DELAY_MAX = 5

    def __init__(self, runtime):
        self.runtime, self.store, self.telegram = runtime, runtime.store, runtime.telegram
        self.inline = {}
        self.search_lock = asyncio.Lock()

    @staticmethod
    def kind(document):
        if not isinstance(document, types.Document):
            return None
        attributes = document.attributes or []
        if any(isinstance(a, types.DocumentAttributeSticker) for a in attributes):
            return 'sticker'
        if any(isinstance(a, types.DocumentAttributeAnimated) for a in attributes) or document.mime_type == 'image/gif':
            return 'gif'
        return None

    def enabled(self, kind):
        if kind == 'photo':
            return
        if not self.store.settings()[kind + 's_enabled']:
            raise ValueError('Стикеры выключены в настройках' if kind == 'sticker' else 'GIF выключены в настройках')

    def cache(self, document, *, kind=None, label='', pack='', source_chat=0, source_message=0):
        if not isinstance(document, types.Document) or document.size > 20 * 1024 * 1024:
            return None
        kind = kind or self.kind(document)
        if kind not in {'sticker', 'gif'}:
            return None
        sticker = next((a for a in document.attributes if isinstance(a, types.DocumentAttributeSticker)), None)
        label = redact_locations(label or (sticker.alt if sticker else '') or ('Анимация' if kind == 'gif' else 'Стикер'))[:180]
        asset_id = f'{kind}:{document.id}'
        encoded = base64.b64encode(bytes(document)).decode()
        now = time.time()
        self.store.execute('''INSERT INTO telegram_media_assets(asset_id,document_id,kind,label,pack,document,source_chat,source_message,created,updated)
            VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(asset_id) DO UPDATE SET
            document=excluded.document,updated=excluded.updated,
            label=CASE WHEN excluded.label NOT IN ('Анимация','Стикер') THEN excluded.label ELSE telegram_media_assets.label END,
            pack=CASE WHEN excluded.pack!='' THEN excluded.pack ELSE telegram_media_assets.pack END,
            source_chat=CASE WHEN excluded.source_message>0 THEN excluded.source_chat ELSE telegram_media_assets.source_chat END,
            source_message=CASE WHEN excluded.source_message>0 THEN excluded.source_message ELSE telegram_media_assets.source_message END''',
            (asset_id, document.id, kind, label, pack[:100], encoded, source_chat, source_message, now, now))
        return self.store.rows('SELECT asset_id,kind,label,pack,description FROM telegram_media_assets WHERE asset_id=?',(asset_id,))[0]

    def capture(self, message, chat_id):
        if not self.store.settings()['collect_telegram_media'] or chat_id == SECURITY_PEER:
            return None
        if security_message(message) or getattr(message, 'noforwards', False):
            return None
        sender_id = getattr(message, 'sender_id', None)
        if sender_id and self.store.user_blocked(sender_id):
            return None
        document = getattr(message, 'document', None)
        kind = self.kind(document)
        if not kind:
            return None
        try:
            self.enabled(kind)
            return self.cache(document, source_chat=chat_id, source_message=getattr(message, 'id', 0) or 0)
        except (ValueError, TypeError):
            return None

    def collection(self, query='', kind='all', limit=20):
        rows = self.store.rows('SELECT asset_id,kind,label,pack,description FROM telegram_media_assets ORDER BY updated DESC LIMIT 500')
        if kind != 'all':
            rows = [r for r in rows if r['kind'] == kind]
        terms = query.casefold().split()
        matched = [r for r in rows if not terms or all(t in (r['label']+' '+r['pack']+' '+r['description']).casefold() for t in terms)]
        return matched[:limit] or rows[:limit]

    def conversation_offer(self, chat_id):
        """A diverse cached menu for the same completion that writes the reply."""
        if not self.store.settings()['stickers_enabled']:
            return []
        sent = self.store.rows("SELECT kind,text,created FROM messages WHERE contact_id=? AND role='assistant' "
                               "ORDER BY id DESC LIMIT 80", (chat_id,))
        last_media = next((i for i, row in enumerate(sent) if row['kind'] in {'sticker','gif','photo','media'}), None)
        text_count = sum(row['kind'] in {'chat','proactive','voice'} for row in sent[:last_media])
        if last_media is None:
            if text_count < 1:
                return []
        elif text_count < self.CONVERSATION_STICKER_GAP or time.time() - sent[last_media]['created'] < self.CONVERSATION_STICKER_SECONDS:
            return []
        recent_labels = {row['text'].removeprefix('[Стикер: ').removesuffix(']').replace('\ufe0f','')
                         for row in [item for item in sent if item['kind'] == 'sticker'][:1]}
        # Alt text describes an emotion, not an unseen character or scene.
        moods = ('😂🤣😄😁😆😹', '🥰😍😘❤💕', '😳☺😊', '🫂🤗🥺🥹', '🥳🎉👏',
                 '👍👌', '👋', '🤔🤨', '😮😲🤯', '😭😢', '🙃😏😉', '😎😺😇')
        rows = self.store.rows("SELECT asset_id,kind,label,pack,description FROM telegram_media_assets "
                               "WHERE kind='sticker' ORDER BY updated DESC LIMIT 500")
        rows = [row for row in rows if row['label'] and row['label'].replace('\ufe0f','') not in recent_labels]
        offered, labels = [], set()
        for mood in moods:
            item = next((row for row in rows if row['label'].replace('\ufe0f','') in mood), None)
            if item:
                offered.append(item)
                labels.add(item['label'].replace('\ufe0f',''))
        for row in rows:
            if len(offered) >= 12:
                break
            if row['description'] and row['label'].replace('\ufe0f','') not in labels:
                offered.append(row)
                labels.add(row['label'].replace('\ufe0f',''))
        return offered

    def queue_conversation_sticker(self, chat_id, asset_id):
        state = self.runtime.reply_states.get(chat_id)
        if (not state or state.get('superseded') or state.get('media_sent')
                or asset_id not in {row['asset_id'] for row in self.conversation_offer(chat_id)}):
            return False
        state['conversation_sticker'] = asset_id
        return True

    async def send_conversation_sticker(self, chat_id, guard=lambda: None, *, source=None):
        state = self.runtime.reply_states.get(chat_id)
        asset_id = state.pop('conversation_sticker', None) if state else None
        if (not asset_id or not state.get('parts_sent') or state.get('superseded') or state.get('media_sent')
                or asset_id not in {row['asset_id'] for row in self.conversation_offer(chat_id)}):
            return {'sent': False}
        # Text is already delivered. This optional addition cannot replay it.
        guard()
        # Keep the pause cancellable like another reply part; never hold send_lock.
        state['phase'] = 'typing'
        await asyncio.sleep(random.uniform(self.CONVERSATION_STICKER_DELAY_MIN,
                                           self.CONVERSATION_STICKER_DELAY_MAX))
        if (self.runtime.reply_states.get(chat_id) is not state or state.get('superseded') or state.get('media_sent')
                or asset_id not in {row['asset_id'] for row in self.conversation_offer(chat_id)}):
            return {'sent': False}
        guard()
        reply_to = source.get('telegram_id') if (source and source.get('contact_id') == chat_id
                   and self.store.contact(chat_id)['kind'] == 'group') else None
        result = await self.send(chat_id, asset_id, 'sticker', guard,
                                 **({'reply_to': reply_to} if reply_to else {}))
        state['committed'] = True
        return result

    async def request(self, request):
        return await asyncio.wait_for(self.telegram.client(request), 20)

    async def stickers(self, query='', limit=20):
        self.enabled('sticker')
        found = self.collection(query, 'sticker', limit)
        async with self.search_lock:
            for request in (functions.messages.GetFavedStickersRequest(0), functions.messages.GetRecentStickersRequest(0, attached=False)):
                try:
                    result = await self.request(request)
                    for document in getattr(result, 'stickers', []) or []:
                        item = self.cache(document)
                        if item:
                            found.append(item)
                except Exception as error:
                    if type(error).__name__ == 'FloodWaitError':
                        raise
            if query:
                requests = [functions.messages.GetStickersRequest(query, 0), functions.messages.SearchStickerSetsRequest(query, 0)]
                for request in requests:
                    try:
                        result = await self.request(request)
                        for document in getattr(result, 'stickers', []) or []:
                            item = self.cache(document)
                            if item:
                                found.insert(0, item)
                        for covered in (getattr(result, 'sets', []) or [])[:8]:
                            pack = getattr(covered.set, 'short_name', '')
                            documents = getattr(covered, 'covers', []) or [getattr(covered, 'cover', None)]
                            for document in documents:
                                item = self.cache(document, pack=pack)
                                if item:
                                    found.insert(0, item)
                    except Exception as error:
                        if type(error).__name__ == 'FloodWaitError':
                            raise
        unique = {r['asset_id']: r for r in found}
        return {'items': list(unique.values())[:limit], 'kind': 'sticker'}

    async def gifs(self, query='', limit=20):
        self.enabled('gif')
        found = self.collection(query, 'gif', limit)
        searched = []
        self.inline = {k: v for k, v in self.inline.items() if v['expires'] > time.time()}
        async with self.search_lock:
            if query:
                try:
                    results = await asyncio.wait_for(self.telegram.client.inline_query('gif', query), 20)
                    for result in results:
                        raw = getattr(result, 'result', None)
                        if getattr(raw, 'type', None) not in {'gif', 'mpeg4_gif'}:
                            continue
                        label = (getattr(raw, 'title', '') or 'GIF')[:180]
                        if query and query.casefold() in label.casefold(): label='GIF'
                        document = getattr(raw, 'document', None)
                        if isinstance(document, types.Document):
                            item = self.cache(document, kind='gif', label=label)
                        else:
                            asset_id = 'gif-inline:' + str(results.query_id) + ':' + str(raw.id)
                            if len(asset_id) > 180:
                                continue
                            self.inline[asset_id] = {'result': result, 'expires': time.time()+min(240,max(10,getattr(results,'cache_time',240))), 'label': label}
                            item = {'asset_id': asset_id, 'kind': 'gif', 'label': label, 'pack': ''}
                        if item:
                            searched.append(item)
                        if len(searched) >= limit:
                            break
                except Exception as error:
                    if type(error).__name__ == 'FloodWaitError':
                        raise
            try:
                saved = await self.request(functions.messages.GetSavedGifsRequest(0))
                for document in getattr(saved, 'gifs', []) or []:
                    item = self.cache(document, kind='gif')
                    if item:
                        found.append(item)
            except Exception as error:
                if type(error).__name__ == 'FloodWaitError':
                    raise
        unique = {r['asset_id']: r for r in searched+found}
        if len(self.inline) > 100:
            self.inline = dict(list(self.inline.items())[-100:])
        return {'items': list(unique.values())[:limit], 'kind': 'gif'}

    async def document(self, asset_id, guard=lambda: None):
        rows = self.store.rows('SELECT * FROM telegram_media_assets WHERE asset_id=?', (asset_id,))
        if not rows:
            raise ValueError('Медиа не найдено. Сначала найди стикер или GIF и выбери настоящий asset_id')
        row = rows[0]
        if row['source_message'] and time.time()-row['updated'] > 1800:
            guard()
            try:
                message = await asyncio.wait_for(self.telegram.client.get_messages(row['source_chat'], ids=row['source_message']), 15)
                guard()
                document = getattr(message, 'document', None)
                if isinstance(document, types.Document) and document.id == row['document_id'] and not getattr(message, 'noforwards', False):
                    self.cache(document, kind=row['kind'], label=row['label'])
                    return document, row
            except Exception as error:
                if isinstance(error, (ValueError, asyncio.CancelledError)):
                    raise
        with BinaryReader(base64.b64decode(row['document'], validate=True)) as reader:
            document = reader.tgread_object()
        if not isinstance(document, types.Document) or document.id != row['document_id']:
            raise ValueError('Некорректная ссылка на медиа')
        return document, row

    async def photos(self, query, limit=12, chat_id=0, guard=lambda: None):
        if not query.strip():
            raise ValueError('Для фото нужен предмет или сюжет поиска')
        guard()
        async with self.search_lock:
            results = await asyncio.wait_for(self.telegram.client.inline_query(
                'pic', query, **({'entity': chat_id} if chat_id else {})), 20)
            guard()
            items = []
            for result in results:
                raw = getattr(result, 'result', None)
                if getattr(raw, 'type', None) != 'photo':
                    continue
                asset_id = f'photo-inline:{results.query_id}:{raw.id}'
                if len(asset_id) > 180:
                    continue
                label = redact_locations(getattr(raw, 'title', '') or 'Фото из @pic')[:180]
                self.inline[asset_id] = {'result': result, 'expires': time.time()+min(240,max(1,getattr(results,'cache_time',240))),
                                         'label': label, 'kind': 'photo', 'chat_id': chat_id}
                items.append({'asset_id': asset_id, 'kind': 'photo', 'label': label,
                              'description': redact_locations(getattr(raw, 'description', '') or '')[:300]})
                if len(items) >= limit:
                    break
            self.inline = dict(list(self.inline.items())[-100:])
            return {'items': items, 'kind': 'photo', 'bot': '@pic'}

    async def select(self, chat_id, kind, query='', guard=lambda: None):
        """Resolve a high-level request before the single recorded delivery."""
        self.enabled(kind)
        guard()
        if kind == 'photo':
            found = await self.photos(query, 8, chat_id, guard)
        elif kind == 'sticker':
            found = await self.stickers(query, 8)
        else:
            found = await self.gifs(query or 'happy', 8)
        guard()
        items = found['items']
        if not items:
            raise ValueError('Подходящее медиа не нашлось. Попробуй другой сюжет или пришли его в Reply')
        if not query:
            return items[0]['asset_id']
        if kind == 'photo' and all(item.get('label') == 'Фото из @pic' and not item.get('description') for item in items):
            # @pic normally supplies only images; its search order is the available relevance signal.
            return items[0]['asset_id']
        chosen = await self.runtime.model.choose_telegram_media(query, items)
        guard()
        if chosen not in {item['asset_id'] for item in items}:
            raise ValueError('Не удалось выбрать подходящее медиа из результатов поиска')
        return chosen

    async def referenced(self, chat_id, source, kind='all', message_id=None, guard=lambda: None):
        """Bind 'save this' to a real message in the authenticated request's chat."""
        if not source or source['contact_id'] != chat_id or not source.get('telegram_id'):
            raise ValueError('Ответь на нужный стикер или GIF командой «сохрани»')
        bound = source['telegram_id']
        explicit = message_id or source.get('reply_to_telegram_id')
        candidates = [explicit] if explicit else [bound]
        if not explicit:
            candidates += [row['telegram_id'] for row in self.store.rows(
                "SELECT telegram_id FROM messages WHERE contact_id=? AND telegram_id<? "
                "AND (kind IN ('sticker','gif') OR image_kind IN ('sticker','gif')) "
                "AND created>=? AND context_sender_id=? AND context_generation=? ORDER BY id DESC LIMIT 1",
                (chat_id, bound, source['created']-86400, source['context_sender_id'], source['context_generation']))]
        for candidate in candidates:
            if type(candidate) is not int or not 0 < candidate <= bound:
                raise ValueError('Выбери медиа из текущей переписки')
            guard()
            msg = await asyncio.wait_for(self.telegram.client.get_messages(chat_id, ids=candidate), 15)
            guard()
            if (not isinstance(msg, types.Message) or msg.id != candidate or msg.chat_id != chat_id
                    or security_message(msg) or getattr(msg, 'noforwards', False)
                    or self.store.user_blocked(msg.sender_id or 0)):
                if explicit:
                    raise ValueError('Это медиа недоступно для сохранения')
                continue
            actual_kind = self.kind(msg.document)
            if actual_kind and kind in {'all', actual_kind}:
                self.enabled(actual_kind)
                return self.cache(msg.document, source_chat=chat_id, source_message=msg.id)['asset_id']
        raise ValueError('Ответь на нужный стикер или GIF командой «сохрани»')

    async def save_selected(self, chat_id, args, source, guard=lambda: None):
        asset_id = args.get('asset_id')
        if not asset_id:
            if source and source.get('reply_to_telegram_id'):
                asset_id = await self.referenced(chat_id, source, args.get('kind','all'), args.get('message_id'), guard)
            elif args.get('query'):
                kind = args.get('kind', 'all')
                if kind not in {'sticker', 'gif'}:
                    raise ValueError('Укажи, что найти и сохранить: стикер или GIF')
                asset_id = await self.select(chat_id, kind, args['query'], guard)
            else:
                asset_id = await self.referenced(chat_id, source, args.get('kind', 'all'), args.get('message_id'), guard)
        return await self.save(asset_id, args.get('remove', False), guard)

    async def send(self, chat_id, asset_id, kind, guard=lambda: None, *, automated=True, reply_to=None):
        self.enabled(kind)
        inline = self.inline.get(asset_id) if kind in {'gif', 'photo'} else None
        if inline:
            if inline['expires'] <= time.time():
                raise ValueError('Результат поиска истёк. Найди медиа снова')
            if inline.get('kind', 'gif') != kind or inline.get('chat_id', chat_id) not in {0, chat_id}:
                raise ValueError('Выбери медиа для текущего чата')
            row = {'label': inline['label'], 'kind': kind}
            document = None
        elif kind == 'photo':
            raise ValueError('Фото не найдено. Сначала выполни поиск через @pic')
        else:
            document, row = await self.document(asset_id, guard)
            if row['kind'] != kind:
                raise ValueError('Выбран другой тип медиа')
        async with self.runtime.send_lock:
            guard()
            state = self.runtime.reply_states.get(chat_id)
            if state and state.get('media_sent'):
                raise ValueError('В этой реплике медиа уже отправлено')
            self.enabled(kind)
            self.runtime.require_active_dialog(chat_id)
            self.telegram.require_unblocked(chat_id)
            if not self.store.contact(chat_id)['enabled'] or automated and not self.store.settings()['enabled']:
                raise ValueError('Отправка отключена')
            if automated and self.store.contact(chat_id)['kind'] == 'group' and not self.store.settings()['group_enabled']:
                raise ValueError('Ответы в группах выключены')
            self.runtime.rate_check(chat_id, time.time(), self.store.settings())
            guard()
            if inline:
                sent = await inline['result'].click(chat_id, reply_to=reply_to)
            else:
                sent = await self.telegram.client.send_file(chat_id, utils.get_input_document(document),
                    **({'reply_to': reply_to} if reply_to else {}))
            if isinstance(sent, list):
                sent = sent[0] if len(sent) == 1 else None
            message_id = getattr(sent, 'id', None)
            if type(message_id) is not int:
                raise ValueError('Telegram не подтвердил отправку медиа; автоматически не повторяю')
            label = {'sticker': 'Стикер', 'gif': 'GIF', 'photo': 'Фото'}[kind]
            self.store.message(chat_id, 'assistant', '['+label+': '+row['label']+']', message_id, kind,
                               reply_to_telegram_id=reply_to)
            if inline:
                self.capture(sent, chat_id)
            if state:
                state['media_sent'] = True
            self.store.event(label+' отправлен: '+self.store.contact(chat_id)['name'])
            return {'sent': True, 'telegram_id': message_id, 'kind': kind}

    async def save(self, asset_id, remove=False, guard=lambda: None):
        inline = self.inline.get(asset_id)
        if inline:
            if inline.get('kind', 'gif') != 'gif' or inline['expires'] <= time.time():
                raise ValueError('Для сохранения найди GIF снова')
            raw = inline['result'].result
            content = getattr(raw, 'content', None)
            url = getattr(content, 'url', '')
            if not url.startswith('https://'):
                raise ValueError('У GIF нет доступного файла. Сохрани её через Reply после отправки')
            guard()
            uploaded = await self.request(functions.messages.UploadMediaRequest(
                types.InputPeerSelf(), types.InputMediaDocumentExternal(url)))
            guard()
            document = getattr(uploaded, 'document', None)
            if self.kind(document) != 'gif':
                raise ValueError('Telegram не подтвердил файл GIF для сохранения')
            asset_id = self.cache(document, label=inline['label'])['asset_id']
        document, row = await self.document(asset_id, guard)
        self.enabled(row['kind'])
        guard()
        request = (functions.messages.FaveStickerRequest(utils.get_input_document(document), unfave=remove)
                   if row['kind']=='sticker' else functions.messages.SaveGifRequest(utils.get_input_document(document), unsave=remove))
        if await self.request(request) is False:
            raise ValueError('Telegram не подтвердил сохранение медиа')
        guard()
        return {'saved': not remove, 'asset_id': asset_id, 'kind': row['kind']}

    async def preview(self, asset_id):
        import hashlib
        from PIL import Image
        directory=self.store.directory/'telegram-media-previews'
        directory.mkdir(exist_ok=True,mode=0o700)
        path=directory/(hashlib.sha256(asset_id.encode()).hexdigest()+'.jpg')
        if path.is_file(): return path
        document,_=await self.document(asset_id)
        raw=await asyncio.wait_for(self.telegram.client.download_media(document,thumb=-1,file=bytes),15)
        if not raw and document.mime_type=='image/webp' and document.size<512*1024:
            raw=await asyncio.wait_for(self.telegram.client.download_media(document,file=bytes),15)
        if not isinstance(raw,bytes) or len(raw)>1024*1024:
            raise ValueError('Превью этого медиа недоступно')
        with Image.open(io.BytesIO(raw)) as picture:
            picture.thumbnail((240,240))
            canvas=Image.new('RGB',picture.size,'white')
            if picture.mode=='RGBA': canvas.paste(picture,mask=picture.getchannel('A'))
            else: canvas.paste(picture.convert('RGB'))
            canvas.save(path,'JPEG',quality=80)
        path.chmod(0o600)
        return path

    async def inspect(self,asset_id,guard=lambda:None):
        document,row=await self.document(asset_id,guard)
        if row['description']: return {'asset_id':asset_id,'description':row['description']}
        if not self.store.settings()['vision_enabled']: raise ValueError('Распознавание изображений выключено')
        if document.size>12*1024*1024: raise ValueError('Для распознавания медиа должно быть до 12 МБ')
        guard()
        raw=await asyncio.wait_for(self.telegram.client.download_media(document,file=bytes),20)
        guard()
        from .vision import save_visual,image_url
        root=self.store.directory/'media-inspection';root.mkdir(exist_ok=True,mode=0o700)
        relative=await asyncio.to_thread(save_visual,root,raw,row['kind'],document.mime_type)
        try:
            description=await self.runtime.model.inspect_image(image_url(root,relative),
                'Это отдельный стикер или GIF. Кадры анимации идут по порядку. Коротко опиши сюжет, эмоцию и уместное настроение. '
                'Не связывай изображение с отправителем или личной перепиской. Не выводи пароли, коды входа или токены.')
            guard()
            description=redact_locations(description)[:600]
            if re.search(r'(?i)парол|api.?key|токен|ibsec_|код\s+(?:входа|подтверждения)',description):
                description='Описание содержит закрытые данные и не сохраняется'
            self.store.execute('UPDATE telegram_media_assets SET description=? WHERE asset_id=?',(description,asset_id))
            return {'asset_id':asset_id,'description':description}
        finally:
            (root/relative).unlink(missing_ok=True)

    async def learn_pack(self,asset_id,guard=lambda:None):
        document,row=await self.document(asset_id,guard)
        self.enabled('sticker')
        sticker=next((a for a in document.attributes if isinstance(a,types.DocumentAttributeSticker)),None)
        if row['kind']!='sticker' or sticker is None or isinstance(sticker.stickerset,types.InputStickerSetEmpty):
            raise ValueError('У этого стикера нет доступного набора')
        guard()
        result=await self.request(functions.messages.GetStickerSetRequest(sticker.stickerset,0))
        guard()
        pack=result.set.short_name
        items=[item for d in result.documents[:200] if (item:=self.cache(d,pack=pack))]
        return {'pack':pack,'title':result.set.title,'collected':len(items),'items':items[:20]}

    async def execute(self, chat_id, name, args, guard=lambda: None, *, source=None):
        if name in {'find_stickers', 'find_gifs', 'find_photos', 'media_collection'}:
            allowed = {'query', 'limit'} | ({'kind'} if name=='media_collection' else set())
            if set(args)-allowed or not isinstance(args.get('query',''),str) or len(args.get('query',''))>100:
                raise ValueError('Укажи короткий запрос и число результатов')
            limit=args.get('limit',12)
            if type(limit) is not int or not 1<=limit<=20:
                raise ValueError('Количество результатов: 1–20')
            guard()
            if name=='find_stickers':
                return await self.stickers(args.get('query',''),limit)
            if name=='find_gifs':
                return await self.gifs(args.get('query',''),limit)
            if name=='find_photos':
                return await self.photos(args.get('query',''),limit,chat_id,guard)
            kind=args.get('kind','all')
            if kind not in {'all','sticker','gif'}:
                raise ValueError('Выбери sticker, gif или all')
            return {'items': self.collection(args.get('query',''),kind,limit)}
        allowed={'asset_id'} | ({'remove','query','kind'} if name=='save_telegram_media' else
                               {'query'} if name in {'send_sticker','send_gif','send_photo'} else set())
        if (set(args)-allowed or 'asset_id' in args and (not isinstance(args['asset_id'],str) or not 1<=len(args['asset_id'])<=180)
                or 'query' in args and (not isinstance(args['query'],str) or len(args['query'])>100)):
            raise ValueError('Укажи настоящий asset_id из поиска или коллекции')
        if name=='save_telegram_media':
            if type(args.get('remove',False)) is not bool:
                raise ValueError('remove должен быть переключателем')
            if args.get('kind','all') not in {'all','sticker','gif'}:
                raise ValueError('Выбери sticker, gif или all')
            return await self.save_selected(chat_id,args,source,guard)
        if name=='inspect_telegram_media': return await self.inspect(args['asset_id'],guard)
        if name=='learn_sticker_pack': return await self.learn_pack(args['asset_id'],guard)
        kind={'send_sticker':'sticker','send_gif':'gif','send_photo':'photo'}[name]
        state=self.runtime.reply_states.get(chat_id)
        if state and state.get('media_sent'):
            raise ValueError('В этой реплике медиа уже отправлено')
        asset_id=args.get('asset_id') or await self.select(chat_id,kind,args.get('query',''),guard)
        topic=self.store.group_topic(chat_id,source.get('telegram_id')) if source and source.get('contact_id')==chat_id else 0
        result=await self.send(chat_id,asset_id,kind,guard,**({'reply_to':topic} if topic else {}))
        if state:
            state['media_sent']=True
        return result
