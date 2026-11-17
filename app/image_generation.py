"""A separate image model, with delivery confined to the requesting dialog."""
import asyncio
import base64
import binascii
import io
import re
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from PIL import Image, UnidentifiedImageError

from .settings import validate_url
from .websites import validate_public_url

MAX_IMAGE_BYTES = 10 * 1024 * 1024


def origin(url):
    parsed = urlsplit(url)
    return parsed.scheme, parsed.hostname, parsed.port or 443


def parse_generation_request(text):
    text = re.sub(r'^(?:(?:дарья|даша|даш|дашка|дашенька)[,\s]+)+', '', text.strip(), flags=re.I)
    match = re.fullmatch(
        r'(?:(?:пожалуйста|плиз)[,\s]+)?(?:нарисуй|нарисуйте|сгенерируй|сгенерируйте|'
        r'создай\s+(?:изображение|картинку|фото)|сделай\s+(?:картинку|изображение)|'
        r'(?:можешь|можете)\s+(?:нарисовать|сгенерировать))\s+(.+)', text, re.I | re.S)
    return match[1].strip() if match and match[1].strip() else None


def normalize_image(data):
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise ValueError('Провайдер вернул пустое или слишком большое изображение')
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.format not in {'PNG', 'JPEG', 'WEBP'} or image.width * image.height > 25_000_000:
                raise ValueError('Неподдерживаемое изображение')
            image.load()
            image.thumbnail((4096, 4096))
            rgba = image.convert('RGBA')
            background = Image.new('RGB', rgba.size, 'white')
            background.paste(rgba, mask=rgba.getchannel('A'))
            output = io.BytesIO()
            background.save(output, 'JPEG', quality=93)
            data = output.getvalue()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
        raise ValueError('Провайдер вернул повреждённое изображение') from None
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError('Изображение слишком большое для Telegram')
    return data


