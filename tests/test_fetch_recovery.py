import asyncio
from datetime import datetime,timezone
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock,patch

import httpx
from app.brightdata import BrightData,CloudUnavailable,CloudPending,CloudUncertain
from app.runtime import Runtime
from app.store import Store

URL='https://www.avito.ru/simferopol/telefony?q=iphone+11&s=104'
HTML='<title>Авито</title><div data-marker="item"><a data-marker="item-title" href="/simferopol/telefony/iphone_11_123">iPhone 11</a><span data-marker="item-price">5900 ₽</span></div>'


class FetchRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.store=Store(Path(self.tmp.name))
        self.store.update_settings(dict(brightdata_enabled=True,enabled=True,natural_enabled=False,min_interval=0))
        self.store.update_secrets({'brightdata_key':'private-test-key'})
        self.cloud=BrightData(self.store)
        self.requests=[]
        self.parameters=json.dumps(dict(url=URL,max_items=10,pages=1,filters={}))
        self.job=self.store.execute("INSERT INTO jobs(task,provider,details,request_details,created,updated) VALUES (?,?,?,?,?,?)",
            ('Поиск','brightdata',self.parameters,self.parameters,time.time(),time.time())).lastrowid

    async def asyncTearDown(self):
        await self.cloud.close()
        self.store.close()
        self.tmp.cleanup()

    def transport(self,handler,cloud=None):
        cloud=self.cloud if cloud is None else cloud
        def record(request):
            self.requests.append(request)
            return handler(request)
        cloud.http=httpx.AsyncClient(base_url='https://api.brightdata.com',transport=httpx.MockTransport(record))

    async def test_async_trigger_and_transient_poll_download_failures_never_resubmit_snapshot(self):
        counts={'progress':0,'snapshot':0}
        def handler(request):
            if request.method=='POST':
                self.assertEqual(request.url.path,'/datasets/v3/trigger')
                return httpx.Response(200,json={'snapshot_id':'sd_recovery123'})
            kind='progress' if '/progress/' in request.url.path else 'snapshot'
            counts[kind]+=1
            if counts[kind]==1:return httpx.Response(502,text='temporary gateway failure')
            return httpx.Response(200,json={'status':'ready'} if kind=='progress' else [{'page_html':HTML}])
        self.transport(handler)
        with patch('app.brightdata.asyncio.sleep',new=AsyncMock()):
            with self.cloud.job(self.job):result=await self.cloud.fetch(URL)
        self.assertEqual(result['page_html'],HTML)
        self.assertEqual(sum(r.method=='POST' for r in self.requests),1)
        self.assertEqual(self.store.settings()['brightdata_requests'],1)

    async def test_rejected_trigger_retries_are_counted_and_free_guard_applies_to_every_attempt(self):
        self.store.update_settings({'brightdata_month':datetime.now(timezone.utc).strftime('%Y-%m'),'brightdata_requests':4999})
        self.transport(lambda request:httpx.Response(502,text='temporary'))
        with patch('app.brightdata.asyncio.sleep',new=AsyncMock()):
            with self.assertRaisesRegex(ValueError,'квота'):
                with self.cloud.job(self.job):await self.cloud.fetch(URL)
        self.assertEqual(len(self.requests),1)
        self.assertEqual(self.store.settings()['brightdata_requests'],5000)

    async def test_explicit_gateway_failure_recovers_on_trigger_without_exposing_response_secrets(self):
        posts=0
        def handler(request):
            nonlocal posts
            if request.method=='POST':
                posts+=1
                return httpx.Response(503,text='private-test-key') if posts<3 else httpx.Response(200,json={'snapshot_id':'sd_trigger123'})
            return httpx.Response(200,json={'status':'ready'} if '/progress/' in request.url.path else [{'page_html':HTML}])
        self.transport(handler)
        with patch('app.brightdata.asyncio.sleep',new=AsyncMock()):
            with self.cloud.job(self.job):await self.cloud.fetch(URL)
        self.assertEqual(posts,3)
        self.assertEqual(self.store.settings()['brightdata_requests'],3)

    async def test_restart_resumes_known_snapshot_and_reuses_page_cache_without_posting_again(self):
        def failed(request):
            return httpx.Response(200,json={'snapshot_id':'sd_resume123'}) if request.method=='POST' else httpx.Response(502)
        self.transport(failed)
        with patch('app.brightdata.asyncio.sleep',new=AsyncMock()):
            with self.assertRaises(CloudUnavailable):
                with self.cloud.job(self.job):await self.cloud.fetch(URL)
        self.assertEqual(self.store.rows('SELECT snapshot_id FROM cloud_fetches')[0]['snapshot_id'],'sd_resume123')
        reopened=Store(Path(self.tmp.name));resumed=BrightData(reopened)
        def ready(request):
            self.assertEqual(request.method,'GET')
            return httpx.Response(200,json={'status':'ready'} if '/progress/' in request.url.path else [{'page_html':HTML}])
        self.transport(ready,resumed)
        try:
            with resumed.job(self.job):self.assertEqual((await resumed.fetch(URL))['page_html'],HTML)
            count=len(self.requests)
            with resumed.job(self.job):self.assertEqual((await resumed.fetch(URL))['page_html'],HTML)
            self.assertEqual(len(self.requests),count)
            self.assertEqual(sum(r.method=='POST' for r in self.requests),1)
            self.assertEqual(reopened.settings()['brightdata_requests'],1)
        finally:
            await resumed.close();reopened.close()

    async def test_unconfirmed_post_is_not_automatically_repeated(self):
        def timeout(request):raise httpx.ReadTimeout('private-test-key',request=request)
        self.transport(timeout)
        with self.assertRaises(CloudUncertain):
            with self.cloud.job(self.job):await self.cloud.fetch(URL)
        self.assertEqual(self.store.rows('SELECT status FROM cloud_fetches')[0]['status'],'uncertain')
        with self.assertRaises(ValueError):
            with self.cloud.job(self.job):await self.cloud.fetch(URL)
        self.assertEqual(len(self.requests),1)

    async def test_rate_limit_defers_the_provider_without_immediate_retries(self):
        self.transport(lambda request:httpx.Response(429,headers={'Retry-After':'60'}))
        before=time.time()
        with self.assertRaises(CloudUnavailable) as raised:
            await self.cloud.fetch(URL)
        self.assertEqual(len(self.requests),1)
        self.assertEqual(raised.exception.retry_after,60)
        self.assertGreaterEqual(self.cloud.pause_until,before+60)

    async def test_store_keeps_cloud_jobs_resumable_and_uncertain_submissions_protected(self):
        self.store.execute("UPDATE jobs SET status='running' WHERE id=?",(self.job,))
        self.store.execute("INSERT INTO cloud_fetches(job_id,url,status,updated) VALUES (?,?,'submitting',?)",(self.job,URL,time.time()))
        reopened=Store(Path(self.tmp.name))
        try:
            self.assertEqual(reopened.rows('SELECT status FROM jobs')[0]['status'],'pending')
            self.assertEqual(reopened.rows('SELECT status FROM cloud_fetches')[0]['status'],'uncertain')
        finally:reopened.close()

    async def test_runtime_resumes_job_at_start_and_persists_original_parameters(self):
        rt=Runtime(self.store)
        rt.telegram.start=AsyncMock();rt.loop=AsyncMock();rt.telegram.ready=lambda:False
        rt.websites.cloud.fetch=AsyncMock(return_value={'page_html':HTML,'final_url':URL})
        try:
            with patch('app.websites.validate_public_url',new=AsyncMock()):
                await rt.start()
                await asyncio.gather(*list(rt.work))
            row=self.store.rows('SELECT * FROM jobs WHERE id=?',(self.job,))[0]
            self.assertEqual(row['status'],'completed')
            self.assertEqual(row['request_details'],self.parameters)
            self.assertEqual(len(json.loads(row['details'])['items']),1)
        finally:await rt.stop()

    async def test_runtime_retries_transient_collection_without_sending_a_failure_message(self):
        rt=Runtime(self.store)
        rt.telegram.ready=lambda:False
        rt.websites.parse=AsyncMock(side_effect=[CloudUnavailable('502',1),{'blocked':False,'items':[],'pages':1,'text':'Пустая выдача'}])
        try:
            with patch('app.runtime.asyncio.sleep',new=AsyncMock()):
                await rt.run_website(self.job,URL,10,1,0)
            row=self.store.rows('SELECT * FROM jobs WHERE id=?',(self.job,))[0]
            self.assertEqual(row['status'],'completed')
            self.assertEqual(row['attempts'],1)
            self.assertEqual(rt.websites.parse.await_count,2)
            self.assertNotIn('не удалось',row['result'])
        finally:await rt.stop()
