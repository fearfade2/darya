import asyncio
import json
import time
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock,patch

import httpx
from telethon import types,errors

from app.publications import Feed,PublicationStopped
from app.store import Store
from telegram_fixtures import TelegramFixture,NOW,GROUP,SELF,message


class PublicationFixture(TelegramFixture):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.pub=self.rt.publications
        self.rt.research.read=AsyncMock(return_value={'text':'Полный текст тестового материала. '*10})
        self.store.update_settings({'publishing_enabled':True})
        self.news=types.Channel(200,'Новости аниме',types.ChatPhotoEmpty(),NOW,access_hash=321,broadcast=True,username='anime_news')
        self.destination=types.Channel(300,'Мой канал',types.ChatPhotoEmpty(),NOW,access_hash=321,broadcast=True,username='my_channel',admin_rights=types.ChatAdminRights(post_messages=True))
        original=self.client.get_entity.side_effect
        async def entity(v):
            if v in {'@anime_news','anime_news'}:return self.news
            if v in {'@my_channel','my_channel'}:return self.destination
            return await original(v)
        self.client.get_entity.side_effect=entity
        self.client.get_messages.return_value=[message(101,text='Студия объявила новый сезон аниме. Премьера осенью.')]
        self.config={'name':'Новости аниме','topic':'Анонсы аниме','destination':'@group_test','sources':[{'kind':'telegram','value':'@anime_news'}],
                     'include_attribution':True}

    async def feed(self,**kwargs):
        saved=await self.pub.save(self.config|kwargs)
        return self.pub.feed(saved['feed_id'])

    async def draft(self,feed):
        guard=lambda:self.pub.active(feed)
        await self.pub.collect(feed,guard)
        id=self.store.rows('SELECT id FROM publication_items ORDER BY id DESC LIMIT 1')[0]['id']
        self.replies({'publish':True,'text':'Новый сезон уже анонсирован. Премьеру обещают осенью!','source_ids':[id]})
        post=await self.pub.make_draft(feed,guard)
        return post

