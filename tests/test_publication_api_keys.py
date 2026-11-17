import json
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from app.publication_api_keys import access_record, discover_models, fingerprint, render,source_models,model_ids
from app.publications import Feed
from test_publications import PublicationFixture
from telegram_fixtures import message


PUBLIC_KEY = 'sk-public-test-only-0123456789'
ENDPOINT = 'https://api.example.org/v1'


def item(text=None, **values):
    return {'id': 1, 'url': 'https://t.me/public_api/101', 'payload': '{}',
            'text': text or f'🔑 {PUBLIC_KEY}\n\n🌐\n{ENDPOINT}\n\nМодели:\nmodel-alpha\nvendor/model-beta\n\nи ещё 2\n\n@public_api'} | values


class PublicAccessTests(unittest.IsolatedAsyncioTestCase):
    def test_english_bullets_inline_ids_and_comma_separated_models(self):
        for text,expected in [
            ('Model ID: claude-opus-5 , claude-sonnet-5-5',['claude-opus-5','claude-sonnet-5-5']),
            ('🤖 Testing models:\n• deepseek-v4.1-flash\n• glm-5.3-flash\n\n🔗 Source: API Auditor',['deepseek-v4.1-flash','glm-5.3-flash']),
            ('Бесплатно: gpt-6-sol, gpt-6-astra',['gpt-6-sol','gpt-6-astra'])]:
            self.assertEqual(source_models(text),expected)

    def test_parenthesized_base_url_has_no_trailing_closing_bracket(self):
        record=access_record(item('Base URL ('+ENDPOINT+')\nКлюч: '+PUBLIC_KEY+'\nModel ID: model-alpha'))
        self.assertEqual(record['endpoint'],ENDPOINT)
        self.assertEqual(record['models'],['model-alpha'])

    def test_model_catalog_formats_are_decoded_without_inventing_ids(self):
        self.assertEqual(model_ids({'models':[{'name':'models/gemini-real'}]}),['gemini-real'])
        self.assertEqual(model_ids({'data':{'models':['model-alpha',{'model_id':'model-beta'}]}}),['model-alpha','model-beta'])

    async def test_anthropic_header_is_tried_when_bearer_is_not_supported(self):
        def response(request):
            return httpx.Response(200,json={'data':[{'id':'claude-real'}]}) if request.headers.get('x-api-key')==PUBLIC_KEY else httpx.Response(401)
        async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as http:
            runtime=SimpleNamespace(websites=SimpleNamespace(http=http),store=SimpleNamespace(execute=lambda *args:None))
            with patch('app.publication_api_keys.validate_public_url',new=AsyncMock()):
                record=await discover_models(runtime,item(),access_record(item()),lambda:None)
        self.assertEqual(record['models'],['claude-real'])
        self.assertTrue(record['models_checked'])

    async def test_failed_old_cache_does_not_hide_newly_parsed_source_models(self):
        source=item();record=access_record(source)
        source['payload']=json.dumps({'public_api_models':{'fingerprint':fingerprint(record),'models':[],
            'checked':False,'updated':time.time()}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request:httpx.Response(403))) as http:
            runtime=SimpleNamespace(websites=SimpleNamespace(http=http),store=SimpleNamespace(execute=lambda *args:None))
            with patch('app.publication_api_keys.validate_public_url',new=AsyncMock()):
                result=await discover_models(runtime,source,record,lambda:None)
        self.assertEqual(result['models'],record['models'])
    def test_exact_public_key_endpoint_and_models_with_no_source_attribution(self):
        record = access_record(item())
        self.assertEqual(record['key'], PUBLIC_KEY)
        self.assertEqual(record['endpoint'], ENDPOINT)
        self.assertEqual(record['models'], ['model-alpha', 'vendor/model-beta'])
        self.assertNotIn('@public_api', render(record))
        self.assertIn(ENDPOINT, render(record))

    def test_signed_and_labelled_keys_are_preserved_without_truncation(self):
        key = PUBLIC_KEY + '.signed.signature_123'
        self.assertEqual(access_record(item(f'Ключ: {key}\n{ENDPOINT}'))['key'], key)

    def test_private_sources_ambiguous_endpoints_and_missing_keys_are_rejected(self):
        for source in [item(url='https://t.me/c/123/101'), item('🔑 ' + PUBLIC_KEY),
                       item('Модели: test\n' + ENDPOINT), item(item()['text'] + '\nhttps://other.example.org/v1'),
                       item('Ключ: ibsec_private_placeholder012345\n' + ENDPOINT)]:
            self.assertIsNone(access_record(source))

    async def test_models_are_read_from_declared_host_without_redirecting_the_key(self):
        requests = []
        def respond(request):
            requests.append(request)
            return httpx.Response(200, json={'data': [{'id': 'model-real'}, {'id': 'vendor/real'}]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            store = SimpleNamespace(execute=lambda *args: None)
            runtime = SimpleNamespace(websites=SimpleNamespace(http=http), store=store)
            with patch('app.publication_api_keys.validate_public_url', new=AsyncMock()):
                record = await discover_models(runtime, item(), access_record(item()), lambda: None)
        self.assertTrue(record['models_checked'])
        self.assertEqual(record['models'], ['model-real', 'vendor/real'])
        self.assertEqual(str(requests[0].url), ENDPOINT + '/models')
        self.assertEqual(requests[0].headers['authorization'], 'Bearer ' + PUBLIC_KEY)

    async def test_redirects_and_failed_model_lookups_keep_only_source_models(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(
                302, headers={'location': 'https://other.example.org/models'}))) as http:
            runtime = SimpleNamespace(websites=SimpleNamespace(http=http), store=SimpleNamespace(execute=lambda *args: None))
            with patch('app.publication_api_keys.validate_public_url', new=AsyncMock()):
                result = await discover_models(runtime, item(), access_record(item()), lambda: None)
        self.assertFalse(result['models_checked'])
        self.assertEqual(result['models'], ['model-alpha', 'vendor/model-beta'])

    def test_changed_model_catalog_does_not_duplicate_the_same_key_and_endpoint(self):
        record = access_record(item())
        self.assertEqual(fingerprint(record), fingerprint(record | {'models': ['other']}))

    def test_api_topic_requires_an_existing_route(self):
        with self.assertRaises(ValueError):
            Feed(name='API', topic='API', destination='@group_test', sources=[{'kind': 'telegram', 'value': '@public_api'}],
                 public_api_keys_topic_id=3368).checked()


class PublicAccessPublicationTests(PublicationFixture):
    async def test_general_publication_keeps_attachments_when_api_mode_is_disabled(self):
        feed=await self.feed(routes=[],topic_id=0,public_api_keys_topic_id=0)
        post_id=await self.draft(feed)
        from telegram_fixtures import document
        attachment=document(kind='file')
        with patch.object(self.pub.telegram_sources,'attachments',new=AsyncMock(return_value=[attachment])) as load:
            with patch.object(self.pub,'api_model_catalog',new=AsyncMock()) as catalog:
                result=await self.pub.publish(post_id)
        self.assertTrue(result['sent'])
        load.assert_awaited_once()
        self.client.send_file.assert_awaited_once()
        self.assertIs(self.client.send_file.call_args.args[1],attachment)
        self.client.send_message.assert_not_awaited()
        catalog.assert_not_awaited()

    async def test_long_model_catalog_is_attached_once_to_the_original_post(self):
        models=['model-'+str(i)+'-'+'a'*100 for i in range(100)]
        await self.rt.websites.http.aclose()
        self.rt.websites.http=httpx.AsyncClient(transport=httpx.MockTransport(lambda request:httpx.Response(200,json={'data':[{'id':model} for model in models]})))
        feed=await self.prepare()
        with patch('app.publication_api_keys.validate_public_url',new=AsyncMock()):
            post_id=await self.pub.make_draft(feed,lambda:None)
            result=await self.pub.publish(post_id)
            item=self.store.rows('SELECT * FROM publication_items')[0]
            from app.publication_api_keys import cached_record
            post=self.store.rows('SELECT * FROM publication_posts')[0]
            await self.pub.api_model_catalog(feed,post,item,cached_record(item),lambda:None)
        self.client.send_file.assert_awaited_once()
        self.assertEqual(self.client.send_file.call_args.kwargs['reply_to'],result['telegram_id'])
        from pathlib import Path
        content=Path(self.client.send_file.call_args.args[1]).read_text()
        self.assertTrue(all(model in content for model in models))
        self.assertNotIn(PUBLIC_KEY,content)

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.channel.forum = True
        self.client.return_value = SimpleNamespace(topics=[SimpleNamespace(id=3368, closed=False)])
        self.rt.websites.http = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={'data': [{'id': 'model-real'}]})))

    async def prepare(self):
        feed = await self.feed(routes=[{'topic_id':3368,'name':'api ключи'}], public_api_keys_topic_id=3368, include_attribution=False)
        self.pub.remember(feed, '@anime_news', {'title':'Public API','text':item()['text'],
            'url':item()['url'],'published':time.time(),'payload':{}})
        return feed

    async def test_api_draft_is_grounded_and_published_to_the_selected_topic(self):
        feed = await self.prepare()
        with patch('app.publication_api_keys.validate_public_url', new=AsyncMock()):
            post_id = await self.pub.make_draft(feed, lambda: None)
        post = self.store.rows('SELECT * FROM publication_posts WHERE id=?',(post_id,))[0]
        self.assertEqual(post['topic_id'],3368)
        self.assertIn(PUBLIC_KEY,post['text'])
        self.assertIn('model-real',post['text'])
        self.rt.model.completion.assert_not_awaited()
        result = await self.pub.publish(post_id)
        self.assertTrue(result['sent'])
        self.assertEqual(self.client.send_message.call_args.kwargs['reply_to'],3368)

    async def test_edited_or_invented_api_values_cannot_pass_source_verification(self):
        feed = await self.prepare()
        with patch('app.publication_api_keys.validate_public_url', new=AsyncMock()):
            post_id = await self.pub.make_draft(feed, lambda: None)
        self.store.execute('UPDATE publication_posts SET text=? WHERE id=?',('API-ключ: fabricated',post_id))
        result = await self.pub.publish(post_id)
        self.assertFalse(result['sent'])
        self.client.send_message.assert_not_awaited()

    async def test_sent_post_models_can_be_updated_without_sending_a_second_post(self):
        feed=await self.prepare()
        with patch('app.publication_api_keys.validate_public_url',new=AsyncMock()):
            post_id=await self.pub.make_draft(feed,lambda:None)
            await self.pub.publish(post_id)
            self.store.execute('UPDATE publication_posts SET text=? WHERE id=?',('Old model list',post_id))
            self.client.get_messages.side_effect=None
            self.client.get_messages.return_value=message(out=True)
            result=await self.pub.refresh_api_models(post_id)
        self.assertTrue(result['updated'])
        self.assertEqual(result['model_count'],1)
        self.client.edit_message.assert_awaited_once()
        self.assertEqual(self.client.send_message.await_count,1)

    async def test_unknown_models_stay_queued_without_an_empty_api_post(self):
        feed=await self.prepare()
        self.store.execute('UPDATE publication_items SET text=?',('Ключ: '+PUBLIC_KEY+'\n'+ENDPOINT,))
        await self.rt.websites.http.aclose()
        self.rt.websites.http=httpx.AsyncClient(transport=httpx.MockTransport(lambda request:httpx.Response(403)))
        with patch('app.publication_api_keys.validate_public_url',new=AsyncMock()):
            self.assertIsNone(await self.pub.make_draft(feed,lambda:None))
        self.assertEqual(self.store.rows('SELECT status FROM publication_items')[0]['status'],'new')
        self.assertEqual(self.store.rows('SELECT * FROM publication_posts'),[])
