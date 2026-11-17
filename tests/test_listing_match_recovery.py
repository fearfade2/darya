import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from app.ai import Model, ModelUnavailable
from app.runtime import Runtime
from app.store import Store


class ListingMatchRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name))
        self.model = Model(self.store)
        self.items = [{'title':'iPhone 11','description':'Заблокирован iCloud, на запчасти.'}]
        self.good = {'content':json.dumps({'matches':[{'index':0,'evidence':'Заблокирован iCloud'}]})}

    async def asyncTearDown(self):
        await self.model.close()
        self.store.close()
        self.tmp.cleanup()

    async def test_malformed_answer_retries_without_losing_verified_matches(self):
        self.model.completion = AsyncMock(side_effect=[{'content':'{"matches":['},self.good])
        self.assertEqual(await self.model.match_listings(self.items,'заблокирован'),self.items)
        self.assertEqual(self.model.completion.await_count,2)
        self.assertTrue(all(c.kwargs['with_tools'] is False for c in self.model.completion.call_args_list))

    async def test_unverifiable_quote_is_never_accepted_and_remains_retryable(self):
        self.model.completion = AsyncMock(return_value={'content':'{"matches":[{"index":0,"evidence":"Без блокировки"}]}'})
        with self.assertRaises(ModelUnavailable):
            await self.model.match_listings(self.items,'заблокирован')
        self.assertEqual(self.model.completion.await_count,2)

    async def test_correct_empty_result_finishes_without_retry(self):
        self.model.completion = AsyncMock(return_value={'content':'{"matches":[]}'})
        self.assertEqual(await self.model.match_listings(self.items,'с чеком'),[])
        self.model.completion.assert_awaited_once()

    async def test_native_commands_are_rejected_without_retry(self):
        self.model.completion = AsyncMock(return_value={'tool_calls':[{'function':{'name':'send_message_to'}}]})
        with self.assertRaises(ValueError):
            await self.model.match_listings(self.items,'заблокирован')
        self.model.completion.assert_awaited_once()

    async def test_structured_budget_is_independent_of_owner_reply_length_and_sent_to_provider(self):
        self.store.update_settings({'reply_max_tokens':30})
        self.store.update_secrets({'ai_key':'test-secret'})
        requests = []
        def handler(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200,json={'choices':[{'message':self.good,'finish_reason':'stop'}]})
        await self.model.http.aclose()
        self.model.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.assertEqual(await self.model.match_listings(self.items,'заблокирован'),self.items)
        self.assertEqual(requests[0]['max_tokens'],4096)
        self.assertNotIn('tools',requests[0])
        self.assertEqual(self.store.settings()['reply_max_tokens'],30)

    async def test_no_listings_need_no_model_call(self):
        self.model.completion = AsyncMock()
        self.assertEqual(await self.model.match_listings([],'заблокирован'),[])
        self.model.completion.assert_not_awaited()

    async def test_qualitative_matching_keeps_verified_model_ranking_without_requiring_adjective_in_text(self):
        items = [{'title':'Ноутбук A','description':'Core i5, RTX 3050, 16 ГБ'},
                 {'title':'Ноутбук B','description':'Ryzen 7, RTX 3060, 16 ГБ'}]
        self.model.completion = AsyncMock(return_value={'content':json.dumps({'matches':[
            {'index':1,'evidence':'Ryzen 7, RTX 3060, 16 ГБ'},
            {'index':0,'evidence':'Core i5, RTX 3050, 16 ГБ'}]})})
        self.assertEqual(await self.model.match_listings(items,'мощный для игр'),[items[1],items[0]])

    async def test_separate_specification_quotes_confirm_facts_spread_through_the_description(self):
        items = [{'title':'ASUS TUF', 'description':'Процессор Intel Core i5-12500H. ' + 'Подробности. '*30 +
                  'Видеокарта RTX 3050. ' + 'Комплектация. '*30 + 'Память 16 ГБ DDR4.'}]
        self.model.completion = AsyncMock(return_value={'content':json.dumps({'matches':[
            {'index':0,'evidence':['Intel Core i5-12500H','Видеокарта RTX 3050','Память 16 ГБ DDR4']} ]})})
        self.assertEqual(await self.model.match_listings(items,'подходит для игр по процессору, видеокарте и памяти'),items)

    async def test_one_fabricated_specification_invalidates_the_match_even_with_other_valid_quotes(self):
        self.model.completion = AsyncMock(return_value={'content':json.dumps({'matches':[
            {'index':0,'evidence':['Заблокирован iCloud','Коробка и чек']} ]})})
        with self.assertRaises(ModelUnavailable):
            await self.model.match_listings(self.items,'заблокирован с коробкой и чеком')

    async def test_empty_or_invalid_evidence_lists_do_not_count_as_verified(self):
        for evidence in ([],[''],['Заблокирован iCloud',None],['Заблокирован iCloud']*9):
            with self.subTest(evidence=evidence):
                self.model.completion = AsyncMock(return_value={'content':json.dumps({'matches':[
                    {'index':0,'evidence':evidence}]})})
                with self.assertRaises(ModelUnavailable):
                    await self.model.match_listings(self.items,'заблокирован')

    async def test_source_quote_references_preserve_obfuscated_specs_without_retyping_them(self):
        description='Процессор Intеl Соrе i5‑12500Н. ' + 'Подробности. '*30 + 'Графика NVIDIА GеFоrсе RТХ 3050. Память 16 ГБ.'
        items=[{'title':'ASUS TUF','description':description,'price':'53500 ₽'}]
        async def answer(messages,**kwargs):
            rows=json.loads(messages[1]['content'])['listings']
            quotes=rows[0]['quotes']
            self.assertEqual(''.join(quotes[1:]),description)
            self.assertEqual(rows[0]['price'],'53500 ₽')
            return {'content':json.dumps({'matches':[{'index':0,'evidence':[1,len(quotes)-1]}]})}
        self.model.completion=AsyncMock(side_effect=answer)
        self.assertEqual(await self.model.match_listings(items,'для игр'),items)

    async def test_invalid_quote_references_cannot_confirm_an_advertisement(self):
        for evidence in ([-1],[2],[True],[0,999]):
            with self.subTest(evidence=evidence):
                self.model.completion=AsyncMock(return_value={'content':json.dumps({'matches':[
                    {'index':0,'evidence':evidence}]})})
                with self.assertRaises(ModelUnavailable):
                    await self.model.match_listings(self.items,'заблокирован')

    async def test_temporarily_malformed_provider_response_remains_retryable(self):
        self.store.update_secrets({'ai_key':'test-secret'})
        await self.model.http.aclose()
        self.model.http = httpx.AsyncClient(transport=httpx.MockTransport(lambda request:httpx.Response(200,json={})))
        with self.assertRaises(ModelUnavailable):
            await self.model.match_listings(self.items,'заблокирован')

    async def test_runtime_resumes_invalid_match_check_from_cached_pages_without_another_collection(self):
        self.store.update_settings({'brightdata_enabled':True})
        self.store.update_secrets({'brightdata_key':'test-secret'})
        rt = Runtime(self.store)
        rt.telegram.ready = lambda:False
        rt.model.completion = AsyncMock(side_effect=[{'content':''},{'content':'broken JSON'},self.good])
        url = 'https://www.avito.ru/simferopol/telefony?q=iphone+11'
        filters = {'query':'iphone 11','city_slug':'simferopol','description':'заблокирован','description_optional':False}
        parameters = json.dumps(dict(url=url,max_items=1,pages=1,filters=filters))
        job = self.store.execute("INSERT INTO jobs(task,provider,details,request_details,created,updated) VALUES (?,?,?,?,?,?)",('Поиск','brightdata',parameters,parameters,time.time(),time.time())).lastrowid
        requests = []
        def handler(request):
            requests.append(request)
            if request.method == 'POST':
                target = json.loads(request.content)[0]['url']
                return httpx.Response(200,json={'snapshot_id':'sd_main' if target==url else 'sd_detail'})
            if '/progress/' in request.url.path:
                return httpx.Response(200,json={'status':'ready'})
            html = ('<title>Авито</title><div data-marker="item"><a data-marker="item-title" href="/simferopol/telefony/iphone_11_123">iPhone 11</a></div>'
                    if request.url.path.endswith('sd_main') else '<title>iPhone 11</title><div data-marker="item-view/item-description">Заблокирован iCloud, на запчасти.</div>')
            return httpx.Response(200,json=[{'page_html':html}])
        rt.websites.cloud.http = httpx.AsyncClient(base_url='https://api.brightdata.com',transport=httpx.MockTransport(handler))
        try:
            with patch('app.websites.validate_public_url',new=AsyncMock()),patch('app.runtime.asyncio.sleep',new=AsyncMock()):
                await rt.run_website(job,url,1,1,0,filters=filters)
            saved = self.store.rows('SELECT * FROM jobs WHERE id=?',(job,))[0]
            self.assertEqual(saved['status'],'completed')
            self.assertEqual(saved['attempts'],1)
            self.assertEqual(saved['last_error'],'')
            self.assertEqual(saved['error_source'],'')
            self.assertEqual(len(json.loads(saved['details'])['items']),1)
            self.assertEqual(sum(r.method=='POST' for r in requests),2)
            self.assertEqual(self.store.settings()['brightdata_requests'],2)
        finally:
            await rt.stop()

    async def test_large_result_is_checked_in_small_groups_and_reused_after_model_restart(self):
        items=[{'title':f'Товар {i}','description':'С коробкой'} for i in range(13)]
        sizes=[]
        async def answer(messages,**kwargs):
            rows=json.loads(messages[1]['content'])['listings'];sizes.append(len(rows))
            return {'content':json.dumps({'matches':[{'index':r['index'],'evidence':[0,1]} for r in rows]})}
        self.model.completion=AsyncMock(side_effect=answer)
        self.assertEqual(await self.model.match_listings(items,'с коробкой'),items)
        self.assertEqual(sorted(sizes),[1,6,6])
        replacement=Model(self.store);replacement.completion=AsyncMock(side_effect=AssertionError('Проверка уже сохранена'))
        try:self.assertEqual(await replacement.match_listings(items,'с коробкой'),items)
        finally:await replacement.close()

    async def test_optional_ranking_failure_keeps_all_verified_groups(self):
        items=[{'title':f'Ноутбук {i}','description':'RTX 3060, 16 ГБ'} for i in range(7)]
        async def answer(messages,**kwargs):
            payload=json.loads(messages[1]['content'])
            if 'listings' not in payload:raise ModelUnavailable('Сортировка недоступна')
            return {'content':json.dumps({'matches':[{'index':i,'evidence':[1]} for i in range(len(payload['listings']))]})}
        self.model.completion=AsyncMock(side_effect=answer)
        self.assertEqual(await self.model.match_listings(items,'мощный для игр'),items)

    async def test_changed_listing_content_cannot_reuse_previous_verification(self):
        self.model.completion=AsyncMock(return_value=self.good)
        await self.model.match_listings(self.items,'заблокирован')
        changed=[self.items[0]|{'description':'Без блокировки'}]
        self.model.completion=AsyncMock(return_value=self.good)
        with self.assertRaises(ModelUnavailable):await self.model.match_listings(changed,'заблокирован')
        self.assertEqual(self.model.completion.await_count,2)

    async def test_global_ranking_compares_later_groups_and_survives_restart(self):
        items=[{'title':f'Ноутбук {i}','description':f'RTX {i}, 16 ГБ'} for i in range(13)]
        sizes=[]
        async def answer(messages,**kwargs):
            rows=json.loads(messages[1]['content'])['items'];sizes.append(len(rows))
            self.assertFalse(kwargs['with_tools'])
            self.assertEqual(kwargs['max_tokens'],8192)
            return {'content':json.dumps({'scores':[{'index':r['index'],'score':int(r['title'].split()[-1])+30} for r in reversed(rows)]})}
        self.model.completion=AsyncMock(side_effect=answer)
        self.assertEqual(await self.model.rank_listings(items,'для игр'),items[::-1])
        self.assertEqual(sorted(sizes),[1,6,6])
        replacement=Model(self.store);replacement.completion=AsyncMock(side_effect=AssertionError('Оценки уже сохранены'))
        try:self.assertEqual(await replacement.rank_listings(items,'для игр'),items[::-1])
        finally:await replacement.close()

    async def test_ranking_reads_specs_at_the_end_and_changed_specs_invalidate_cache(self):
        items=[{'title':'Ноутбук','description':'Комплектация. '*100+'RTX 3060, 16 ГБ'}]
        async def answer(messages,**kwargs):
            self.assertTrue(json.loads(messages[1]['content'])['items'][0]['description'].endswith('RTX 3060, 16 ГБ'))
            return {'content':'{"scores":[{"index":0,"score":75}]}'}
        self.model.completion=AsyncMock(side_effect=answer)
        await self.model.rank_listings(items,'для игр')
        self.model.completion=AsyncMock(return_value={'content':'{"scores":[{"index":0,"score":45}]}'})
        self.assertEqual(await self.model.rank_listings([items[0]|{'description':'GTX 1050'}],'для игр'),[items[0]|{'description':'GTX 1050'}])
        self.model.completion.assert_awaited_once()

    async def test_ranking_rejects_incomplete_duplicate_out_of_range_and_boolean_scores(self):
        items=[{'title':'A'},{'title':'B'}]
        for scores in ([{'index':0,'score':70}], [{'index':0,'score':70}]*2,
                       [{'index':0,'score':101},{'index':1,'score':70}],
                       [{'index':False,'score':70},{'index':1,'score':70}],
                       [{'index':0,'score':True},{'index':1,'score':70}]):
            with self.subTest(scores=scores):
                self.model.completion=AsyncMock(return_value={'content':json.dumps({'scores':scores})})
                with self.assertRaises(ValueError):await self.model.rank_listings(items,'для игр')

    async def test_successful_rank_groups_survive_another_group_failure(self):
        items=[{'title':str(i)} for i in range(7)]
        async def answer(messages,**kwargs):
            rows=json.loads(messages[1]['content'])['items']
            if rows[0]['title']=='6':raise ModelUnavailable('Пауза')
            return {'content':json.dumps({'scores':[{'index':r['index'],'score':50} for r in rows]})}
        self.model.completion=AsyncMock(side_effect=answer)
        with self.assertRaises(ModelUnavailable):await self.model.rank_listings(items,'для игр')
        self.model.completion=AsyncMock(return_value={'content':'{"scores":[{"index":0,"score":75}]}'})
        self.assertEqual(await self.model.rank_listings(items,'для игр'),[items[6]]+items[:6])
        self.model.completion.assert_awaited_once()