class PublicationTests(PublicationFixture):
    async def test_missing_required_draft_fields_are_repaired_before_any_write(self):
        feed=await self.feed(mode='draft',include_attribution=False)
        await self.pub.collect(feed,lambda:None)
        calls=0
        async def model(messages,**kwargs):
            nonlocal calls
            calls+=1
            self.assertEqual(self.store.rows('SELECT * FROM publication_posts'),[])
            self.assertEqual(self.store.rows('SELECT status FROM publication_items')[0]['status'],'new')
            data={'publish':True,'text':'Объявлен новый сезон аниме.','source_ids':[1]}
            if calls==2:
                self.assertIn('validation_error',messages[-1]['content'])
                data['links']=[]
            return {'content':json.dumps(data)}
        self.rt.model.completion.side_effect=model
        post=await self.pub.make_draft(feed,lambda:None)
        self.assertIsNotNone(post);self.assertEqual(calls,2)
        self.client.send_message.assert_not_awaited()

    async def test_incomplete_draft_json_is_repaired_with_same_sources(self):
        feed=await self.feed(mode='draft');await self.pub.collect(feed,lambda:None)
        self.rt.model.completion.side_effect=[{'content':'{"publish":true,"text":'},
            {'content':json.dumps({'publish':True,'text':'Объявлен новый сезон аниме.','source_ids':[1]})}]
        self.assertIsNotNone(await self.pub.make_draft(feed,lambda:None))
        self.assertIn('"id": 1',self.rt.model.completion.call_args.args[0][1]['content'])
        self.client.send_message.assert_not_awaited()

    async def test_two_invalid_draft_responses_retain_queue_without_a_post(self):
        feed=await self.feed(mode='draft');await self.pub.collect(feed,lambda:None)
        self.replies({'publish':True},{'publish':True})
        with self.assertRaises(ValueError):await self.pub.make_draft(feed,lambda:None)
        self.assertEqual(self.rt.model.completion.await_count,2)
        self.assertEqual(self.store.rows('SELECT * FROM publication_posts'),[])
        self.assertEqual(self.store.rows('SELECT status FROM publication_items')[0]['status'],'new')

    async def test_authority_revocation_during_draft_repair_prevents_write(self):
        feed=await self.feed(mode='draft');await self.pub.collect(feed,lambda:None)
        calls=0
        async def model(*args,**kwargs):
            nonlocal calls
            calls+=1
            if calls==1:return {'content':'not json'}
            self.store.update_settings({'publishing_enabled':False})
            return {'content':json.dumps({'publish':True,'text':'Новость','source_ids':[1]})}
        self.rt.model.completion.side_effect=model
        with self.assertRaises(PublicationStopped):await self.pub.make_draft(feed,lambda:self.pub.active(feed))
        self.assertEqual(self.store.rows('SELECT * FROM publication_posts'),[])
        self.client.send_message.assert_not_awaited()

    async def test_removed_source_cannot_leave_publishable_material_in_queue(self):
        feed=await self.feed(mode='draft');await self.draft(feed)
        await self.pub.configure({'settings':{'sources':[{'kind':'telegram','value':'@other_news'}]},'feed_id':feed['id']})
        self.assertEqual(self.store.rows('SELECT status FROM publication_items')[0]['status'],'skipped')
        self.assertEqual(self.store.rows('SELECT status FROM publication_posts')[0]['status'],'discarded')

    async def test_limit_500_preserves_queue_cursors_routes_and_draft(self):
        feed=await self.feed(mode='draft',new_only=True)
        post=await self.draft(feed)
        self.store.execute('INSERT INTO publication_source_cursors VALUES (?,?,?,?,?)',(feed['id'],'@anime_news',200,101,time.time()))
        items=self.store.rows('SELECT * FROM publication_items')
        cursors=self.store.rows('SELECT * FROM publication_source_cursors')
        await self.pub.configure({'settings':{'daily_limit':500},'feed_id':feed['id']})
        after=self.pub.feed(feed['id'])
        self.assertEqual(after['config'],feed['config']|{'daily_limit':500})
        self.assertEqual(self.store.rows('SELECT * FROM publication_items'),items)
        self.assertEqual(self.store.rows('SELECT * FROM publication_source_cursors'),cursors)
        draft=self.store.rows('SELECT * FROM publication_posts WHERE id=?',(post,))[0]
        self.assertEqual(draft['status'],'draft');self.assertEqual(draft['feed_revision'],after['revision'])
        await self.pub.publish(post)
        self.client.send_message.assert_awaited_once()

    async def test_group_patch_is_atomic_and_retained_for_future_sources(self):
        first=await self.feed();second=await self.feed(name='Второй источник')
        before=self.store.rows('SELECT * FROM publication_feeds ORDER BY id')
        self.store.execute("INSERT INTO publication_posts(feed_id,feed_revision,target_id,text,source_ids,fingerprint,status,created) VALUES (?,?,?,?,?,?,?,?)",
            (second['id'],second['revision'],second['target_id'],'text','[]','sending','dispatch',time.time()))
        with self.assertRaises(ValueError):await self.pub.configure({'settings':{'daily_limit':500},'destination':'@group_test'})
        self.assertEqual(self.store.rows('SELECT * FROM publication_feeds ORDER BY id'),before)
        self.store.execute('DELETE FROM publication_posts')
        result=await self.pub.configure({'settings':{'daily_limit':500},'destination':'@group_test'})
        self.assertEqual(set(result['feed_ids']),{first['id'],second['id']})
        self.assertEqual(self.pub.telegram_sources.policy(first['target_id']),{'daily_limit':500})

    async def test_content_patch_discards_only_draft_and_retains_material_queue(self):
        feed=await self.feed(mode='draft');post=await self.draft(feed)
        items=self.store.rows('SELECT * FROM publication_items')
        await self.pub.configure({'settings':{'instructions':'Очень кратко'},'feed_id':feed['id']})
        self.assertEqual(self.store.rows('SELECT * FROM publication_items'),items)
        self.assertEqual(self.store.rows('SELECT status FROM publication_posts WHERE id=?',(post,))[0]['status'],'discarded')

    async def test_monitor_collects_new_public_posts_and_deduplicates_polls(self):
        feed=await self.feed(mode='draft')
        await self.pub.collect(feed,lambda:None);await self.pub.collect(feed,lambda:None)
        rows=self.store.rows('SELECT * FROM publication_items')
        self.assertEqual(len(rows),1);self.assertEqual(rows[0]['url'],'https://t.me/anime_news/101')
        self.assertEqual(self.store.history(10),[])

    async def test_auto_mode_creates_grounded_post_and_publishes_once(self):
        feed=await self.feed()
        self.replies({'publish':True,'text':'Объявлен новый сезон аниме. Премьера осенью!','source_ids':[1]})
        self.assertTrue((await self.pub.check(feed['id']))['checked'])
        row=self.store.rows('SELECT * FROM publication_posts')[0]
        self.assertEqual(row['status'],'sent');self.assertEqual(row['telegram_id'],500)
        self.assertIn('Источники:\nhttps://t.me/anime_news/101',row['text'])
        self.client.send_message.assert_awaited_once()
        await self.pub.check(feed['id']);self.client.send_message.assert_awaited_once()

    async def test_two_monitors_cannot_repost_the_same_article_to_one_destination(self):
        first=await self.feed(mode='draft');post=await self.draft(first);await self.pub.publish(post)
        second=await self.feed(mode='draft',name='Ещё один монитор');await self.pub.collect(second,lambda:None)
        item=self.store.rows('SELECT id FROM publication_items WHERE feed_id=?',(second['id'],))[0]['id']
        self.replies({'publish':True,'text':'Новый комментарий об этом сезоне','source_ids':[item]})
        self.assertIsNone(await self.pub.make_draft(second,lambda:None))
        self.client.send_message.assert_awaited_once()

    async def test_an_already_published_whole_digest_cannot_be_replayed_as_parts(self):
        feed=await self.feed(mode='draft');post=await self.draft(feed);await self.pub.publish(post)
        item=self.store.rows('SELECT * FROM publication_items')[0]
        self.pub.remember(feed,item['source'],{'url':item['url'],'text':'Отдельная новость',
            'payload':{'part_id':'part-one','links':['https://example.com/project']}})
        part=self.store.rows('SELECT id FROM publication_items ORDER BY id DESC LIMIT 1')[0]['id']
        self.replies({'publish':True,'text':'Отдельная новость','source_ids':[part]})
        self.assertIsNone(await self.pub.make_draft(feed,lambda:None))
        self.client.send_message.assert_awaited_once()

    async def test_draft_mode_never_sends_until_owner_publishes(self):
        feed=await self.feed(mode='draft');post=await self.draft(feed)
        self.client.send_message.assert_not_awaited()
        await self.pub.publish(post,manual=True);self.client.send_message.assert_awaited_once()
        again=await self.pub.publish(post,manual=True)
        self.assertTrue(again['already_sent']);self.client.send_message.assert_awaited_once()

    async def test_model_gets_only_public_sources_and_no_private_history(self):
        self.store.message(10,'user','Моя личная тайна',1,sender_id=10)
        feed=await self.feed(mode='draft');await self.draft(feed)
        context=str(self.rt.model.completion.call_args)
        self.assertIn('новый сезон',context);self.assertNotIn('Моя личная тайна',context)

    async def test_unknown_source_ids_and_invented_links_cannot_be_published(self):
        feed=await self.feed(mode='draft');await self.pub.collect(feed,lambda:None)
        for data in [{'publish':True,'text':'Новость','source_ids':[99]}, {'publish':True,'text':'Смотри https://attacker.example','source_ids':[1]}]:
            self.replies(data)
            with self.assertRaises(ValueError):await self.pub.make_draft(feed,lambda:None)
        self.assertEqual(self.store.rows('SELECT * FROM publication_posts'),[])
        self.client.send_message.assert_not_awaited()

    async def test_editor_can_skip_irrelevant_material(self):
        feed=await self.feed();self.replies({'publish':False})
        await self.pub.check(feed['id'])
        self.assertEqual(self.store.rows('SELECT status FROM publication_items')[0]['status'],'skipped')
        self.client.send_message.assert_not_awaited()

    async def test_disable_during_generation_stops_before_draft_or_send(self):
        feed=await self.feed()
        async def model(*args,**kwargs):
            self.store.update_settings({'publishing_enabled':False})
            return {'content':'{"publish":true,"text":"Новость","source_ids":[1]}'}
        self.rt.model.completion.side_effect=model
        result=await self.pub.check(feed['id'])
        self.assertFalse(result['checked']);self.assertEqual(self.store.rows('SELECT * FROM publication_posts'),[])
        self.client.send_message.assert_not_awaited()

    async def test_revision_change_during_preparation_stops_delivery(self):
        feed=await self.feed(mode='draft');post=await self.draft(feed)
        original=self.client.get_entity.side_effect
        async def entity(v):
            self.store.execute('UPDATE publication_feeds SET revision=revision+1 WHERE id=?',(feed['id'],))
            return await original(v)
        self.client.get_entity.side_effect=entity
        with self.assertRaises(PublicationStopped):await self.pub.publish(post)
        self.client.send_message.assert_not_awaited()

    async def test_lost_acknowledgment_stays_uncertain_and_is_not_retried(self):
        feed=await self.feed(mode='draft');post=await self.draft(feed)
        self.client.send_message.side_effect=ConnectionError('lost acknowledgment')
        with self.assertRaises(ConnectionError):await self.pub.publish(post)
        self.assertEqual(self.store.rows('SELECT status FROM publication_posts')[0]['status'],'uncertain')
        with self.assertRaises(ValueError):await self.pub.publish(post)
        self.client.send_message.assert_awaited_once()
        await self.pub.execute({'action':'discard','post_id':post})
        self.assertEqual(self.store.rows('SELECT status FROM publication_items')[0]['status'],'used')

    async def test_floodwait_defers_unsent_post_without_spending_daily_limit(self):
        feed=await self.feed(mode='draft');post=await self.draft(feed)
        self.client.send_message.side_effect=errors.FloodWaitError(None,10)
        with self.assertRaises(errors.FloodWaitError):await self.pub.publish(post)
        row=self.store.rows('SELECT * FROM publication_posts')[0]
        self.assertEqual(row['status'],'draft');self.assertEqual(row['dispatched'],0)
        self.assertGreater(self.rt.flood_until,time.time())

    async def test_daily_limit_is_enforced_for_all_posts_in_feed(self):
        feed=await self.feed(mode='draft',daily_limit=1);post=await self.draft(feed)
        await self.pub.publish(post)
        other=self.store.execute('''INSERT INTO publication_posts(feed_id,feed_revision,target_id,text,source_ids,fingerprint,created)
            VALUES (?,?,?,'Вторая новость','[1]','another',?)''',(feed['id'],feed['revision'],feed['target_id'],time.time())).lastrowid
        with self.assertRaises(PublicationStopped):await self.pub.publish(other)
        self.client.send_message.assert_awaited_once()

    async def test_contact_disabled_while_waiting_for_send_lock_cannot_receive_post(self):
        feed=await self.feed(mode='draft');post=await self.draft(feed)
        await self.rt.send_lock.acquire()
        entered=asyncio.Event();original=self.pub.target
        async def target(*args,**kwargs):
            result=await original(*args,**kwargs);entered.set();return result
        self.pub.target=target
        task=asyncio.create_task(self.pub.publish(post))
        await entered.wait();self.store.execute('UPDATE contacts SET enabled=0 WHERE id=?',(GROUP,));self.rt.send_lock.release()
        with self.assertRaises(PublicationStopped):await task
        self.client.send_message.assert_not_awaited()

    async def test_concurrent_posts_cannot_bypass_daily_limit(self):
        feed=await self.feed(mode='draft',daily_limit=1);post=await self.draft(feed)
        other=self.store.execute("""INSERT INTO publication_posts(feed_id,feed_revision,target_id,text,source_ids,fingerprint,created)
            VALUES (?,?,?,'Вторая новость','[1]','other-concurrent',?)""",(feed['id'],feed['revision'],feed['target_id'],time.time())).lastrowid
        results=await asyncio.gather(self.pub.publish(post),self.pub.publish(other),return_exceptions=True)
        self.assertEqual(sum(isinstance(r,PublicationStopped) for r in results),1)
        self.client.send_message.assert_awaited_once()

    async def test_private_user_cannot_be_publication_destination(self):
        with self.assertRaises(ValueError):await self.feed(destination='@olga_test')
        self.assertEqual(self.store.rows('SELECT * FROM publication_feeds'),[])

    async def test_channel_requires_post_permission(self):
        self.destination.admin_rights=None
        with self.assertRaises(ValueError):await self.feed(destination='@my_channel')
        self.client.send_message.assert_not_awaited()

    async def test_owned_channel_is_valid_destination(self):
        feed=await self.feed(destination='@my_channel',mode='draft');post=await self.draft(feed)
        await self.pub.publish(post,manual=True)
        self.assertEqual(self.client.send_message.call_args.args[0].id,300)

    async def test_protected_channels_and_private_chats_are_not_sources(self):
        self.news.noforwards=True
        with self.assertRaises(ValueError):await self.rt.research.channel('@anime_news')
        with self.assertRaises(ValueError):await self.rt.research.channel('@olga_test')
        self.client.get_messages.assert_not_awaited()

    async def test_protected_post_is_not_collected(self):
        m=message(101,text='Запрещённое копирование');m.noforwards=True
        self.client.get_messages.return_value=[m]
        feed=await self.feed();await self.pub.collect(feed,lambda:None)
        self.assertEqual(self.store.rows('SELECT * FROM publication_items'),[])

    async def test_feed_edit_invalidates_old_draft_and_old_source_candidates(self):
        feed=await self.feed(mode='draft');post=await self.draft(feed)
        await self.pub.save(self.config|{'topic':'Другие новости'},feed['id'])
        with self.assertRaises(ValueError):await self.pub.publish(post)
        self.assertEqual(self.store.rows('SELECT status FROM publication_posts')[0]['status'],'discarded')
        self.assertEqual(self.store.rows('SELECT status FROM publication_items')[0]['status'],'skipped')

    async def test_restart_recovers_inflight_post_as_uncertain(self):
        feed=await self.feed(mode='draft');post=await self.draft(feed)
        self.store.execute("UPDATE publication_posts SET status='dispatch' WHERE id=?",(post,))
        recovered=Store(self.store.directory)
        self.assertEqual(recovered.rows('SELECT status FROM publication_posts')[0]['status'],'uncertain');recovered.close()

    async def test_quiet_hours_block_background_but_manual_draft_send_is_allowed(self):
        feed=await self.feed(mode='draft');post=await self.draft(feed)
        self.store.update_settings({'enabled':False})
        with self.assertRaises(PublicationStopped):await self.pub.publish(post)
        await self.pub.publish(post,manual=True);self.client.send_message.assert_awaited_once()

    async def test_scheduler_queues_one_due_feed_and_persists_next_check(self):
        feed=await self.feed();self.replies({'publish':False})
        self.pub.tick();self.assertEqual(len(self.pub.workers),1)
        await asyncio.gather(*list(self.pub.workers.values()))
        self.assertGreater(self.pub.feed(feed['id'])['next_check'],time.time())

    async def test_disabled_scheduler_does_no_network_work(self):
        await self.feed();self.store.update_settings({'publishing_enabled':False});self.pub.tick()
        self.assertEqual(self.pub.workers,{});self.client.get_messages.assert_not_awaited()

    async def test_invalid_monitor_config_is_rejected(self):
        for values in [{'sources':[]},{'sources':[{'kind':'web','value':'file:///etc/passwd'}]},{'interval_minutes':1},{'sources':[{'kind':'telegram','value':'t.me/+invite'}]}]:
            with self.assertRaises(ValueError):Feed.model_validate(self.config|values).checked()