class ImageGenerator:
    def __init__(self, runtime):
        self.runtime, self.store = runtime, runtime.store
        self.lock = asyncio.Lock()
        self.previews = {}

    def connection(self, overrides=None):
        saved = self.store.settings()
        values = {
            'base_url': saved['image_generation_base_url'] or saved['ai_base_url'],
            'model': saved['image_generation_model'],
            'api_mode': saved['image_generation_api_mode'],
        }
        if overrides:
            values.update({key: value for key, value in overrides.items()
                           if key in values and value is not None})
        values['base_url'] = (values['base_url'] or saved['ai_base_url']).rstrip('/')
        validate_url(values['base_url'])
        if values['api_mode'] not in {'images', 'chat'}:
            raise ValueError('Выберите формат API генерации изображений')
        credentials = self.store.secrets()
        separate_key = credentials.get('image_generation_key', '')
        bound = credentials.get('image_generation_key_origin')
        key = separate_key if bound == list(origin(values['base_url'])) else ''
        if not key and origin(values['base_url']) == origin(saved['ai_base_url']):
            key = self.runtime.model.key()
        if overrides and overrides.get('key', '').strip():
            key = overrides['key'].strip()
        if not key:
            raise ValueError('Укажите API-ключ для генерации изображений на сайте')
        return values, key

    def status(self):
        saved = self.store.settings()
        try:
            _, key = self.connection()
        except ValueError:
            key = ''
        return {'configured': bool(saved['image_generation_model'].strip() and key),
                'enabled': saved['image_generation_enabled']}

    async def discover(self, overrides=None):
        values, key = self.connection(overrides)
        try:
            response = await self.runtime.model.http.get(values['base_url']+'/models',
                headers={'Authorization': 'Bearer '+key})
            if response.status_code != 200:
                raise ValueError(f'Список моделей не получен: API вернул {response.status_code}. Модель можно вписать вручную')
            body = response.json()
            rows = body.get('data', body.get('models', []))
            models, image_models = set(), set()
            for row in rows:
                name = row.get('id') if isinstance(row, dict) else row
                if not isinstance(name, str) or not 0 < len(name) <= 200 or name.startswith('sk-'):
                    continue
                models.add(name)
                architecture = row.get('architecture', {}) if isinstance(row, dict) else {}
                if 'image' in architecture.get('output_modalities', []) or re.search(
                        r'gpt-image|dall-e|flux|imagen|stable-diffusion|image-preview|image-generation', name, re.I):
                    image_models.add(name)
            if not models:
                raise ValueError('API не вернул моделей. Впишите модель вручную')
            return {'models': sorted(models)[:1000], 'image_models': sorted(image_models)[:1000]}
        except httpx.HTTPError:
            raise ValueError('API не ответил при поиске моделей') from None
        except (AttributeError, TypeError):
            raise ValueError('API вернул неподдерживаемый список моделей') from None

    async def generate(self, prompt, *, overrides=None, guard=lambda: None):
        if not isinstance(prompt, str) or not 1 <= len(prompt.strip()) <= 4000:
            raise ValueError('Опишите изображение текстом длиной до 4000 символов')
        if not self.store.settings()['image_generation_enabled']:
            raise ValueError('Генерация изображений выключена на сайте')
        values, key = self.connection(overrides)
        if not values['model'].strip():
            raise ValueError('Модель генерации изображений ещё не указана на сайте')
        if values['api_mode'] == 'images':
            path = '/images/generations'
            payload = {'model': values['model'], 'prompt': prompt.strip(), 'n': 1}
        else:
            path = '/chat/completions'
            payload = {'model': values['model'], 'messages': [{'role': 'user', 'content': prompt.strip()}],
                       'modalities': ['image', 'text']}
        guard()
        try:
            # No automatic retries: a timeout may follow a paid generation.
            async with self.lock:
                guard()
                response = await self.runtime.model.http.post(values['base_url']+path, json=payload,
                    headers={'Authorization': 'Bearer '+key}, timeout=httpx.Timeout(180, connect=15))
            guard()
            if response.status_code != 200:
                raise ValueError(f'Изображение не создано: API вернул {response.status_code}. Проверьте модель и формат API на сайте')
            body = response.json()
            if values['api_mode'] == 'images':
                row = body['data'][0]
                value = row.get('b64_json') or row.get('url')
                encoded = bool(row.get('b64_json'))
            else:
                message = body['choices'][0]['message']
                images = message.get('images') or [part for part in message.get('content', [])
                          if isinstance(part, dict) and part.get('type') in {'image_url', 'image'}]
                row = images[0]
                value = row.get('image_url', row.get('url'))
                value = value.get('url') if isinstance(value, dict) else value
                encoded = False
            if not isinstance(value, str) or not value:
                raise ValueError('Модель не вернула изображение. Проверьте, что выбранная модель умеет рисовать')
            if value.startswith('data:image/'):
                prefix, value = value.split(',', 1)
                if ';base64' not in prefix:
                    raise ValueError('Неподдерживаемый формат изображения')
                encoded = True
            if encoded:
                if len(value) > (MAX_IMAGE_BYTES * 4 // 3 + 8):
                    raise ValueError('Провайдер вернул слишком большое изображение')
                data = base64.b64decode(value, validate=True)
            else:
                await validate_public_url(value)
                # Download public image bytes without the provider's API key.
                async with self.runtime.model.http.stream('GET', value, headers={'Authorization': ''},
                        follow_redirects=False, timeout=45) as download:
                    if download.status_code != 200:
                        raise ValueError('Не удалось скачать созданное изображение')
                    data = bytearray()
                    async for chunk in download.aiter_bytes():
                        data.extend(chunk)
                        if len(data) > MAX_IMAGE_BYTES:
                            raise ValueError('Провайдер вернул слишком большое изображение')
            guard()
            data = await asyncio.to_thread(normalize_image, data)
            guard()
            return data
        except httpx.HTTPError:
            raise ValueError('API генерации не ответил. Автоматически не повторяю запрос') from None
        except (KeyError, IndexError, TypeError, binascii.Error):
            raise ValueError('Модель не вернула корректное изображение') from None

    async def execute(self, chat_id, args, guard, *, source=None):
        if set(args) != {'prompt'}:
            raise ValueError('Укажите только сюжет изображения; чат выбирает сервер')
        state = self.runtime.reply_states.get(chat_id)
        if state and state.get('media_sent'):
            raise ValueError('В этой реплике изображение уже отправлено')
        if state and state.get('image_generation_started'):
            raise ValueError('Генерация в этой реплике уже запускалась; автоматически не повторяю')
        settings = self.store.settings()
        model_fingerprint = (settings['image_generation_model'], settings['image_generation_base_url'],
                             settings['image_generation_api_mode'], settings['image_generation_enabled'])
        def check():
            guard()
            current = self.store.settings()
            if model_fingerprint != (current['image_generation_model'], current['image_generation_base_url'],
                                     current['image_generation_api_mode'], current['image_generation_enabled']):
                raise ValueError('Настройки генерации изменились; задай запрос снова')
            self.runtime.telegram.require_unblocked(chat_id)
            if self.store.contact(chat_id)['kind'] == 'group' and not current['group_enabled']:
                raise ValueError('Ответы в группах выключены')
        check()
        self.runtime.rate_check(chat_id, time.time(), self.store.settings())
        if state:
            state['image_generation_started'] = True
        data = await self.generate(args['prompt'], guard=check)
        folder = self.store.directory/'generated'
        folder.mkdir(mode=0o700, exist_ok=True)
        with tempfile.NamedTemporaryFile(suffix='.jpg', dir=folder, delete=False) as file:
            file.write(data)
            path = Path(file.name)
        try:
            async with self.runtime.send_lock:
                check()
                if state and state.get('media_sent'):
                    raise ValueError('В этой реплике медиа уже отправлено')
                self.runtime.rate_check(chat_id, time.time(), self.store.settings())
                reply_to = source['telegram_id'] if source and source['contact_id'] == chat_id else None
                message_id = await self.runtime.telegram.send_media(chat_id, path, 'Создано Дарьей',
                    before_dispatch=check, reply_to=reply_to)
                if type(message_id) is not int:
                    raise ValueError('Telegram не подтвердил отправку; автоматически не повторяю')
                if state:
                    state['media_sent'] = True
                self.store.message(chat_id, 'assistant', '[Созданное изображение]', message_id, 'photo',
                                   reply_to_telegram_id=reply_to)
                return {'sent': True, 'telegram_id': message_id, 'kind': 'photo', 'generated': True}
        finally:
            path.unlink(missing_ok=True)
