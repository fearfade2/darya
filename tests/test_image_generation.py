import asyncio
import base64
import io
import json
from pathlib import Path
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from PIL import Image

from app.dialogs import current_dialog
from app.image_generation import parse_generation_request
from tests import test_core as core
from tests import telegram_fixtures as tg


def png():
    output = io.BytesIO()
    Image.new('RGB', (32, 32), 'red').save(output, 'PNG')
    return output.getvalue()


def image_response():
    return httpx.Response(200, json={'data': [{'b64_json': base64.b64encode(png()).decode()}]})


class ImageGenerationTests(tg.TelegramFixture):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.store.update_settings({'ai_base_url':'https://api.example.com/v1','ai_model':'chat-model',
                                    'image_generation_model':'image-model','presence_enabled':False})
        self.store.update_secrets({'ai_key':'main-test-key','ai_key_host':'api.example.com'})
        self.requests = []
        async def handler(request):
            self.requests.append(request)
            return image_response()
        await self.mock_http(handler)
        self.client.upload_file = AsyncMock(return_value='uploaded-image')

    async def mock_http(self, handler):
        await self.rt.model.http.aclose()
        self.rt.model.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    def source(self, chat=10, sender=10, id=55):
        self.store.message(chat, 'user', 'Даша нарисуй кота', id, sender_id=sender)

    async def test_separate_model_uses_images_api_and_leaves_chat_model_unchanged(self):
        data = await self.rt.image_generator.generate('кот')
        request = self.requests[0]
        self.assertEqual(request.url.path, '/v1/images/generations')
        self.assertEqual(json.loads(request.content), {'model':'image-model','prompt':'кот','n':1})
        self.assertEqual(request.headers['authorization'], 'Bearer main-test-key')
        self.assertEqual(Image.open(io.BytesIO(data)).format, 'JPEG')
        self.assertEqual(self.store.settings()['ai_model'], 'chat-model')

    async def test_separate_provider_needs_its_own_key(self):
        self.store.update_settings({'image_generation_base_url':'https://images.example.com/v1'})
        with self.assertRaisesRegex(ValueError, 'API-ключ'):
            await self.rt.image_generator.generate('кот')
        self.assertEqual(self.requests, [])

    async def test_separate_key_is_bound_to_provider_origin(self):
        self.store.update_settings({'image_generation_base_url':'https://images.example.com/v1'})
        self.store.update_secrets({'image_generation_key':'image-test-key',
                                  'image_generation_key_origin':['https','images.example.com',443]})
        await self.rt.image_generator.generate('кот')
        self.assertEqual(self.requests[0].headers['authorization'], 'Bearer image-test-key')
        self.store.update_settings({'image_generation_base_url':'https://other.example.com/v1'})
        with self.assertRaisesRegex(ValueError, 'API-ключ'):
            await self.rt.image_generator.generate('кот')
        self.assertEqual(len(self.requests), 1)

    async def test_chat_generation_reads_openrouter_image_result(self):
        async def handler(request):
            payload = json.loads(request.content)
            self.assertEqual(request.url.path, '/v1/chat/completions')
            self.assertEqual(payload['model'], 'image-model')
            self.assertEqual(payload['modalities'], ['image','text'])
            return httpx.Response(200,json={'choices':[{'message':{'images':[{'image_url':{
                'url':'data:image/png;base64,'+base64.b64encode(png()).decode()}}]}}]})
        await self.mock_http(handler)
        data = await self.rt.image_generator.generate('кот',overrides={'api_mode':'chat'})
        self.assertEqual(Image.open(io.BytesIO(data)).format, 'JPEG')

    async def test_public_image_download_does_not_receive_api_key(self):
        async def handler(request):
            if request.method == 'POST':
                return httpx.Response(200,json={'data':[{'url':'https://cdn.example.com/image.png?signature=test'}]})
            self.assertFalse(request.headers.get('authorization'))
            return httpx.Response(200,content=png())
        await self.mock_http(handler)
        with patch('app.image_generation.validate_public_url',new=AsyncMock()) as validate:
            await self.rt.image_generator.generate('кот')
        validate.assert_awaited_once_with('https://cdn.example.com/image.png?signature=test')

    async def test_api_errors_do_not_echo_keys_and_generation_is_not_retried(self):
        async def handler(request):
            self.requests.append(request)
            return httpx.Response(401,json={'error':{'message':'main-test-key'}})
        await self.mock_http(handler)
        with self.assertRaises(ValueError) as error:
            await self.rt.image_generator.generate('кот')
        self.assertNotIn('main-test-key',str(error.exception))
        self.assertEqual(len(self.requests),1)

    async def test_timeout_is_not_repeated_in_same_reply(self):
        async def handler(request):
            self.requests.append(request)
            raise httpx.ReadTimeout('timeout',request=request)
        await self.mock_http(handler)
        self.rt.reply_states[10]={'media_sent':False}
        for _ in range(2):
            with self.assertRaises(ValueError):
                await self.rt.action(10,55,'generate_image',{'prompt':'кот'})
        self.assertEqual(len(self.requests),1)
        self.client.send_file.assert_not_awaited()

    async def test_delivers_in_private_chat_and_removes_temporary_file(self):
        self.source()
        self.rt.reply_states[10]={'media_sent':False}
        result = await self.rt.action(10,55,'generate_image',{'prompt':'кот'})
        self.assertTrue(result['sent'])
        self.assertEqual(self.client.send_file.call_args.args[:2],(10,'uploaded-image'))
        self.assertEqual(self.client.send_file.call_args.kwargs['reply_to'],55)
        self.assertEqual(list((self.store.directory/'generated').iterdir()),[])
        self.assertTrue(self.rt.reply_states[10]['media_sent'])
        with self.assertRaises(ValueError):
            await self.rt.action(10,55,'generate_image',{'prompt':'ещё кот'})
        self.assertEqual(len(self.requests),1)

    async def test_delivers_to_group_request_without_global_permission(self):
        self.store.group_message(tg.GROUP,33,'Тема',sender_id=10,topic_id=33)
        self.store.group_message(tg.GROUP,55,'Даша нарисуй кота',sender_id=10,topic_id=33)
        self.source(tg.GROUP)
        token = current_dialog.set(self.store.dialog_scope(tg.GROUP,10))
        try:
            result = await self.rt.action(tg.GROUP,55,'generate_image',{'prompt':'кот'})
        finally:
            current_dialog.reset(token)
        self.assertTrue(result['sent'])
        self.assertEqual(self.client.send_file.call_args.args[0],tg.GROUP)
        self.assertEqual(self.client.send_file.call_args.kwargs['reply_to'],55)
        self.assertEqual(self.store.group_topic(tg.GROUP,55),33)
        self.assertEqual(self.store.rows('SELECT * FROM controller_permissions WHERE user_id=10'),[])

    async def test_disabled_during_generation_prevents_delivery(self):
        async def handler(request):
            self.store.update_settings({'enabled':False})
            return image_response()
        await self.mock_http(handler)
        with self.assertRaisesRegex(ValueError,'выключено'):
            await self.rt.action(10,55,'generate_image',{'prompt':'кот'})
        self.client.upload_file.assert_not_awaited()
        self.client.send_file.assert_not_awaited()

    async def test_dialog_archived_during_upload_prevents_delivery(self):
        token = current_dialog.set(self.store.dialog_scope(10))
        async def upload(*args):
            self.store.execute('INSERT INTO dialog_contexts(contact_id,sender_id,generation,started) VALUES (10,0,1,0)')
            return 'uploaded-image'
        self.client.upload_file.side_effect=upload
        try:
            with self.assertRaisesRegex(ValueError,'архив'):
                await self.rt.action(10,55,'generate_image',{'prompt':'кот'})
        finally:
            current_dialog.reset(token)
        self.client.send_file.assert_not_awaited()
        self.assertEqual(list((self.store.directory/'generated').iterdir()),[])

    async def test_private_direct_request_uses_generator_without_chat_planning(self):
        self.source(chat=tg.OWNER,sender=tg.OWNER)
        self.rt.model.run=AsyncMock()
        self.rt.control.plan=AsyncMock()
        await self.rt.reply(tg.OWNER,telegram_id=55,prepared=True)
        self.assertEqual(len(self.requests),1)
        self.client.send_file.assert_awaited_once()
        self.rt.model.run.assert_not_awaited()
        self.rt.telegram.send.assert_not_awaited()

    async def test_group_direct_request_uses_same_generation_path(self):
        self.source(chat=tg.GROUP,sender=10)
        self.rt.model.run=AsyncMock()
        await self.rt.reply(tg.GROUP,telegram_id=55,prepared=True)
        self.assertEqual(len(self.requests),1)
        self.assertEqual(self.client.send_file.call_args.args[0],tg.GROUP)
        self.rt.model.run.assert_not_awaited()

    async def test_missing_image_model_answers_instead_of_silence(self):
        self.store.update_settings({'image_generation_model':''})
        self.source()
        await self.rt.reply(10,telegram_id=55,prepared=True)
        self.assertEqual(len(self.requests),0)
        self.assertIn('ещё не указана',self.rt.telegram.send.call_args.args[1])