class ResearchTests(TelegramFixture):
    async def test_web_read_extracts_article_and_omits_scripts_and_navigation(self):
        def respond(request):return httpx.Response(200,headers={'Content-Type':'text/html'},text='<title>Новость</title><nav>Меню</nav><article>Новый сезон осенью<script>secret code</script></article>')
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            self.rt.research.http=client
            with patch('app.research.validate_public_url',AsyncMock()):r=await self.rt.research.read('https://news.example/article')
        self.assertEqual(r['text'],'Новый сезон осенью');self.assertEqual(r['title'],'Новость')

    async def test_redirects_revalidate_destination_before_request(self):
        requests=[]
        def respond(request):requests.append(str(request.url));return httpx.Response(302,headers={'Location':'https://127.0.0.1/private'})
        async def validate(url):
            if '127.0.0.1' in url:raise ValueError('Private destination')
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            self.rt.research.http=client
            with patch('app.research.validate_public_url',side_effect=validate):
                with self.assertRaises(ValueError):await self.rt.research.read('https://news.example')
        self.assertEqual(len(requests),1)

    async def test_rss_and_atom_links_and_timestamps_are_read(self):
        from app.research import WebResearch
        rss=b'<rss><channel><item><title>News</title><link>https://news.example/a</link><description>Actual facts</description><pubDate>Sun, 08 Nov 2026 12:00:00 GMT</pubDate></item></channel></rss>'
        atom=b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>News</title><link href="https://news.example/a"/><summary>Actual facts</summary><updated>2026-11-08T12:00:00Z</updated></entry></feed>'
        for raw in [rss,atom]:
            result=WebResearch.feed(raw,'https://news.example/feed');self.assertEqual(result[0]['url'],'https://news.example/a');self.assertGreater(result[0]['published'],0)

    async def test_search_falls_back_to_rss_and_returns_actual_urls(self):
        def respond(request):
            if 'duckduckgo.com' in request.url.host:return httpx.Response(403)
            return httpx.Response(200,headers={'Content-Type':'application/rss+xml'},text='<rss><channel><item><title>Новость аниме</title><link>https://news.example/a</link><description>Анонс сезона</description></item></channel></rss>')
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            self.rt.research.http=client
            with patch('app.research.validate_public_url',AsyncMock()):r=await self.rt.research.search('новости аниме')
        self.assertEqual(r['items'][0]['url'],'https://news.example/a')

    async def test_source_urls_with_credentials_are_rejected(self):
        from app.research import public_link
        for url in ['https://user:password@news.example','https://news.example/?api_key=secret','http://news.example','file:///etc/passwd']:
            with self.assertRaises(ValueError):public_link(url)

    async def test_autonomy_can_read_public_web_without_sending(self):
        self.autonomy();self.rt.research.search=AsyncMock(return_value={'items':[{'url':'https://news.example/a','title':'Новости'}]})
        self.replies({'focus_id':0,'reason':'Почитать новости'},{'action':'web_search','query':'аниме'},{'done':True})
        await self.auto.consider();self.rt.research.search.assert_awaited_once();self.client.send_message.assert_not_awaited()
