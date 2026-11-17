import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock,patch

import httpx

from app.ai import ModelUnavailable
from app.api import app
from app.avito import parse_request
from app.privacy import has_location,redact_locations
from app.runtime import Runtime
from app.security import create_session
from app.store import Store

OWNER=1000000001


class LocationPrivacyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.store=Store(self.root)
        self.store.update_settings({'owner_id':OWNER})
        self.store.upsert_contact(OWNER,'Владелец');self.store.upsert_contact(10,'Другой')
        self.rt=Runtime(self.store)
        self.rt.model.memory_has_location=AsyncMock(return_value=False)

    async def asyncTearDown(self):
        await self.rt.stop();self.store.close();self.tmp.cleanup()

    def old_memory(self,contact,key,value):
        self.store.execute('INSERT INTO memories VALUES (?,?,?,?)',(contact,key,value,time.time()))

    async def test_known_cities_aliases_inflections_and_addresses_are_blocked_before_model_call(self):
        values=['Симферополь','в симферополе','Москва','по Коломне','Нурлат','Париж','место жительства',
                'я живу в Вымышленном Селе','https://www.avito.ru/simferopol/noutbuki/x_123','44.9482, 34.1001']
        for value in values:
            with self.subTest(value=value):
                result=await self.rt.action(OWNER,1,'remember',{'key':'интересы','value':value})
                self.assertFalse(result['saved'])
                with self.assertRaises(ValueError):self.store.remember(OWNER,'интересы',value)
        self.rt.model.memory_has_location.assert_not_awaited()
        self.assertEqual(self.store.rows('SELECT * FROM memories'),[])

    async def test_unknown_or_misspelled_location_is_rejected_by_semantic_memory_check(self):
        self.rt.model.memory_has_location.return_value=True
        result=await self.rt.action(OWNER,1,'remember',{'key':'факт','value':'Предпочитает Усть-Ыттхъячокк'})
        self.assertFalse(result['saved']);self.assertEqual(self.store.rows('SELECT * FROM memories'),[])
        self.rt.model.memory_has_location.assert_awaited_once()

    async def test_unavailable_privacy_check_saves_nothing_without_breaking_conversation(self):
        self.rt.model.memory_has_location.side_effect=ModelUnavailable('Недоступно')
        result=await self.rt.action(OWNER,1,'remember',{'key':'музыка','value':'Джаз'})
        self.assertFalse(result['saved']);self.assertEqual(self.store.rows('SELECT * FROM memories'),[])

    async def test_non_geographic_memory_is_saved_without_changing_other_settings(self):
        before=self.store.settings()
        result=await self.rt.action(OWNER,1,'remember',{'key':'музыка','value':'Джаз'})
        self.assertTrue(result['saved']);self.assertEqual(self.store.rows('SELECT value FROM memories')[0]['value'],'Джаз')
        self.assertEqual(self.store.settings(),before)

    async def test_city_memory_requests_do_not_turn_off_other_memory(self):
        for text in ('не запоминай города','забудь мои города','удали геоданные из памяти'):
            self.assertEqual(self.rt.control.parse([text]),[])
        self.assertTrue(self.store.settings()['memory_enabled'])

    async def test_stale_city_memories_are_hidden_immediately_and_removed_on_restart(self):
        self.old_memory(OWNER,'город','Симферополь');self.store.remember(OWNER,'музыка','Джаз')
        context=json.dumps(self.rt.model.context(OWNER),ensure_ascii=False)
        self.assertNotIn('Симферополь',context);self.assertIn('Джаз',context)
        self.store.close();self.store=Store(self.root);self.rt.store=self.store;self.rt.model.store=self.store
        self.assertEqual(self.store.rows('SELECT key,value FROM memories'),[{'key':'музыка','value':'Джаз'}])

    async def test_history_and_image_captions_hide_city_even_after_a_restore_request(self):
        self.store.message(OWNER,'user','Найди ноутбук в Симферополе',100,sender_id=OWNER,image_path='photo.png')
        self.store.execute("UPDATE messages SET image_note='На фото Симферополь' WHERE telegram_id=100")
        self.store.message(OWNER,'assistant','Вот: https://www.avito.ru/simferopol/noutbuki/x_123',101)
        self.store.message(OWNER,'user','Ну восстанови чё уж',102,sender_id=OWNER)
        context=json.dumps(self.rt.model.context(OWNER),ensure_ascii=False)
        self.assertNotIn('Симферополь',context);self.assertNotIn('/simferopol/',context)
        self.assertIn('ноутбук',context);self.assertIn('Ну восстанови',context)
        self.assertIn('Симферополе',self.store.history(OWNER)[0]['text'])
        self.assertIn('https://www.avito.ru/simferopol',self.store.history(OWNER)[1]['text'])

    async def test_all_new_messages_in_one_batch_keep_explicit_search_city_then_hide_it(self):
        first=self.store.message(OWNER,'user','Найди в Коломне ноутбук',100,sender_id=OWNER)
        self.store.message(OWNER,'user','Только до 60 тысяч',101,sender_id=OWNER)
        context=json.dumps(self.rt.model.context(OWNER,current_from=first),ensure_ascii=False)
        self.assertIn('Коломне',context);self.assertIn('60 тысяч',context)
        self.store.message(OWNER,'user','Какие города я говорил раньше?',102,sender_id=OWNER)
        self.assertNotIn('Коломне',json.dumps(self.rt.model.context(OWNER),ensure_ascii=False))
        self.assertNotIn('city',parse_request('найди на авито iphone 11'))

    async def test_proactive_reply_and_assistant_only_history_have_no_old_city(self):
        self.store.message(OWNER,'assistant','Париж — прежний поиск',100)
        self.assertNotIn('Париж',json.dumps(self.rt.model.context(OWNER),ensure_ascii=False))
        self.store.message(OWNER,'user','Москва',101,sender_id=OWNER)
        self.assertNotIn('Москва',json.dumps(self.rt.model.context(OWNER,proactive=True),ensure_ascii=False))

    async def test_history_tool_cannot_recover_city_and_cannot_read_another_contact(self):
        self.store.message(OWNER,'user','ноутбук в Симферополе PRIVATE_OWNER_SECRET',100,sender_id=OWNER)
        self.store.message(10,'user','ноутбук для игр',200,sender_id=10)
        self.assertEqual(await self.rt.action(OWNER,1,'search_history',{'query':'симферополь'}),[])
        rows=await self.rt.action(OWNER,1,'search_history',{'query':'ноутбук'})
        self.assertNotIn('Симферополе',str(rows));self.assertIn('ноутбук',str(rows))
        other=await self.rt.action(10,1,'search_history',{'query':'ноутбук','contact_id':OWNER})
        self.assertNotIn('PRIVATE_OWNER_SECRET',str(other))
        self.assertNotIn('PRIVATE_OWNER_SECRET',json.dumps(self.rt.model.context(10),ensure_ascii=False))

    async def test_owner_search_csv_is_not_listed_or_sent_to_another_contact(self):
        (self.root/'media').mkdir(exist_ok=True);(self.root/'media/listings-private.csv').write_text('city\nСимферополь\n')
        self.store.execute("INSERT INTO jobs(contact_id,task,status,created,updated,artifact) VALUES (?,'поиск','completed',?,?,'listings-private.csv')",(OWNER,time.time(),time.time()))
        self.assertEqual(await self.rt.action(10,1,'list_media',{}),[])
        self.assertEqual(self.rt.media(OWNER)[0]['filename'],'listings-private.csv')
        self.rt.telegram.send_file=AsyncMock()
        with self.assertRaises(ValueError):await self.rt.action(10,1,'send_media',{'filename':'listings-private.csv','caption':'тест'})
        self.rt.telegram.send_file.assert_not_awaited()

    async def test_forget_locations_only_cleans_current_contact(self):
        self.old_memory(OWNER,'город','Москва');self.old_memory(10,'город','Коломна')
        self.store.remember(OWNER,'музыка','Джаз')
        result=await self.rt.action(OWNER,1,'forget_locations',{})
        self.assertEqual(result['removed'],1)
        self.assertEqual(self.store.rows('SELECT value FROM memories WHERE contact_id=?',(OWNER,)),[{'value':'Джаз'}])
        self.assertEqual(self.store.rows('SELECT value FROM memories WHERE contact_id=10'),[{'value':'Коломна'}])
        with self.assertRaises(ValueError):await self.rt.action(10,1,'forget_locations',{'contact_id':OWNER})

    async def test_contact_notes_and_global_style_do_not_leak_geography(self):
        self.store.execute('UPDATE contacts SET notes=? WHERE id=?',('Владелец живёт в Симферополе',OWNER))
        self.store.update_settings({'persona':'Игривая Дарья. Собеседник из Москвы. Называй его господин.'})
        first=self.rt.model.context(10)[0]['content'];last=self.rt.model.context(10)[-1]['content']
        self.assertNotIn('Москвы',first);self.assertIn('господин',first)
        self.assertIn('Приватность городов',last);self.assertIn('Не восстанавливай',last)
        self.assertNotIn('Симферополе',json.dumps(self.rt.model.context(OWNER),ensure_ascii=False))

    async def test_preview_history_also_cannot_restore_city_and_sends_no_telegram_message(self):
        app.state.store,app.state.runtime=self.store,self.rt
        self.rt.model.completion=AsyncMock(return_value={'content':'Города прошлых запросов не сохраняю'})
        self.rt.telegram.send=AsyncMock()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test',headers={'Origin':'http://test'},cookies={'alisa_session':create_session(self.store)}) as client:
            with patch.dict('os.environ',{'PUBLIC_ORIGINS':'http://test'}):
                response=await client.post('/api/preview',json={'history':[{'role':'user','content':'Найди в Симферополе ноутбук'},{'role':'assistant','content':'Помню Москва и Симферополь'}],'text':'Ну восстанови чё уж'})
        self.assertEqual(response.status_code,200)
        context=json.dumps(self.rt.model.completion.call_args.args[0],ensure_ascii=False)
        self.assertNotIn('Симферополь',context);self.assertNotIn('Симферополе',context);self.assertNotIn('Москва',context)
        self.rt.telegram.send.assert_not_awaited()

    async def test_memory_classifier_requires_a_boolean_and_never_executes_tools(self):
        # Use the actual classifier, rather than the runtime stub.
        from app.ai import Model
        self.rt.model.memory_has_location=Model.memory_has_location.__get__(self.rt.model,Model)
        for content in ('','{}','{"contains_location":"false"}','{"contains_location":false,"extra":true}'):
            self.rt.model.completion=AsyncMock(return_value={'content':content})
            with self.assertRaises(ModelUnavailable):await self.rt.model.memory_has_location('музыка','Джаз')
        self.rt.model.completion=AsyncMock(return_value={'content':'{"contains_location":false}'})
        self.assertFalse(await self.rt.model.memory_has_location('музыка','Джаз'))
        self.assertFalse(self.rt.model.completion.call_args.kwargs['with_tools'])
        self.rt.model.completion=AsyncMock(return_value={'content':'{"contains_location":false}','tool_calls':[{}]})
        with self.assertRaises(ModelUnavailable):await self.rt.model.memory_has_location('музыка','Джаз')

    async def test_location_urls_and_cyrillic_case_forms_are_removed_without_altering_normal_preferences(self):
        text='Москва и Коломна, ищу в Нурлате. Люблю джаз. https://www.avito.ru/nizhniy_novgorod/noutbuki/x_123'
        redacted=redact_locations(text)
        for value in ('Москва','Коломна','Нурлате','nizhniy_novgorod'):self.assertNotIn(value,redacted)
        self.assertIn('Люблю джаз.',redacted);self.assertFalse(has_location('Люблю джаз.'))

    async def test_settlements_named_after_common_words_do_not_corrupt_normal_style(self):
        text='Называй меня котик. При выборе манеры держись прежнего стиля. Каждый новый ответ в том же стиле.'
        self.assertEqual(redact_locations(text),text)
        self.assertFalse(has_location(text))
