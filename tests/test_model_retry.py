import tempfile
import json
import unittest
import time
from email.utils import formatdate
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from app.ai import Model
from app.store import Store


class ModelRetryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.store=Store(Path(self.tmp.name));self.store.update_secrets({'ai_key':'test-key'})
        self.store.upsert_contact(10,'Оля');self.model=Model(self.store)
        await self.model.http.aclose()
        self.calls=[]

    async def asyncTearDown(self):
        await self.model.close();self.store.close();self.tmp.cleanup()

    def transport(self, responses):
        def handler(request):
            self.calls.append(request)
            result=responses[min(len(self.calls)-1,len(responses)-1)]
            if isinstance(result,Exception):
                raise result
            return result
        self.model.http=httpx.AsyncClient(transport=httpx.MockTransport(handler))

    def answer(self,content='Привет'):
        return httpx.Response(200,json={'choices':[{'message':{'content':content},'finish_reason':'stop'}]})

    async def test_transient_503_recovers_without_changing_connection(self):
        before=self.store.settings()
        self.transport([httpx.Response(503),self.answer()])
        with patch('app.ai.asyncio.sleep',new_callable=AsyncMock):
            response=await self.model.completion([{'role':'user','content':'Привет'}])
        self.assertEqual(response['content'],'Привет');self.assertEqual(len(self.calls),2)
        self.assertEqual(self.store.settings(),before)

    async def test_temporary_failure_stops_after_three_attempts(self):
        self.transport([httpx.Response(503)])
        with patch('app.ai.asyncio.sleep',new_callable=AsyncMock):
            with self.assertRaisesRegex(ValueError,'503'):
                await self.model.completion([])
        self.assertEqual(len(self.calls),3)

    async def test_temporary_gateway_errors_are_not_reported_as_bad_credentials(self):
        for status in (408,500,502,504):
            await self.model.http.aclose()
            self.calls.clear()
            self.transport([httpx.Response(status)])
            with patch('app.ai.asyncio.sleep',new_callable=AsyncMock):
                with self.assertRaisesRegex(ValueError,str(status)) as error:
                    await self.model.completion([])
            self.assertNotIn('Проверьте ключ',str(error.exception))
            self.assertEqual(len(self.calls),3)

    async def test_invalid_credentials_are_not_retried(self):
        self.transport([httpx.Response(401)])
        with self.assertRaisesRegex(ValueError,'401'):
            await self.model.completion([])
        self.assertEqual(len(self.calls),1)

    async def test_long_retry_after_is_not_ignored(self):
        for header in ['60',formatdate(time.time()+60,usegmt=True)]:
            await self.model.http.aclose();self.calls.clear()
            self.transport([httpx.Response(429,headers={'Retry-After':header})])
            with patch('app.ai.asyncio.sleep',new_callable=AsyncMock) as sleep:
                with self.assertRaisesRegex(ValueError,'429'):
                    await self.model.completion([])
            sleep.assert_not_awaited();self.assertEqual(len(self.calls),1)

    async def test_transport_failure_can_recover(self):
        self.transport([httpx.ConnectError('Temporary network error'),self.answer()])
        with patch('app.ai.asyncio.sleep',new_callable=AsyncMock):
            self.assertEqual((await self.model.completion([]))['content'],'Привет')
        self.assertEqual(len(self.calls),2)

    async def test_failed_200_response_cannot_be_sent_as_a_reply(self):
        self.transport([httpx.Response(200,json={'choices':[{'message':{'content':'private-provider-detail'},'finish_reason':'error'}]})])
        with self.assertRaisesRegex(ValueError,'не завершил') as error:
            await self.model.completion([])
        self.assertNotIn('private-provider-detail',str(error.exception))

    async def test_retry_does_not_execute_a_tool_twice(self):
        tool=httpx.Response(200,json={'choices':[{'message':{'content':None,'tool_calls':[{'id':'call-1','type':'function','function':{'name':'remember','arguments':'{"key":"music","value":"rock"}'}}]},'finish_reason':'tool_calls'}]})
        self.transport([httpx.Response(503),tool,self.answer('Запомнила')])
        execute=AsyncMock(return_value={'ok':True})
        with patch('app.ai.asyncio.sleep',new_callable=AsyncMock):
            result=await self.model.run(10,execute)
        self.assertEqual(result,'Запомнила');execute.assert_awaited_once()

    async def test_proactive_skip_can_finish_without_a_text_message(self):
        tool=httpx.Response(200,json={'choices':[{'message':{'tool_calls':[{'id':'skip-1','type':'function','function':{'name':'skip_message','arguments':'{}'}}]}}]})
        self.transport([tool,self.answer('')])
        self.assertEqual(await self.model.run(10,AsyncMock(),proactive=True),'')

    async def test_json_request_uses_structured_output_without_changing_chat_temperature(self):
        before = self.store.settings()
        self.transport([self.answer('{"reply":true}')])
        await self.model.completion([],with_tools=False,temperature=0,json_object=True)
        payload=json.loads(self.calls[0].content)
        self.assertEqual(payload['response_format'],{'type':'json_object'})
        self.assertEqual(payload['temperature'],0)
        self.assertNotIn('tools',payload)
        self.assertEqual(self.store.settings(),before)

    async def test_reasoning_budget_cannot_swallow_a_chat_answer(self):
        clipped=httpx.Response(200,json={'choices':[{'message':{'content':''},'finish_reason':'length'}],
            'usage':{'completion_tokens_details':{'reasoning_tokens':700}}})
        self.transport([clipped,self.answer('Вот ответ')])
        response=await self.model.completion([])
        self.assertEqual(response['content'],'Вот ответ')
        self.assertEqual([json.loads(call.content)['max_tokens'] for call in self.calls],[700,8192])

    async def test_truncated_json_is_never_returned_to_a_decision_parser(self):
        clipped=httpx.Response(200,json={'choices':[{'message':{'content':'{"reply":'},'finish_reason':'length'}],
            'usage':{'completion_tokens_details':{'reasoning_tokens':700}}})
        self.transport([clipped])
        with self.assertRaisesRegex(ValueError,'не завершила'):
            await self.model.completion([],with_tools=False,json_object=True)
        self.assertEqual(len(self.calls),3)

    async def test_tool_call_is_not_retried_even_if_reasoning_reaches_the_limit(self):
        call={'id':'call-1','type':'function','function':{'name':'remember','arguments':'{}'}}
        self.transport([httpx.Response(200,json={'choices':[{'message':{'tool_calls':[call]},'finish_reason':'length'}],
            'usage':{'completion_tokens_details':{'reasoning_tokens':700}}})])
        self.assertEqual((await self.model.completion([]))['tool_calls'],[call])
        self.assertEqual(len(self.calls),1)
