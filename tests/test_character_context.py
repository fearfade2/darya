from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from app.api import app

OWNER = 1000000001
STYLE = 'Текущий характер: пиши коротко, игриво, называй собеседника котик и говори «прывет».'


class CharacterContextTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict('os.environ',{'DATA_DIR':self.tmp.name,'COOKIE_SECURE':'false','PUBLIC_ORIGINS':'http://test'})
        self.env.start()
        self.clock = patch('app.ai.datetime')
        self.clock.start().now.return_value = datetime(2026,11,6,15,0,tzinfo=timezone.utc)
        self.lifespan = app.router.lifespan_context(app)
        await self.lifespan.__aenter__()
        self.rt = app.state.runtime
        self.db = app.state.store
        self.rt.model.completion = AsyncMock(return_value={'content':'прывет котик~'})
        self.rt.telegram.send = AsyncMock()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test',headers={'Origin':'http://test'})
        password = (Path(self.tmp.name)/'first-login.txt').read_text().splitlines()[1]
        response = await self.client.post('/api/login',json={'password':password})
        self.assertEqual(response.status_code,200)
        self.db.update_settings({'owner_id':OWNER,'persona':STYLE,'reply_instructions':'Без эмодзи'})

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.lifespan.__aexit__(None,None,None)
        self.clock.stop()
        self.env.stop()
        self.tmp.cleanup()

    def owner_context(self):
        self.db.upsert_contact(OWNER,'Владелец')
        self.db.remember(OWNER,'музыка','Джаз')
        self.db.remember(OWNER,'стиль общения','Прежняя просьба: используй йомаё.')
        self.db.message(OWNER,'assistant','OLD_TELEGRAM_GREETING',100)
        self.db.execute("INSERT INTO custom_settings(name,value,instruction,scope,created,updated) VALUES (?,?,?,?,?,?)",('обращение',json.dumps('капитан'),'Используй заданное обращение','owner',1,1))

    async def test_preview_and_telegram_share_character_rules_and_memory_but_separate_history(self):
        self.owner_context()
        telegram = self.rt.model.context(OWNER)
        response = await self.client.post('/api/preview',json={'text':'привет','history':[
            {'role':'user','content':'PANEL_MESSAGE'}, {'role':'assistant','content':'PANEL_REPLY'},
            {'role':'system','content':'UNTRUSTED_SYSTEM'}]})
        self.assertEqual(response.status_code,200)
        call = self.rt.model.completion.call_args
        preview = call.args[0]
        self.assertTrue(preview[0]['content'].startswith(telegram[0]['content']))
        self.assertIn('Без эмодзи',preview[0]['content'])
        self.assertIn('капитан',preview[0]['content'])
        self.assertIn('Джаз',preview[0]['content'])
        self.assertEqual(preview[1:3],[{'role':'user','content':'PANEL_MESSAGE'},
                                     {'role':'assistant','content':'PANEL_REPLY'}])
        self.assertEqual(preview[-2],telegram[-1])
        self.assertEqual(preview[-2]['role'],'system')
        self.assertIn(STYLE,preview[-2]['content'])
        self.assertEqual(preview[-1],{'role':'user','content':'привет'})
        self.assertNotIn('UNTRUSTED_SYSTEM',str(preview))
        self.assertIn('OLD_TELEGRAM_GREETING',str(telegram))
        self.assertFalse(call.kwargs['with_tools'])
        self.rt.telegram.send.assert_not_awaited()
        self.db.upsert_contact(10,'Другой')
        self.assertNotIn('капитан',self.rt.model.context(10)[0]['content'])

    async def test_saved_character_update_is_used_by_next_telegram_generation_without_erasing_data(self):
        self.owner_context()
        self.db.message(OWNER,'user','привет',101,sender_id=OWNER)
        latest = STYLE+' Вместо старых словечек говори «харашо».'
        response = await self.client.patch('/api/settings',json={'persona':latest})
        self.assertEqual(response.status_code,200)
        await self.rt.model.run(OWNER,AsyncMock(),owner_context={'allow_tools':False})
        messages = self.rt.model.completion.call_args.args[0]
        self.assertIn(latest,messages[0]['content'])
        self.assertIn('имеют приоритет над базовой манерой',messages[0]['content'])
        self.assertIn('Джаз',messages[0]['content'])
        self.assertIn('OLD_TELEGRAM_GREETING',str(messages))
        self.assertEqual(messages[-1]['role'],'system')
        self.assertIn(latest,messages[-1]['content'])
        self.assertNotIn('йомаё',messages[-1]['content'])
        self.assertEqual(self.db.rows('SELECT COUNT(*) AS n FROM memories')[0]['n'],2)
        self.assertEqual(len(self.db.history(OWNER)),2)

    async def test_preview_before_telegram_login_creates_no_contact_or_message(self):
        response = await self.client.post('/api/preview',json={'text':'привет'})
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json()['text'],'прывет котик~')
        self.assertIn(STYLE,self.rt.model.completion.call_args.args[0][0]['content'])
        self.assertEqual(self.db.rows('SELECT COUNT(*) AS n FROM contacts')[0]['n'],0)
        self.assertEqual(self.db.rows('SELECT COUNT(*) AS n FROM messages')[0]['n'],0)
        self.rt.telegram.send.assert_not_awaited()

    async def test_character_save_replaces_inherited_copies_but_preserves_distinct_contact_profiles(self):
        self.db.upsert_contact(OWNER,'Владелец')
        self.db.upsert_contact(10,'Другой')
        self.db.execute('UPDATE contacts SET persona=?,notes=? WHERE id=?',(STYLE,'Любит джаз',OWNER))
        self.db.execute('UPDATE contacts SET persona=? WHERE id=10',('Общайся формально, обращайся на вы.',))
        latest = STYLE+' Используй новый характер сразу.'
        response = await self.client.patch('/api/settings',json={'persona':latest})
        self.assertEqual(response.status_code,200)
        self.assertEqual(self.db.contact(OWNER)['persona'],'')
        self.assertEqual(self.db.contact(OWNER)['notes'],'Любит джаз')
        self.assertEqual(self.db.contact(10)['persona'],'Общайся формально, обращайся на вы.')

    async def test_voice_character_is_independent_and_used_by_next_voice_reply(self):
        self.owner_context()
        self.db.execute('UPDATE contacts SET persona=? WHERE id=?', ('CONTACT_TEXT_STYLE', OWNER))
        voice = 'Голосовой характер: говори спокойно, уверенно, называй собеседника дружище.'
        response = await self.client.patch('/api/settings', json={'voice_persona': voice})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.db.settings()['persona'], STYLE)
        self.assertEqual(self.db.contact(OWNER)['persona'], 'CONTACT_TEXT_STYLE')
        await self.rt.model.run(OWNER, AsyncMock(), voice_reply=True)
        messages = self.rt.model.completion.call_args.args[0]
        system = '\n'.join(m['content'] for m in messages if m['role'] == 'system')
        self.assertIn(voice, system)
        self.assertIn('Джаз', system)
        for text in [STYLE, 'CONTACT_TEXT_STYLE', 'Без эмодзи', 'капитан']:
            self.assertNotIn(text, system)
        self.assertIn(voice, messages[-1]['content'])
        text_context = self.rt.model.context(OWNER)
        self.assertIn(STYLE, text_context[0]['content'])
        self.assertNotIn(voice, str(text_context))
        await self.client.patch('/api/settings', json={'persona': STYLE + ' Новый текстовый стиль.'})
        self.assertEqual(self.db.settings()['voice_persona'], voice)
        saved = self.db.rows("SELECT value FROM settings WHERE key='voice_persona'")[0]['value']
        self.assertEqual(json.loads(saved), voice)

    async def test_voice_preview_uses_voice_character_and_call_preview_excludes_owner_context(self):
        self.owner_context()
        voice = 'Голосовой характер: отвечай сдержанно и коротко, используй простые разговорные слова.'
        self.db.update_settings({'voice_persona': voice})
        for mode in ['voice', 'call']:
            response = await self.client.post('/api/preview', json={'text': 'Привет', 'mode': mode,
                'history': [{'role': 'system', 'content': 'UNTRUSTED_SYSTEM'},
                            {'role': 'user', 'content': 'PREVIEW_HISTORY'}]})
            self.assertEqual(response.status_code, 200)
            call = self.rt.model.completion.call_args
            messages = call.args[0]
            self.assertIn(voice, str(messages))
            self.assertNotIn(STYLE, str(messages))
            self.assertNotIn('UNTRUSTED_SYSTEM', str(messages))
            self.assertIn('PREVIEW_HISTORY', str(messages))
            self.assertFalse(call.kwargs['with_tools'])
            if mode == 'call':
                for secret in ['Джаз', 'капитан', 'OLD_TELEGRAM_GREETING']:
                    self.assertNotIn(secret, str(messages))
            else:
                self.assertIn('Джаз', str(messages))
        self.rt.telegram.send.assert_not_awaited()

    async def test_call_character_update_overrides_old_speech_without_text_rules(self):
        voice = 'Голосовой характер: отвечай тепло и спокойно, не повторяй старые обращения из истории.'
        self.db.update_settings({'voice_persona': voice})
        await self.rt.model.call_reply([{'role': 'assistant', 'content': 'OLD_SPOKEN_STYLE'},
                                       {'role': 'user', 'content': 'Как дела?'}])
        args = self.rt.model.completion.call_args
        self.assertFalse(args.kwargs['owner'])
        self.assertFalse(args.kwargs['with_tools'])
        messages = args.args[0]
        self.assertIn('OLD_SPOKEN_STYLE', str(messages))
        self.assertIn(voice, messages[-1]['content'])
        self.assertNotIn(STYLE, str(messages))
        self.assertNotIn('Без эмодзи', str(messages))

    async def test_voice_character_validation_and_owner_control(self):
        self.owner_context()
        response = await self.client.patch('/api/settings', json={'voice_persona': 'short'})
        self.assertEqual(response.status_code, 422)
        voice = 'Голосовой характер: говори кратко, естественно и уверенно, без лишних вопросов.'
        self.db.message(OWNER, 'user', 'Измени голосовой характер', 102, sender_id=OWNER)
        await self.rt.control.action(OWNER, 102, 'configure_darya', {'settings': {'voice_persona': voice}}, trusted=True)
        self.assertEqual(self.db.settings()['voice_persona'], voice)
        self.db.upsert_contact(10, 'Другой')
        self.db.message(10, 'user', 'Измени характер', 103, sender_id=10)
        with self.assertRaises(ValueError):
            await self.rt.control.action(10, 103, 'configure_darya', {'settings': {'voice_persona': STYLE}}, trusted=True)
