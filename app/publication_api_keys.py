"""Publish public API access records exactly as supplied by a configured source."""
import asyncio
import hashlib
import json
import re
import time
from urllib.parse import urlsplit
import httpx

from .research import public_link
from .websites import validate_public_url


KEY = r'sk-[A-Za-z0-9_.-]{16,2048}'
MODEL = r'[A-Za-z0-9][A-Za-z0-9_./:+-]{1,160}'


def source_models(text):
    models=[]
    for match in re.finditer(r'(?im)\b(?:модели|models|model[ _-]?id|бесплатно)\s*(?:\([^\n)]*\))?\s*:',text):
        for line in text[match.end():].splitlines():
            value=line.strip().lstrip('•*- ').strip('`')
            if not value:continue
            values=[part.strip().strip('`') for part in value.split(',')]
            if not all(re.fullmatch(MODEL,part) for part in values):break
            models.extend(values)
    return list(dict.fromkeys(models))


def model_ids(data):
    if isinstance(data,dict):
        data=data.get('data',data.get('models',[]))
        if isinstance(data,dict):data=data.get('models',[])
    if not isinstance(data,list):return []
    models=[]
    for row in data:
        value=row if isinstance(row,str) else next((row.get(key) for key in ('id','name','model','model_id') if isinstance(row.get(key),str)),None) if isinstance(row,dict) else None
        if isinstance(value,str) and re.fullmatch(MODEL,value):
            models.append(value.removeprefix('models/'))
    return list(dict.fromkeys(models))[:500]


def access_record(item):
    """Require a public material, one exact key and one explicit API endpoint."""
    origin = urlsplit(item['url'])
    if origin.hostname in {'t.me', 'telegram.me'} and origin.path.startswith('/c/'):
        return None
    text = item['text']
    keys = list(dict.fromkeys(re.findall(r'(?<![\w-])' + KEY, text)))
    keys += [key for key in re.findall(
        r'(?im)(?:^|\n)\s*(?:ключ|api[ _-]?key|token|🔑)\s*[:=]?\s*([A-Za-z0-9_.-]{16,2048})', text)
        if key not in keys]
    if len(keys) != 1 or keys[0].startswith(('ibsec_', 'ibk_')):
        return None
    urls = re.findall(r'https://[^\s<>"«»]+', text)
    endpoints = []
    for value in urls:
        value=value.rstrip('.,;!]}')
        while value.endswith(')') and value.count(')')>value.count('('):value=value[:-1]
        try:
            endpoint = public_link(value)
        except ValueError:
            continue
        parsed = urlsplit(endpoint)
        if (parsed.hostname in {'t.me', 'telegram.me', 'www.t.me'} or parsed.query
                or any(word in parsed.path.casefold().split('/') for word in ['docs', 'guides', 'pricing', 'register'])):
            continue
        if endpoint not in endpoints:
            endpoints.append(endpoint)
    if len(endpoints) != 1:
        return None
    models = source_models(text)
    return {'key': keys[0], 'endpoint': endpoints[0], 'models': list(dict.fromkeys(models)), 'models_checked': False}


def fingerprint(record):
    return hashlib.sha256(('public-api:' + record['key'] + '\n' + record['endpoint']).encode()).hexdigest()


async def discover_models(runtime, item, record, guard, *, force=False):
    payload = json.loads(item.get('payload') or '{}')
    cached = payload.get('public_api_models')
    if not force and cached and cached.get('fingerprint') == fingerprint(record):
        if cached['checked'] and cached.get('updated',0)>time.time()-3600:
            return record | {'models': cached['models'], 'models_checked': True}
        if cached.get('version')==2 and cached.get('updated',0)>time.time()-300:
            return record
    base = record['endpoint'].rstrip('/')
    origin='https://'+urlsplit(base).netloc
    paths = list(dict.fromkeys([base+'/models',origin+'/v1/models',origin+'/models']))
    result = record
    deadline=time.monotonic()+15
    attempts=[(url,headers) for url in paths for headers in [
        {'Authorization':'Bearer '+record['key'],'Accept':'application/json'},
        {'x-api-key':record['key'],'anthropic-version':'2023-06-01','Accept':'application/json'}]]
    for url,headers in attempts:
        if time.monotonic()>=deadline:break
        guard()
        try:
            async with asyncio.timeout(min(5,deadline-time.monotonic())):
                await validate_public_url(url)
                guard()
                async with runtime.websites.http.stream('GET',url,headers=headers,
                        timeout=httpx.Timeout(4,connect=3)) as response:
                    if response.status_code != 200:
                        continue
                    raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        raw.extend(chunk)
                        if len(raw) > 256 * 1024:
                            raise ValueError('Model catalog too large')
                    data = json.loads(raw)
                models = model_ids(data)
                if models:
                    result = record | {'models': list(dict.fromkeys(models))[:500], 'models_checked': True}
                    break
        except asyncio.CancelledError:
            raise
        except Exception:
            guard()
    guard()
    payload['public_api_models'] = {'fingerprint': fingerprint(record), 'models': result['models'],
                                  'checked': result['models_checked'], 'updated': time.time(),'version':2}
    encoded = json.dumps(payload, ensure_ascii=False)
    runtime.store.execute("UPDATE publication_items SET payload=? WHERE id=? AND status IN ('new','used')", (encoded, item['id']))
    item['payload'] = encoded
    return result


def render(record):
    text = 'API-ключ:\n' + record['key'] + '\n\nЭндпоинт: ' + record['endpoint']
    label = 'Модели (/models)' if record['models_checked'] else 'Модели по источнику'
    text += '\n\n' + label + ':\n'
    if not record['models']:
        return text + 'Не указаны; получить список не удалось.'
    for index, model in enumerate(record['models']):
        if len((text + model + '\n').encode('utf-16-le')) // 2 > 3900:
            return text + 'И ещё ' + str(len(record['models']) - index) + ' моделей.'
        text += model + '\n'
    return text.rstrip()


def cached_record(item):
    record = access_record(item)
    cached = json.loads(item.get('payload') or '{}').get('public_api_models')
    if record and cached and cached.get('fingerprint') == fingerprint(record) and cached['checked']:
        record |= {'models': cached['models'], 'models_checked': cached['checked']}
    return record
