"""Fetch public pages using Bright Data's monthly free-credit allowance."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
import math
import os
import re
import time

import httpx
from .store import private_json

FETCH_DATASET = "gd_m6gjtfmeh43we6cqc"
MONTHLY_REQUESTS = 5000
TRANSIENT = {408, 429, 500, 502, 503, 504}


class CloudUnavailable(ValueError):
    def __init__(self, message, retry_after=15):
        super().__init__(message)
        self.retry_after = max(1, retry_after)


class CloudPending(CloudUnavailable):
    pass


class CloudUncertain(ValueError):
    pass


def retry_delay(headers, default=2):
    try:
        value = float(headers.get('Retry-After', default))
        return max(1, value) if math.isfinite(value) else default
    except ValueError:
        try:
            return max(1, parsedate_to_datetime(headers['Retry-After']).timestamp()-time.time())
        except (ValueError,KeyError,TypeError,OverflowError):
            return default


class BrightData:
    def __init__(self, store):
        self.store = store
        self.http = None
        self.scope = ContextVar('brightdata_job',default=None)
        self.pause_until = 0

    @contextmanager
    def job(self, job_id):
        token = self.scope.set(job_id)
        try:
            yield
        finally:
            self.scope.reset(token)

    def key(self):
        return os.getenv("DARYA_BRIGHTDATA_KEY", "") or self.store.secrets().get("brightdata_key", "")

    def configured(self):
        return bool(self.key()) and self.store.settings()["brightdata_enabled"]

    def reserve(self):
        month = datetime.now(timezone.utc).strftime("%Y-%m")
        settings = self.store.settings()
        used = settings["brightdata_requests"] if settings["brightdata_month"] == month else 0
        if used >= MONTHLY_REQUESTS:
            raise ValueError("Бесплатная месячная квота запросов Дарьи к Bright Data закончилась")
        # Every POST attempt consumes the local free-credit allowance.
        self.store.update_settings({"brightdata_month": month, "brightdata_requests": used + 1})

    async def request(self, method, path, **kwargs):
        if self.http is None:
            self.http = httpx.AsyncClient(base_url="https://api.brightdata.com",
                timeout=httpx.Timeout(30, connect=10), follow_redirects=False)
        for attempt in range(3):
            await asyncio.sleep(max(0,self.pause_until-time.time()))
            if method == 'POST':
                self.reserve()
            try:
                async with self.http.stream(method,path,headers={'Authorization':'Bearer '+self.key()},**kwargs) as response:
                    code,headers = response.status_code,response.headers
                    raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        raw.extend(chunk)
                        if len(raw)>12*1024*1024:
                            raise ValueError('Ответ Bright Data слишком большой')
                try:
                    data = json.loads(raw)
                except (ValueError,TypeError):
                    data = None
                if code in {402,403}:
                    raise ValueError('Bright Data отказал в доступе. Проверьте бесплатные кредиты и состояние аккаунта')
                if method=='POST' and isinstance(data,dict) and self.valid_snapshot(data.get('snapshot_id')):
                    return 202,data
                if code in {200,202}:
                    if data is None:
                        raise ValueError('Bright Data вернул неподдерживаемый ответ')
                    return code,data
                if code not in TRANSIENT:
                    raise ValueError(f'Bright Data вернул HTTP {code}')
                error = CloudUnavailable(f'Bright Data временно недоступен (HTTP {code})',retry_delay(headers,30 if code==429 else 2))
                if code==429:
                    self.pause_until = max(self.pause_until,time.time()+error.retry_after)
                    raise error
            except (httpx.HTTPError,asyncio.TimeoutError):
                if method=='POST':
                    raise CloudUncertain('Bright Data не подтвердил запуск сбора. Повтор без номера задания автоматически не выполнялся') from None
                error = CloudUnavailable('Bright Data временно не подтвердил проверку задания')
            if attempt==2:
                raise error
            await asyncio.sleep(max(error.retry_after,2**(attempt+1)))

    @staticmethod
    def valid_snapshot(value):
        return isinstance(value,str) and re.fullmatch(r'sd_[a-zA-Z0-9]+',value)

    def checkpoint(self,url,**values):
        job_id = self.scope.get()
        if job_id is not None:
            fields = list(values)
            self.store.execute('UPDATE cloud_fetches SET '+','.join(field+'=?' for field in fields)+',updated=? WHERE job_id=? AND url=?',
                tuple(values[field] for field in fields)+(time.time(),job_id,url))

    @staticmethod
    def record(data):
        rows = data if isinstance(data,list) else [data]
        if len(rows)!=1 or not isinstance(rows[0],dict) or rows[0].get('error'):
            raise ValueError('Bright Data не смог получить страницу')
        html = rows[0].get('page_html')
        if not isinstance(html,str) or not html or len(html.encode())>3*1024*1024:
            raise ValueError('Bright Data не вернул допустимую HTML-страницу')
        return rows[0]

    def save_record(self,url,data):
        record = self.record(data)
        job_id = self.scope.get()
        if job_id is not None:
            filename = str(job_id)+'-'+hashlib.sha256(url.encode()).hexdigest()+'.json'
            private_json(self.store.directory/'cloud-cache'/filename,record)
            self.checkpoint(url,status='ready',cache_file=filename,error='')
        return record

    async def fetch(self, url):
        if not self.configured():
            raise ValueError("Получение страниц через Bright Data не подключено")
        job_id,snapshot = self.scope.get(),None
        rows = self.store.rows('SELECT * FROM cloud_fetches WHERE job_id=? AND url=?',(job_id,url)) if job_id is not None else []
        if rows:
            saved = rows[0]
            snapshot = saved['snapshot_id'] or None
            if saved['status']=='ready' and saved['cache_file']:
                filename = saved['cache_file']
                if re.fullmatch(r'\d+-[a-f0-9]{64}\.json',filename):
                    try:
                        return self.record(json.loads((self.store.directory/'cloud-cache'/filename).read_text()))
                    except (OSError,ValueError):
                        pass
            if saved['status'] in {'submitting','uncertain','failed'}:
                raise ValueError(saved['error'] or 'Запуск страницы не подтверждён; новый сбор автоматически не выполнялся')
        elif job_id is not None:
            self.store.execute('INSERT INTO cloud_fetches(job_id,url,updated) VALUES (?,?,?)',(job_id,url,time.time()))
        try:
            async with asyncio.timeout(165):
                if not snapshot:
                    self.checkpoint(url,status='submitting',error='')
                    try:
                        code,data = await self.request('POST','/datasets/v3/trigger',
                            params={'dataset_id':FETCH_DATASET,'format':'json'},json=[{'url':url}])
                    except CloudUnavailable as error:
                        self.checkpoint(url,status='pending',error=str(error))
                        raise
                    except ValueError as error:
                        self.checkpoint(url,status='uncertain' if isinstance(error,CloudUncertain) else 'failed',error=str(error))
                        raise
                    snapshot = data.get('snapshot_id') if isinstance(data,dict) else None
                    if not self.valid_snapshot(snapshot):
                        if code==200:
                            return self.save_record(url,data)
                        self.checkpoint(url,status='uncertain',error='Bright Data не вернул номер сбора')
                        raise CloudUncertain('Bright Data не вернул номер сбора')
                    self.checkpoint(url,snapshot_id=snapshot,status='running',error='')
                for _ in range(24):
                    _, progress = await self.request("GET", "/datasets/v3/progress/" + snapshot)
                    if not isinstance(progress, dict):
                        raise ValueError("Bright Data не вернул состояние сбора")
                    if progress.get("status") == "ready":
                        code, data = await self.request("GET", "/datasets/v3/snapshot/" + snapshot, params={"format": "json"})
                        if code==202:
                            await asyncio.sleep(5)
                            continue
                        return self.save_record(url,data)
                    if progress.get("status") == "failed":
                        self.checkpoint(url,status='failed',error='Bright Data не смог получить страницу')
                        raise ValueError("Bright Data не смог получить страницу")
                    await asyncio.sleep(5)
                else:
                    raise CloudPending('Bright Data ещё собирает страницу; проверка того же задания продолжится',15)
        except asyncio.TimeoutError:
            if snapshot:
                raise CloudPending('Проверка страницы задержалась; номер задания сохранён',15) from None
            self.checkpoint(url,status='uncertain',error='Bright Data не подтвердил запуск сбора')
            raise CloudUncertain('Bright Data не подтвердил запуск сбора') from None

    def clear_cache(self,job_id):
        for row in self.store.rows('SELECT cache_file FROM cloud_fetches WHERE job_id=?',(job_id,)):
            if re.fullmatch(r'\d+-[a-f0-9]{64}\.json',row['cache_file']):
                (self.store.directory/'cloud-cache'/row['cache_file']).unlink(missing_ok=True)
        self.store.execute("UPDATE cloud_fetches SET cache_file='' WHERE job_id=?",(job_id,))

    async def close(self):
        if self.http is not None:
            await self.http.aclose()
