import asyncio
import os
from unittest.mock import Mock,patch
import httpx

from app.api import app,COOKIE
from app.security import create_session
from test_publications import PublicationFixture


class PublicationApiTests(PublicationFixture):
    async def test_topic_rules_endpoint_requires_auth_and_origin(self):
        result=await self.http.post('/api/publications/topic',json={'destination':'@group_test','topic_id':38,'enabled':False},headers={'Origin':'https://attacker.example'})
        self.assertEqual(result.status_code,403);self.client.get_entity.assert_not_awaited()
        self.http.cookies.clear()
        result=await self.http.post('/api/publications/topic',json={'destination':'@group_test','topic_id':38,'enabled':False})
        self.assertEqual(result.status_code,401)

    async def test_configuration_endpoint_raises_limit_without_losing_queue(self):
        feed=await self.feed(mode='draft');post=await self.draft(feed)
        before=self.store.rows('SELECT * FROM publication_items')
        result=await self.http.post('/api/publications/configure',json={'feed_id':feed['id'],'settings':{'daily_limit':500}})
        self.assertEqual(result.status_code,200)
        self.assertEqual(self.pub.feed(feed['id'])['config']['daily_limit'],500)
        self.assertEqual(self.store.rows('SELECT * FROM publication_items'),before)
        self.assertEqual(self.store.rows('SELECT status FROM publication_posts WHERE id=?',(post,))[0]['status'],'draft')
        self.http.cookies.clear()
        result=await self.http.post('/api/publications/configure',json={'feed_id':feed['id'],'settings':{'daily_limit':500}})
        self.assertEqual(result.status_code,401)

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.origins=patch.dict(os.environ,{'PUBLIC_ORIGINS':'http://127.0.0.1:8790'});self.origins.start()
        app.state.store=self.store;app.state.runtime=self.rt
        self.token=create_session(self.store)
        self.http=httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8790',headers={'Origin':'http://127.0.0.1:8790','Content-Type':'application/json'},cookies={COOKIE:self.token})

    async def asyncTearDown(self):
        await self.http.aclose();self.origins.stop();await super().asyncTearDown()

    async def test_read_and_write_endpoints_require_auth(self):
        self.http.cookies.clear()
        self.assertEqual((await self.http.get('/api/publications')).status_code,401)
        self.assertEqual((await self.http.post('/api/publications/feeds',json=self.config)).status_code,401)
        self.assertEqual((await self.http.post('/api/publications/publish',json={'post_id':1})).status_code,401)

    async def test_cross_origin_create_does_no_network_or_database_work(self):
        r=await self.http.post('/api/publications/feeds',json=self.config,headers={'Origin':'https://attacker.example'})
        self.assertEqual(r.status_code,403);self.client.get_entity.assert_not_awaited()
        self.assertEqual(self.store.rows('SELECT * FROM publication_feeds'),[])

    async def test_create_update_list_and_stop_monitor(self):
        r=await self.http.post('/api/publications/feeds',json=self.config)
        self.assertEqual(r.status_code,200);id=r.json()['feed_id']
        data=(await self.http.get('/api/publications')).json();self.assertEqual(data['feeds'][0]['config']['topic'],'Анонсы аниме')
        update=await self.http.patch('/api/publications/feeds/'+str(id),json=self.config|{'daily_limit':2})
        self.assertEqual(update.status_code,200)
        stopped=await self.http.delete('/api/publications/feeds/'+str(id));self.assertEqual(stopped.status_code,200)
        self.assertFalse(self.pub.feed(id)['enabled'])

    async def test_invalid_config_never_reaches_telegram(self):
        r=await self.http.post('/api/publications/feeds',json=self.config|{'sources':[]})
        self.assertEqual(r.status_code,422);self.client.get_entity.assert_not_awaited()

    async def test_session_revoked_during_create_preparation_stops_save(self):
        original=self.client.get_entity.side_effect
        async def entity(v):self.store.execute('DELETE FROM sessions');return await original(v)
        self.client.get_entity.side_effect=entity
        r=await self.http.post('/api/publications/feeds',json=self.config)
        self.assertEqual(r.status_code,400);self.assertEqual(self.store.rows('SELECT * FROM publication_feeds'),[])

    async def test_draft_edit_and_manual_publication_report_confirmed_status(self):
        feed=await self.feed(mode='draft');post=await self.draft(feed)
        r=await self.http.patch('/api/publications/posts/'+str(post),json={'text':'Исправленный пост\nhttps://t.me/anime_news/101'})
        self.assertEqual(r.status_code,200)
        r=await self.http.post('/api/publications/publish',json={'post_id':post})
        self.assertTrue(r.json()['accepted']);await asyncio.gather(*list(self.rt.work))
        data=(await self.http.get('/api/publications')).json();self.assertEqual(data['posts'][0]['status'],'sent')
        self.assertEqual(data['posts'][0]['text'],'Исправленный пост\nhttps://t.me/anime_news/101')

    async def test_duplicate_publish_request_cannot_send_twice(self):
        feed=await self.feed(mode='draft');post=await self.draft(feed)
        await self.pub.publish(post)
        r=await self.http.post('/api/publications/publish',json={'post_id':post})
        self.assertEqual(r.status_code,400);self.client.send_message.assert_awaited_once()

    async def test_only_real_owner_can_manage_permanent_publications(self):
        self.rt.control.require=Mock(return_value={'sender_id':20,'contact_id':20,'id':1})
        with self.assertRaises(ValueError):await self.rt.control.action(20,1,'manage_publications',{'action':'create','config':self.config})
        self.assertEqual(self.store.rows('SELECT * FROM publication_feeds'),[])

    async def test_global_publication_switch_can_be_saved(self):
        r=await self.http.patch('/api/settings',json={'publishing_enabled':False})
        self.assertEqual(r.status_code,200);self.assertFalse((await self.http.get('/api/publications')).json()['enabled'])

    async def test_new_panel_scripts_are_available_through_existing_proxy(self):
        self.http.cookies.clear()
        r=await self.http.get('/api/telegram/panel');self.assertIn('/api/telegram/panel-assets/publications.js',r.text)
        self.assertEqual((await self.http.get('/api/telegram/panel-assets/publications.css')).status_code,200)
