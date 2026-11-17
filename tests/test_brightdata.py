import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from app.brightdata import BrightData
from app.runtime import Runtime
from app.security import create_session
from app.store import Store

URL = 'https://www.avito.ru/all/noutbuki'
HTML = '<title>Ноутбуки</title><div data-marker="item"><a data-marker="item-title" href="/moskva/noutbuki/laptop_123">Ноутбук</a><span data-marker="item-price">5900 ₽</span><span data-marker="item-address">Москва</span></div>'


class BrightDataTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name))
        self.store.update_settings({'brightdata_enabled': True})
        self.store.update_secrets({'brightdata_key': 'test-private-key'})
        self.cloud = BrightData(self.store)
        self.requests = []

    async def asyncTearDown(self):
        await self.cloud.close()
        self.store.close()
        self.tmp.cleanup()

    def transport(self, handler):
        def record(request):
            self.requests.append(request)
            self.assertEqual(request.headers['Authorization'], 'Bearer test-private-key')
            self.assertNotIn('test-private-key', str(request.url))
            return handler(request)
        self.cloud.http = httpx.AsyncClient(base_url='https://api.brightdata.com', transport=httpx.MockTransport(record))

    async def test_sync_collection_returns_html_and_reserves_one_request(self):
        def handler(request):
            self.assertEqual(json.loads(request.content), [{'url': URL}])
            return httpx.Response(200, json=[{'page_html': HTML, 'final_url': URL}])
        self.transport(handler)
        self.assertEqual((await self.cloud.fetch(URL))['page_html'], HTML)
        self.assertEqual(self.store.settings()['brightdata_requests'], 1)
        self.assertEqual(len(self.requests), 1)

    async def test_async_collection_polls_snapshot_without_reposting(self):
        progress = 0
        def handler(request):
            nonlocal progress
            if request.method == 'POST':
                return httpx.Response(202, json={'snapshot_id': 'sd_test123'})
            if '/progress/' in request.url.path:
                progress += 1
                return httpx.Response(200, json={'status': 'running' if progress == 1 else 'ready'})
            return httpx.Response(200, json=[{'page_html': HTML, 'final_url': URL}])
        self.transport(handler)
        with patch('app.brightdata.asyncio.sleep', new=AsyncMock()):
            self.assertEqual((await self.cloud.fetch(URL))['page_html'], HTML)
        self.assertEqual(sum(r.method == 'POST' for r in self.requests), 1)
        self.assertEqual(self.store.settings()['brightdata_requests'], 1)

    async def test_monthly_guard_never_submits_a_billable_request_after_limit(self):
        self.store.update_settings({'brightdata_month': datetime.now(timezone.utc).strftime('%Y-%m'), 'brightdata_requests': 5000})
        self.transport(lambda request: self.fail('No collection after free allowance'))
        with self.assertRaisesRegex(ValueError, 'квота'):
            await self.cloud.fetch(URL)
        self.assertEqual(self.requests, [])

    async def test_request_counter_resets_for_a_new_month(self):
        self.store.update_settings({'brightdata_month': '2020-01', 'brightdata_requests': 5000})
        self.transport(lambda request: httpx.Response(200, json=[{'page_html': HTML}]))
        await self.cloud.fetch(URL)
        self.assertEqual(self.store.settings()['brightdata_requests'], 1)

    async def test_payment_error_stops_without_retries_or_echoing_key(self):
        self.transport(lambda request: httpx.Response(402, text='Echoed test-private-key'))
        with self.assertRaises(ValueError) as raised:
            await self.cloud.fetch(URL)
        self.assertNotIn('test-private-key', str(raised.exception))
        self.assertEqual(len(self.requests), 1)

    async def test_disabled_provider_never_submits_a_request(self):
        self.store.update_settings({'brightdata_enabled': False})
        self.transport(lambda request: self.fail('Provider disabled'))
        with self.assertRaises(ValueError):
            await self.cloud.fetch(URL)
        self.assertEqual(self.requests, [])

    async def test_collection_failure_is_not_treated_as_html(self):
        self.transport(lambda request: httpx.Response(200, json=[{'error': 'blocked'}]))
        with self.assertRaises(ValueError):
            await self.cloud.fetch(URL)


    async def test_cloud_extraction_honors_fifty_item_selection(self):
        from app.websites import Websites
        websites = Websites(self.store)
        html = ''.join(HTML.replace('laptop_123', f'laptop_{index}') for index in range(60))
        websites.cloud.fetch = AsyncMock(return_value={'page_html': html, 'final_url': URL})
        try:
            with patch('app.websites.validate_public_url', new=AsyncMock()):
                result = await websites.parse(URL, max_items=50, cloud=True)
            self.assertEqual(len(result['items']), 50)
        finally:
            await websites.close()


class WebsiteJobTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name))
        self.store.update_settings({'brightdata_enabled': True, 'enabled': True, 'min_interval': 0, 'natural_enabled': False})
        self.store.update_secrets({'brightdata_key': 'test-private-key'})
        self.store.upsert_contact(10, 'Оля')
        self.store.upsert_contact(20, 'Саша')
        self.store.execute('UPDATE contacts SET hermes_allowed=1 WHERE id=10')
        self.rt = Runtime(self.store)
        self.rt.websites.cloud.fetch = AsyncMock(return_value={'page_html': HTML, 'final_url': URL})
        self.rt.telegram.ready = lambda: True
        self.rt.telegram.send = AsyncMock(return_value=100)
        self.rt.telegram.send_media = AsyncMock(return_value=101)
        self.validation = patch('app.runtime.validate_public_url', new=AsyncMock())
        self.validation.start()
        self.page_validation = patch('app.websites.validate_public_url', new=AsyncMock())
        self.page_validation.start()

    async def asyncTearDown(self):
        await self.rt.stop()
        self.validation.stop()
        self.page_validation.stop()
        self.store.close()
        self.tmp.cleanup()

    async def test_avito_job_exports_csv_and_delivers_only_to_requesting_contact(self):
        job = await self.rt.parse_website(URL, contact_id=10)
        await asyncio.gather(*list(self.rt.work))
        saved = self.store.rows('SELECT * FROM jobs WHERE id=?', (job['job_id'],))[0]
        self.assertEqual(saved['status'], 'completed')
        self.assertEqual(json.loads(saved['details'])['items'][0]['price'], '5900 ₽')
        content = (self.store.directory / 'media' / saved['artifact']).read_text(encoding='utf-8-sig')
        self.assertIn('Ноутбук', content)
        self.rt.telegram.send_media.assert_awaited_once()
        self.assertEqual(self.rt.telegram.send_media.call_args.args[0], 10)
        self.assertEqual(self.rt.media(20), [])

    async def test_fast_cached_search_waits_for_telegram_startup_before_delivery(self):
        gate = asyncio.Event()
        self.rt.telegram.ready = lambda:False
        async def connect():
            await gate.wait()
            self.rt.telegram.ready = lambda:True
        self.rt.startup = self.rt.spawn(connect())
        job = await self.rt.parse_website(URL,contact_id=10)
        for _ in range(50):
            if self.store.rows('SELECT status FROM jobs WHERE id=?',(job['job_id'],))[0]['status']=='completed':break
            await asyncio.sleep(0.01)
        self.assertEqual(self.store.rows('SELECT status FROM jobs WHERE id=?',(job['job_id'],))[0]['status'],'completed')
        self.rt.telegram.send.assert_not_awaited()
        self.rt.telegram.send_media.assert_not_awaited()
        gate.set()
        await asyncio.gather(*list(self.rt.work))
        self.rt.telegram.send.assert_awaited_once()
        self.rt.telegram.send_media.assert_awaited_once()
        self.assertEqual(self.rt.telegram.send_media.call_args.args[0],10)

    async def test_pending_job_is_reused_instead_of_starting_another_collection(self):
        self.rt.run_website = AsyncMock()
        first = await self.rt.submit_website(URL, contact_id=10)
        second = await self.rt.submit_website(URL, contact_id=10)
        self.assertEqual(first['job_id'], second['job_id'])
        await asyncio.gather(*list(self.rt.work))
        self.rt.run_website.assert_awaited_once()

    async def test_ten_filtered_results_are_all_delivered_and_exported(self):
        html = ''.join(HTML.replace('laptop_123', f'laptop_{index}').replace('Ноутбук', 'iPhone 11' + ' x' * 90)
                       .replace('5900 ₽', '9000 ₽' if index < 6 else '7900 ₽') for index in range(16))
        self.rt.websites.cloud.fetch.return_value = {'page_html': html, 'final_url': URL}
        job = await self.rt.parse_website(URL, max_items=10, contact_id=10, filters={'max_price': 8000})
        await asyncio.gather(*list(self.rt.work))
        saved = self.store.rows('SELECT * FROM jobs WHERE id=?', (job['job_id'],))[0]
        self.assertEqual(saved['status'], 'completed')
        data = json.loads(saved['details'])
        self.assertEqual(len(data['items']), 10)
        self.assertEqual(data['filters']['max_price'], 8000)
        sent = '\n'.join(call.args[1] for call in self.rt.telegram.send.call_args_list)
        self.assertIn('10. iPhone 11', sent)
        self.assertNotIn('9000', sent)
        self.assertTrue(all(len(call.args[1]) <= 4000 for call in self.rt.telegram.send.call_args_list))
        content = (self.store.directory / 'media' / saved['artifact']).read_text(encoding='utf-8-sig')
        self.assertEqual(content.count('7900'), 10)

    async def test_optional_description_keeps_items_without_description(self):
        self.rt.websites.match_listings = AsyncMock()
        result = await self.rt.websites.parse(URL, cloud=True, filters={'description': 'с коробкой', 'description_optional': True})
        self.assertEqual(len(result['items']), 1)
        self.rt.websites.match_listings.assert_not_awaited()
        self.assertEqual(self.rt.websites.cloud.fetch.await_count, 1)

    async def test_mandatory_description_uses_only_actual_detail_text(self):
        detail_url = 'https://www.avito.ru/moskva/noutbuki/laptop_123'
        self.rt.websites.cloud.fetch.side_effect = [
            {'page_html': HTML, 'final_url': URL},
            {'page_html': '<div data-marker="item-view/item-description">С коробкой, чек сохранён.</div>', 'final_url': detail_url}]
        self.rt.websites.match_listings = AsyncMock(side_effect=lambda items, description: items)
        result = await self.rt.websites.parse(URL, cloud=True, filters={'description': 'с коробкой', 'description_optional': False})
        self.assertEqual(result['items'][0]['description'], 'С коробкой, чек сохранён.')
        self.assertEqual(result['detail_pages'], 1)
        self.rt.websites.match_listings.assert_awaited_once()

    async def test_unverified_mandatory_items_do_not_create_csv_or_leak_unfiltered_page(self):
        self.rt.websites.match_listings = AsyncMock(return_value=[])
        job = await self.rt.parse_website(URL, max_items=10, contact_id=10, filters={'description': 'с коробкой'})
        await asyncio.gather(*list(self.rt.work))
        saved = self.store.rows('SELECT * FROM jobs WHERE id=?', (job['job_id'],))[0]
        self.assertEqual(saved['status'], 'completed')
        self.assertFalse(saved['artifact'])
        self.assertNotIn('5900', saved['result'])
        self.assertIn('Не найдено', saved['result'])

    async def test_semantic_match_requires_quotes_from_existing_listing_and_no_native_tools(self):
        items = [{'title': 'Ноутбук', 'description': 'С коробкой, чек сохранён.'}]
        self.rt.model.completion = AsyncMock(return_value={'content': json.dumps({'matches': [{'index': 0, 'evidence': 'С коробкой'}]})})
        self.assertEqual(await self.rt.model.match_listings(items, 'с коробкой'), items)
        self.assertFalse(self.rt.model.completion.call_args.kwargs['with_tools'])
        self.rt.model.listing_checks.clear()
        for path in (self.store.directory/'listing-checks').glob('*.json'):
            path.unlink()
        for response in ({'content': '{"matches":[{"index":0,"evidence":"Выдуманная цитата"}]}'},
                         {'content': '{"matches":[{"index":5,"evidence":"С коробкой"}]}'},
                         {'tool_calls': [{'function': {'name': 'bash'}}]}):
            self.rt.model.completion.return_value = response
            with self.assertRaises(ValueError):
                await self.rt.model.match_listings(items, 'с коробкой')

    async def test_contact_without_permission_cannot_start_a_collection(self):
        with self.assertRaises(ValueError):
            await self.rt.submit_website(URL, contact_id=20)
        self.rt.websites.cloud.fetch.assert_not_awaited()

    async def test_failed_collection_never_exports_fake_listings(self):
        self.rt.websites.cloud.fetch.side_effect = ValueError('Bright Data отказал в доступе')
        job = await self.rt.submit_website(URL)
        await asyncio.gather(*list(self.rt.work))
        saved = self.store.rows('SELECT * FROM jobs WHERE id=?', (job['job_id'],))[0]
        self.assertEqual(saved['status'], 'failed')
        self.assertFalse(saved['artifact'])
        self.assertEqual(self.rt.media(), [])

    async def test_panel_submission_returns_a_job_and_results_can_be_polled(self):
        from app.api import app
        app.state.store, app.state.runtime = self.store, self.rt
        with patch.dict('os.environ', {'PUBLIC_ORIGINS': 'http://test'}):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test', headers={'Origin': 'http://test'},
                                        cookies={'alisa_session': create_session(self.store)}) as client:
                response = await client.post('/api/websites/parse', json={'url': URL, 'max_items': 5})
                self.assertEqual(response.status_code, 200)
                self.assertIn('job_id', response.json())
                await asyncio.gather(*list(self.rt.work))
                jobs = (await client.get('/api/websites/jobs')).json()
                self.assertEqual(jobs[0]['status'], 'completed')
                self.assertEqual(len(jobs[0]['data']['items']), 1)
                self.assertNotIn('test-private-key', json.dumps(jobs))
                client.cookies.clear()
                self.assertEqual((await client.get('/api/websites/jobs')).status_code, 401)
