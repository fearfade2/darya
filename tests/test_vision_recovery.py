import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock

import httpx
from PIL import Image

from app.ai import Model, ModelUnavailable
from app.store import Store
from app.vision import save_image


class VisionRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name))
        self.store.upsert_contact(10,'Оля')
        self.store.upsert_contact(20,'Саша')
        self.store.update_secrets({'ai_key':'test-key'})
        raw=io.BytesIO();Image.new('RGB',(100,80),'red').save(raw,'PNG')
        self.path=save_image(self.store.directory,raw.getvalue())
        self.photo=self.store.message(10,'user','Что видно?',101,image_path=self.path)
        self.model=Model(self.store)

    async def asyncTearDown(self):
        await self.model.close()
        self.store.close()
        self.tmp.cleanup()

    def saved(self):
        return self.store.rows('SELECT image_note,image_check FROM messages WHERE id=?',(self.photo,))[0]

    async def test_vision_has_its_own_provider_budget_even_with_short_chat_replies(self):
        self.store.update_settings({'reply_max_tokens':30,'vision_model':'configured-vision'})
        requests=[]
        def handler(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200,json={'choices':[{'message':{'content':'Красный прямоугольник'}}]})
        await self.model.http.aclose()
        self.model.http=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        await self.model.prepare_images(10)
        self.assertEqual(requests[0]['max_tokens'],4096)
        self.assertEqual(requests[0]['model'],'configured-vision')
        self.assertNotIn('tools',requests[0])
        self.assertEqual(self.store.settings()['reply_max_tokens'],30)
        self.assertIn('Красный прямоугольник',json.dumps(self.model.context(10),ensure_ascii=False))

    async def test_empty_vision_response_remains_retryable_instead_of_becoming_a_cached_failure(self):
        self.model.completion=AsyncMock(side_effect=[{'content':'','reasoning_content':'x'*5000},
            {'content':'Красный прямоугольник'}])
        with self.assertRaises(ModelUnavailable):await self.model.prepare_images(10)
        self.assertEqual(self.saved()['image_note'],'')
        await self.model.prepare_images(10)
        self.assertEqual(self.saved()['image_note'],'Красный прямоугольник')
        self.assertEqual(self.model.completion.await_count,2)
        self.assertNotIn('x'*100,json.dumps(self.model.context(10)))

    async def test_transient_provider_errors_propagate_to_the_existing_reply_retry_queue(self):
        self.model.completion=AsyncMock(side_effect=ModelUnavailable('API модели вернул код 429',retry_after=60))
        with self.assertRaises(ModelUnavailable) as raised:await self.model.prepare_images(10)
        self.assertEqual(raised.exception.retry_after,60)
        self.assertEqual(self.saved()['image_note'],'')
        self.assertEqual(self.saved()['image_check'],'')

    async def test_old_cached_failure_is_recovered_once_and_never_uses_other_contacts_images(self):
        other=self.store.message(20,'user','Чужое фото',102,image_path=self.path)
        self.store.execute("UPDATE messages SET image_note='Просмотр фото не удался: Модель не описала изображение'")
        self.model.completion=AsyncMock(return_value={'content':'Красный прямоугольник'})
        await self.model.prepare_images(10)
        await self.model.prepare_images(10)
        self.model.completion.assert_awaited_once()
        self.assertEqual(self.saved()['image_note'],'Красный прямоугольник')
        self.assertTrue(self.store.rows('SELECT image_note FROM messages WHERE id=?',(other,))[0]['image_note'].startswith('Просмотр фото не удался:'))

    async def test_permanent_failure_waits_for_changed_credentials_or_model(self):
        self.model.completion=AsyncMock(side_effect=ValueError('API модели вернул код 401'))
        await self.model.prepare_images(10)
        await self.model.prepare_images(10)
        self.model.completion.assert_awaited_once()
        self.assertTrue(self.saved()['image_check'])
        self.store.update_secrets({'ai_key':'changed-test-key'})
        self.model.completion=AsyncMock(return_value={'content':'Красный прямоугольник'})
        await self.model.prepare_images(10)
        self.assertEqual(self.saved()['image_note'],'Красный прямоугольник')
        self.model.completion.assert_awaited_once()

    async def test_restart_keeps_successful_image_notes_without_another_provider_call(self):
        self.model.completion=AsyncMock(return_value={'content':'Красный прямоугольник'})
        await self.model.prepare_images(10)
        replacement=Model(self.store)
        replacement.completion=AsyncMock(side_effect=AssertionError('Описание уже сохранено'))
        try:
            await replacement.prepare_images(10)
            replacement.completion.assert_not_awaited()
        finally:await replacement.close()
