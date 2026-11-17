import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlsplit

import httpx

from app.avito import parse_request, search_url, filter_items, filters_from_url, clean_listing_url, city_slug, validate_filters
from app.runtime import Runtime
from app.security import create_session
from app.store import Store

OWNER = 1000000001
TEXT = 'найди на авито топ 5 свежих обьявлений айфон 11 по москве'
GAMING_TEXT = 'найди короче мне ноутбук на авито игровой чтоб был до 60к рублей только в симферополе и мощненький чтоб был'
GAMING_ARGS = dict(query='игровой ноутбук',city='Симферополь',max_price=60000,max_items=5,newest=False,
                   description='Подходит для игр по процессору, дискретной видеокарте и памяти',description_optional=False)


class AvitoSearchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name))
        self.store.upsert_contact(OWNER, 'Владелец')
        self.store.upsert_contact(10, 'Другой')
        self.store.update_settings({'owner_id': OWNER, 'enabled': True, 'brightdata_enabled': True, 'natural_enabled': False, 'min_interval': 0, 'burst_window': 0.01})
        self.store.update_secrets({'brightdata_key': 'test'})
        self.rt = Runtime(self.store)
        self.rt.model.run = AsyncMock()
        self.rt.model.plan_owner = AsyncMock()
        self.rt.telegram.ready = lambda: True
        self.rt.telegram.send = AsyncMock(return_value=99)

    async def asyncTearDown(self):
        await self.rt.stop()
        self.store.close()
        self.tmp.cleanup()

    async def test_exact_telegram_request_builds_moscow_phone_query_sorted_by_date(self):
        args = parse_request(TEXT)
        self.assertEqual(args, {'query': 'айфон 11', 'city': 'москве', 'max_items': 5, 'newest': True})
        url = urlsplit(search_url(args['query'], args['city'], args['newest']))
        self.assertEqual(url.path, '/moskva/telefony')
        self.assertEqual(parse_qs(url.query), {'q': ['iphone 11'], 's': ['104']})

    async def test_failed_screenshot_request_separates_plural_phone_city_and_condition(self):
        args = parse_request('найди 10 айфонов 11 свежих на авито по симферополю заблоканных')
        self.assertEqual(args,{'query':'айфон 11','city':'симферополю','max_items':10,'newest':True,
            'description':'заблокированных','description_optional':False})
        url = urlsplit(search_url(args['query'],args['city'],description=args['description']))
        self.assertEqual(url.path,'/simferopol/telefony')
        self.assertEqual(parse_qs(url.query),{'q':['iphone 11'],'s':['104']})

    async def test_owner_locked_wording_preserves_negation_and_optional_conditions(self):
        for wording,expected in [('заблоканные','заблокированные'),('заблоканных','заблокированных'),
                                 ('заблоканный','заблокированный'),('заблокан','заблокирован'),
                                 ('не заблоканный','не заблокированный'),('незаблоканные','незаблокированные'),
                                 ('с коробкой без документов','с коробкой без документов')]:
            with self.subTest(wording=wording):
                args = parse_request('найди на авито айфон 11 можно '+wording+' по всей россии')
                self.assertEqual(args['description'],expected)
                self.assertTrue(args['description_optional'])
                filters = validate_filters({'description':wording,'description_optional':False})
                self.assertEqual(filters['description'],expected)
                self.assertFalse(filters['description_optional'])

    async def test_mandatory_description_is_verified_separately_from_product_query(self):
        url = urlsplit(search_url('айфонов 11','симферополь',description='с треснутым экраном без документов'))
        self.assertEqual(parse_qs(url.query)['q'],['iphone 11'])
        self.assertEqual(url.path,'/simferopol/telefony')

    async def test_model_number_is_not_treated_as_result_count(self):
        args = parse_request('Найди на Авито айфон 11 в Москве')
        self.assertEqual(args['max_items'], 5)
        self.assertEqual(args['query'], 'айфон 11')

    async def test_screenshot_request_keeps_budget_count_and_optional_description(self):
        args = parse_request('найди дешевый айфон 11 до 8к можно чтоб он был заблокирован в симферополе недавние обьявления топ 10')
        self.assertEqual(args, {'query': 'айфон 11', 'city': 'симферополе', 'max_items': 10,
            'newest': True, 'max_price': 8000, 'description': 'заблокирован', 'description_optional': True})

    async def test_optional_descriptions_are_arbitrary_and_do_not_narrow_query(self):
        for description in ('с коробкой', 'с треснутым экраном', 'фиолетовый', 'без документов'):
            args = parse_request('найди на авито велосипед можно ' + description + ' по всей россии топ 10')
            self.assertEqual(args['description'], description)
            self.assertTrue(args['description_optional'])
            url = search_url(args['query'], args['city'], description=description, description_optional=True)
            self.assertEqual(parse_qs(urlsplit(url).query)['q'], ['велосипед'])

    async def test_default_is_russia_and_unknown_city_uses_planner(self):
        args = parse_request('найди на авито iphone 11 топ 10')
        self.assertNotIn('city', args)
        self.assertEqual(urlsplit(search_url(args['query'])).path, '/rossiya/telefony')
        self.assertEqual(urlsplit(search_url('велосипед')).path, '/rossiya')
        self.assertEqual(city_slug('Коломна'), 'kolomna')
        self.assertIsNone(parse_request('найди на авито iphone 11 в Коломне топ 10'))

    async def test_price_range_and_mandatory_description(self):
        args = parse_request('найди на авито iphone 11 от 5к до 8к с коробкой топ 10')
        self.assertEqual((args['min_price'], args['max_price']), (5000, 8000))
        self.assertEqual(args['description'], 'с коробкой')
        self.assertFalse(args['description_optional'])

    async def test_budget_and_product_are_checked_before_limit(self):
        def listing(index, title='iPhone 11', price='8000 ₽', city='moskva'):
            return {'title': title, 'price': price, 'location': city,
                    'url': f'https://www.avito.ru/{city}/telefony/phone_{index}', 'description': ''}
        items = [listing(1, price='8001 ₽'), listing(2, price=''), listing(3, price='от 5000 ₽'),
                 listing(4, title='iPhone 12'), listing(5, title='iPhone 11 Pro'),
                 listing(6, title='Чехол iPhone 11'), listing(7), listing(8, price='7 500 ₽')]
        filters = filters_from_url(search_url('iphone 11', max_price=8000))
        self.assertEqual([x['url'] for x in filter_items(items, filters)], [items[6]['url'], items[7]['url']])
        self.assertEqual(filter_items([listing(9, city='simferopol')], filters | {'city_slug': 'moskva'}), [])

    async def test_listing_links_drop_tracking(self):
        self.assertEqual(clean_listing_url('https://www.avito.ru/moskva/telefony/iphone_123?context=large#foo'),
                         'https://www.avito.ru/moskva/telefony/iphone_123')

    async def test_full_search_filters_are_persisted_and_optional_variants_have_distinct_jobs(self):
        self.store.message(OWNER, 'user', TEXT, 202, sender_id=OWNER)
        self.rt.run_website = AsyncMock()
        args = {'query': 'iphone 11', 'max_items': 10, 'max_price': 8000, 'description': 'с коробкой', 'description_optional': True}
        with patch('app.runtime.validate_public_url', new=AsyncMock()):
            first = await self.rt.control.action(OWNER, 202, 'search_avito', args, trusted=True)
            second = await self.rt.control.action(OWNER, 202, 'search_avito', args | {'description': 'с чеком'}, trusted=True)
        await asyncio.gather(*list(self.rt.work))
        self.assertNotEqual(first['job_id'], second['job_id'])
        details = json.loads(self.store.rows('SELECT details FROM jobs WHERE id=?', (first['job_id'],))[0]['details'])
        self.assertEqual(details['filters']['max_price'], 8000)
        self.assertEqual(details['filters']['city_slug'], 'all')
        self.assertTrue(details['filters']['description_optional'])

    async def test_examples_and_arbitrary_locations_do_not_trigger_or_redirect_collection(self):
        self.assertIsNone(parse_request('Например, найди на авито айфон 11 в Москве'))
        self.assertIsNone(parse_request('Ты умеешь искать на Авито?'))
        for city in ('../data', 'https://localhost', '127.0.0.1'):
            with self.assertRaises(ValueError):
                search_url('iphone 11', city)

    async def test_owner_phrase_starts_actual_job_without_model_function_calling(self):
        self.rt.run_website = AsyncMock()
        with patch('app.runtime.validate_public_url', new=AsyncMock()):
            self.rt.receive(OWNER, TEXT, 202, sender_id=OWNER)
            await asyncio.gather(*list(self.rt.work))
        self.rt.model.run.assert_not_awaited()
        self.rt.model.plan_owner.assert_not_awaited()
        self.rt.run_website.assert_awaited_once()
        jobs = self.store.rows('SELECT * FROM jobs')
        self.assertEqual(len(jobs), 1)
        self.assertEqual(json.loads(jobs[0]['details'])['max_items'], 5)
        self.assertIn('Запустила поиск', self.rt.telegram.send.call_args.args[1])

    async def test_conversational_search_uses_intent_planner_and_persists_clean_product_region_and_budget(self):
        self.rt.run_website = AsyncMock()
        self.rt.model.plan_owner.return_value = [{'name':'search_avito','args':GAMING_ARGS}]
        self.assertIsNone(self.rt.control.parse([GAMING_TEXT]))
        with patch('app.runtime.validate_public_url',new=AsyncMock()):
            self.rt.receive(OWNER,GAMING_TEXT,202,sender_id=OWNER)
            await asyncio.gather(*list(self.rt.work))
        self.rt.model.plan_owner.assert_awaited_once()
        self.assertEqual(self.rt.model.plan_owner.call_args.args[0],[GAMING_TEXT])
        job = self.store.rows('SELECT * FROM jobs')[0]
        p = json.loads(job['request_details'])
        self.assertEqual((p['filters']['city_slug'],p['filters']['max_price']),('simferopol',60000))
        url = urlsplit(p['url'])
        self.assertEqual(url.path,'/simferopol/noutbuki')
        self.assertEqual(parse_qs(url.query),{'q':['игровой ноутбук'],'pmax':['60000']})
        confirmation = self.rt.telegram.send.call_args.args[1]
        self.assertIn('60 000 ₽',confirmation)
        self.assertIn('Симферополь',confirmation)
        self.assertNotIn('новые объявления',confirmation.casefold())

    async def test_authenticated_retry_replans_complex_original_and_rejects_unrelated_actions(self):
        from app.api import app
        app.state.store,app.state.runtime = self.store,self.rt
        self.store.message(OWNER,'user',GAMING_TEXT,202,sender_id=OWNER)
        self.rt.run_website = AsyncMock()
        self.rt.model.plan_owner.return_value = [{'name':'search_avito','args':GAMING_ARGS}]
        token = create_session(self.store)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test',
                                     headers={'Origin':'http://test'},cookies={'alisa_session':token}) as client:
            with patch('app.runtime.validate_public_url',new=AsyncMock()),patch.dict('os.environ',{'PUBLIC_ORIGINS':'http://test'}):
                response = await client.post(f'/api/contacts/{OWNER}/retry-website',json={'telegram_id':202})
            self.assertEqual(response.status_code,200)
            before = len(self.store.rows('SELECT * FROM jobs'))
            self.rt.model.plan_owner.return_value = [{'name':'send_message_to','args':{'recipient':'@recipient_test','text':'привет'}}]
            with patch.dict('os.environ',{'PUBLIC_ORIGINS':'http://test'}):
                response = await client.post(f'/api/contacts/{OWNER}/retry-website',json={'telegram_id':202})
            self.assertEqual(response.status_code,400)
            self.assertEqual(len(self.store.rows('SELECT * FROM jobs')),before)
        await asyncio.gather(*list(self.rt.work))

    async def test_original_request_replay_does_not_start_second_collection(self):
        self.store.message(OWNER, 'user', TEXT, 202, sender_id=OWNER)
        self.rt.run_website = AsyncMock()
        args = parse_request(TEXT)
        with patch('app.runtime.validate_public_url', new=AsyncMock()):
            first = await self.rt.control.action(OWNER, 202, 'search_avito', args, trusted=True)
            second = await self.rt.control.action(OWNER, 202, 'search_avito', args, trusted=True)
        await asyncio.gather(*list(self.rt.work))
        self.assertEqual(first['job_id'], second['job_id'])
        self.rt.run_website.assert_awaited_once()

    async def test_disabled_cloud_returns_error_without_fake_start_confirmation(self):
        self.store.message(OWNER, 'user', TEXT, 202, sender_id=OWNER)
        self.store.update_settings({'brightdata_enabled': False})
        with self.assertRaisesRegex(ValueError, 'выключен'):
            await self.rt.control.action(OWNER, 202, 'search_avito', parse_request(TEXT), trusted=True)
        self.assertEqual(self.store.rows('SELECT * FROM jobs'), [])

    async def test_explicit_failed_job_retry_starts_once_and_pending_or_completed_is_reused(self):
        self.store.message(OWNER, 'user', TEXT, 202, sender_id=OWNER)
        self.rt.run_website = AsyncMock()
        with patch('app.runtime.validate_public_url', new=AsyncMock()):
            first = await self.rt.control.action(OWNER, 202, 'search_avito', parse_request(TEXT), trusted=True)
            self.store.execute("UPDATE jobs SET status='failed' WHERE id=?", (first['job_id'],))
            default = await self.rt.control.action(OWNER, 202, 'search_avito', parse_request(TEXT), trusted=True)
            second = await self.rt.control.action(OWNER, 202, 'search_avito', parse_request(TEXT), trusted=True, retry_failed=True)
            repeated = await self.rt.control.action(OWNER, 202, 'search_avito', parse_request(TEXT), trusted=True, retry_failed=True)
        await asyncio.gather(*list(self.rt.work))
        self.assertEqual(default['job_id'], first['job_id'])
        self.assertNotEqual(second['job_id'], first['job_id'])
        self.assertEqual(second['job_id'], repeated['job_id'])

    async def test_authenticated_retry_only_replays_original_owner_search(self):
        from app.api import app
        app.state.store, app.state.runtime = self.store, self.rt
        self.store.message(OWNER, 'user', TEXT, 202, sender_id=OWNER)
        self.store.message(OWNER, 'user', 'напиши @recipient_test привет', 203, sender_id=OWNER)
        self.store.message(10, 'user', TEXT, 202, sender_id=10)
        self.rt.run_website = AsyncMock()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1:8790',
                headers={'Origin': 'http://127.0.0.1:8790'}) as client:
            url = f'/api/contacts/{OWNER}/retry-website'
            self.assertEqual((await client.post(url, json={'telegram_id': 202})).status_code, 401)
            client.cookies.set('alisa_session', create_session(self.store))
            self.assertEqual((await client.post(url, json={'telegram_id': 203})).status_code, 400)
            self.assertEqual((await client.post('/api/contacts/10/retry-website', json={'telegram_id': 202})).status_code, 400)
            with patch('app.runtime.validate_public_url', new=AsyncMock()):
                self.assertEqual((await client.post(url, json={'telegram_id': 202})).status_code, 200)
                self.assertEqual((await client.post(url, json={'telegram_id': 202, 'city': 'all'})).status_code, 200)
                self.assertEqual((await client.post(url, json={'telegram_id': 202, 'city': 'https://localhost'})).status_code, 400)
        await asyncio.gather(*list(self.rt.work))
