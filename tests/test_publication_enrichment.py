import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from app.publication_enrichment import enrich,evidence
from app.store import Store


class EnrichmentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=Store(Path(self.tmp.name))
        self.feed=self.store.execute('INSERT INTO publication_feeds(name,config,target_id,next_check,created,updated) VALUES (?,?,?,?,?,?)',
                                    ('Test','{}',-100,0,time.time(),time.time())).lastrowid
        self.runtime=SimpleNamespace(store=self.store,research=SimpleNamespace(read=AsyncMock(return_value={
            'url':'https://example.org/article','title':'Полный разбор','text':'Проверяемые факты статьи. '*20})))

    def tearDown(self):self.store.close();self.tmp.cleanup()

    def item(self,index=1):
        id=self.store.execute('''INSERT INTO publication_items(feed_id,source,url,title,text,fingerprint,published,observed,payload,content_hash)
            VALUES (?,?,?,?,?,?,?,?,?,?)''',(self.feed,'@source','https://t.me/source/'+str(index),'Короткий заголовок','Короткий заголовок',str(index),time.time(),time.time(),
            json.dumps({'links':['https://example.org/article']}),'original-hash')).lastrowid
        return self.store.rows('SELECT * FROM publication_items WHERE id=?',(id,))[0]

    async def test_page_evidence_is_cached_without_changing_material_identity(self):
        item=self.item();await enrich(self.runtime,[item],lambda i:json.loads(i['payload'])['links'],lambda:None)
        after=self.store.rows('SELECT * FROM publication_items WHERE id=?',(item['id'],))[0]
        self.assertEqual(after['text'],'Короткий заголовок');self.assertEqual(after['content_hash'],'original-hash')
        self.assertIn('Проверяемые факты статьи',evidence(after))
        await enrich(self.runtime,[after],lambda i:json.loads(i['payload'])['links'],lambda:None)
        self.runtime.research.read.assert_awaited_once()

    async def test_failed_site_is_deferred_without_consuming_the_material(self):
        self.runtime.research.read.side_effect=ValueError('HTTP 503');item=self.item()
        await enrich(self.runtime,[item],lambda i:['https://example.org/article'],lambda:None)
        after=self.store.rows('SELECT * FROM publication_items WHERE id=?',(item['id'],))[0]
        self.assertEqual(after['status'],'new');self.assertGreater(json.loads(after['payload'])['enrichment_retry_after'],time.time())

    async def test_each_pass_limits_external_fetches(self):
        items=[self.item(i) for i in range(5)]
        await enrich(self.runtime,items,lambda i:['https://example.org/article'],lambda:None)
        self.assertEqual(self.runtime.research.read.await_count,3)

    async def test_substantive_original_text_can_still_be_used_when_link_is_down(self):
        self.runtime.research.read.side_effect=ValueError('HTTP 503');item=self.item()
        item['text']='Конкретные условия бесплатного тарифа и инструкция подключения. '*7
        await enrich(self.runtime,[item],lambda i:['https://example.org/article'],lambda:None)
        self.runtime.research.read.assert_not_awaited()
        self.assertIn('Конкретные условия',evidence(item))

    async def test_revoked_scope_cannot_save_fetched_data(self):
        active=True
        def guard():
            if not active:raise ValueError('Revoked')
        async def read(*args):
            nonlocal active
            active=False;return {'url':'https://example.org/article','text':'text '*100}
        self.runtime.research.read.side_effect=read;item=self.item()
        with self.assertRaises(ValueError):await enrich(self.runtime,[item],lambda i:['https://example.org/article'],guard)
        self.assertNotIn('linked_pages',json.loads(self.store.rows('SELECT payload FROM publication_items')[0]['payload']))