class ImageGenerationAPITests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=core.ApiTests.asyncSetUp
    asyncTearDown=core.ApiTests.asyncTearDown
    login=core.ApiTests.login
    mock_model_http=core.ApiTests.mock_model_http

    async def preview_result(self, run_id):
        for _ in range(200):
            response=await self.client.get('/api/ai/images/test/'+run_id)
            if response.json().get('status')!='running':return response
            await asyncio.sleep(0.01)
        self.fail('Preview did not finish')

    async def test_configuration_is_separate_and_key_never_returns_to_browser(self):
        await self.login()
        db = self.app.state.store
        previous = (db.settings()['ai_model'],db.settings()['ai_base_url'])
        response = await self.client.post('/api/ai/images/config',json={
            'model':'image-model','base_url':'https://images.example.com/v1','key':'private-image-key'})
        self.assertEqual(response.status_code,200,response.text)
        status = await self.client.get('/api/status')
        self.assertNotIn('private-image-key',status.text)
        self.assertTrue(status.json()['image_generation']['configured'])
        self.assertEqual((db.settings()['ai_model'],db.settings()['ai_base_url']),previous)
        self.assertEqual(db.secret_path.stat().st_mode & 0o777,0o600)

    async def test_image_configuration_requires_auth_and_rejects_wrong_origin(self):
        body={'model':'image-model'}
        self.assertEqual((await self.client.post('/api/ai/images/config',json=body)).status_code,401)
        await self.login()
        response=await self.client.post('/api/ai/images/config',json=body,headers={'Origin':'https://other.example'})
        self.assertEqual(response.status_code,403)

    async def test_invalid_url_or_key_does_not_modify_configuration(self):
        await self.login()
        db=self.app.state.store
        before=db.settings(),db.secrets()
        for body in [{'model':'test','base_url':'http://images.example.com/v1'},
                     {'model':'test','key':'invalid\nkey'}]:
            response=await self.client.post('/api/ai/images/config',json=body)
            self.assertEqual(response.status_code,400,response.text)
            self.assertEqual((db.settings(),db.secrets()),before)

    async def test_probe_generates_preview_without_saving_or_sending(self):
        await self.login()
        before=self.app.state.store.settings(),self.app.state.store.secrets()
        await self.mock_model_http(lambda request:image_response())
        response=await self.client.post('/api/ai/images/test',json={
            'base_url':'https://images.example.com/v1','model':'unsaved-image-model','key':'unsaved-key'})
        self.assertEqual(response.status_code,200,response.text)
        run_id=response.json()['run_id']
        result=await self.preview_result(run_id)
        self.assertEqual(result.json()['status'],'complete')
        self.assertTrue(result.json()['image'].startswith('data:image/jpeg;base64,'))
        self.assertEqual((self.app.state.store.settings(),self.app.state.store.secrets()),before)
        self.assertEqual(self.app.state.store.rows("SELECT * FROM messages WHERE role='assistant'"),[])

    async def test_model_discovery_marks_image_models_without_selecting_chat_model(self):
        await self.login()
        await self.mock_model_http(lambda request:httpx.Response(200,json={'data':[
            {'id':'chat-model'}, {'id':'flux-pro'},
            {'id':'custom-image','architecture':{'output_modalities':['image']}}]}))
        response=await self.client.post('/api/ai/images/models',json={
            'base_url':'https://images.example.com/v1','key':'test'})
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['image_models'],['custom-image','flux-pro'])
        self.assertEqual(self.app.state.store.settings()['image_generation_model'],'')

    async def test_clear_separate_key_does_not_clear_main_key(self):
        await self.login()
        db=self.app.state.store
        db.update_secrets({'ai_key':'main-key','image_generation_key':'separate-key'})
        response=await self.client.post('/api/ai/images/config',json={'model':'image-model','clear_key':True})
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(db.secrets()['ai_key'],'main-key')
        self.assertEqual(db.secrets()['image_generation_key'],'')

    async def test_preview_is_bound_to_the_session_that_requested_it(self):
        await self.login()
        await self.mock_model_http(lambda request:image_response())
        response=await self.client.post('/api/ai/images/test',json={'model':'image-model','key':'test'})
        run_id=response.json()['run_id']
        await self.preview_result(run_id)
        await self.client.post('/api/logout')
        await self.login()
        result=await self.client.get('/api/ai/images/test/'+run_id)
        self.assertEqual(result.status_code,403)


class ImageRequestParsingTests(unittest.TestCase):
    def test_drawing_requests_keep_the_requested_subject(self):
        for text in ['Даша нарисуй кота','можешь нарисовать кота','сгенерируй кота','создай изображение кота']:
            self.assertEqual(parse_generation_request(text),'кота')

    def test_discussion_negation_and_quotes_do_not_trigger_direct_generation(self):
        for text in ['Ты умеешь рисовать?','не рисуй кота','Он сказал: нарисуй кота','найди фото кота']:
            self.assertIsNone(parse_generation_request(text))
