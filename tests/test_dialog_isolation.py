import asyncio
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import httpx
from app.api import app
from app.dialogs import current_dialog
from app.runtime import Runtime
from app.security import create_session
from app.store import Store
from app.voice import speech_text

OWNER=1000000001

class DialogIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.db=Store(self.root)
        for id in [OWNER,10,20]:self.db.upsert_contact(id,str(id))
        self.db.upsert_contact(-100,'group',kind='group')
        self.db.update_settings({'owner_id':OWNER,'enabled':True,'natural_enabled':False,'typing_enabled':False,'min_interval':0,
                                 'gifts_enabled':False,'quiet_start':0,'quiet_end':0})
        self.rt=Runtime(self.db)
        self.rt.telegram.send=AsyncMock(return_value=1000)
        self.rt.telegram.send_media=AsyncMock(return_value=1001)
        self.rt.telegram.read=AsyncMock()
        self.rt.telegram.ready=Mock(return_value=True)
        self.rt.model.key=Mock(return_value='test')

    async def asyncTearDown(self):
        for task in list(self.rt.work):task.cancel()
        await asyncio.gather(*list(self.rt.work),return_exceptions=True)
        await self.rt.model.http.aclose();await self.rt.hermes.http.aclose();await self.rt.websites.http.aclose()
        self.db.close();self.tmp.cleanup()

    def bind(self,id,author=None):
        return current_dialog.set(self.db.dialog_scope(id,author))

    async def test_reset_retains_archive_but_removes_all_old_model_inputs(self):
        persona=self.db.settings()['persona']
        self.db.execute("UPDATE contacts SET notes='PRIVATE_NOTE',birthday='2000-01-01',persona='OLD_STYLE',proactive=1 WHERE id=10")
        self.db.remember(10,'hobby','PRIVATE_FACT');self.db.message(10,'user','PRIVATE_MESSAGE',1,sender_id=10,image_path='incoming/old.jpg')
        self.db.set_reply_mode(10,10,'voice');task=self.db.schedule(10,'PRIVATE_REMINDER',time.time()-1)
        old=self.rt.hermes.headers(10)['X-Hermes-Session-Key']
        report=self.db.archive_contexts()
        self.assertEqual(report['messages_retained'],1);self.assertEqual(report['memories_retained'],1)
        context=json.dumps(self.rt.model.context(10),ensure_ascii=False)
        for value in ['PRIVATE_NOTE','PRIVATE_FACT','PRIVATE_MESSAGE','OLD_STYLE','2000-01-01']:self.assertNotIn(value,context)
        self.assertEqual(self.db.context_history(10),[]);self.assertEqual(self.db.reply_mode(10,10),'text')
        self.assertEqual(self.db.all_memories(10)[0]['value'],'PRIVATE_FACT')
        self.assertTrue(self.db.archive_rows('messages',10)[0]['archived'])
        self.assertEqual(self.db.rows('SELECT status FROM tasks WHERE id=?',(task,))[0]['status'],'archived')
        self.assertNotEqual(old,self.rt.hermes.headers(10)['X-Hermes-Session-Key'])
        self.assertEqual(self.db.settings()['persona'],persona)
        await self.rt.tick();self.rt.telegram.send.assert_not_awaited()
        self.db.message(10,'user','NEW_MESSAGE',2,sender_id=10);self.db.remember(10,'hobby','NEW_FACT')
        self.assertIn('NEW_FACT',str(self.rt.model.context(10)));self.assertNotIn('PRIVATE_FACT',str(self.rt.model.context(20)))

    async def test_group_authors_have_independent_memory_photos_history_and_reminders(self):
        for author in [10,20]:
            self.db.message(-100,'user',f'SECRET_{author}',author,sender_id=author,image_path=f'incoming/{author}.jpg')
            token=self.bind(-100,author)
            try:
                self.db.remember(-100,'hobby',f'FACT_{author}')
                self.db.message(-100,'assistant',f'REPLY_{author}',author+100)
                self.db.schedule(-100,f'REMINDER_{author}',time.time()+100)
            finally:current_dialog.reset(token)
        token=self.bind(-100,10)
        try:
            context=str(self.rt.model.context(-100));self.assertIn('SECRET_10',context);self.assertIn('FACT_10',context)
            for value in ['SECRET_20','REPLY_20']:self.assertIn(value,context)
            self.assertNotIn('FACT_20',context)
            self.assertEqual([row['image_path'] for row in self.db.context_history(-100) if row['image_path']],['incoming/10.jpg'])
            reminders=await self.rt.action(-100,10,'list_reminders',{});self.assertEqual([r['text'] for r in reminders],['REMINDER_10'])
            other=self.db.rows("SELECT id FROM tasks WHERE text='REMINDER_20'")[0]['id']
            self.assertFalse((await self.rt.action(-100,10,'cancel_reminder',{'task_id':other}))['cancelled'])
        finally:current_dialog.reset(token)

    async def test_background_job_preserves_original_author_and_generation(self):
        self.db.execute('UPDATE contacts SET hermes_allowed=1')
        started=asyncio.Event();resume=asyncio.Event()
        async def helper(task,id):
            started.set();await resume.wait()
            self.assertEqual(self.db.dialog_scope(id)[1],10)
            return 'JOB_RESULT'
        self.rt.hermes.run=AsyncMock(side_effect=helper)
        token=self.bind(-100,10)
        try:job=await self.rt.submit_job('Task',-100)
        finally:current_dialog.reset(token)
        await started.wait()
        self.db.message(-100,'user','OTHER_AUTHOR',1,sender_id=20)
        resume.set();await asyncio.gather(*list(self.rt.work))
        row=self.db.rows('SELECT * FROM jobs WHERE id=?',(job['job_id'],))[0]
        self.assertEqual(row['context_sender_id'],10)
        self.assertEqual(self.db.context_history(-100),[self.db.history(-100)[0]])
        token=self.bind(-100,10)
        try:self.assertIn('JOB_RESULT',str(self.db.context_history(-100)))
        finally:current_dialog.reset(token)

    async def test_reset_blocks_delivery_from_in_flight_old_job(self):
        self.db.execute('UPDATE contacts SET hermes_allowed=1')
        started=asyncio.Event();resume=asyncio.Event()
        async def helper(*args):started.set();await resume.wait();return 'OLD_JOB_SECRET'
        self.rt.hermes.run=AsyncMock(side_effect=helper)
        job=await self.rt.submit_job('Task',10);await started.wait();self.db.archive_contexts();resume.set()
        await asyncio.gather(*list(self.rt.work));self.rt.telegram.send.assert_not_awaited()
        self.assertEqual(self.db.context_rows('jobs',10),[])
        self.assertTrue(self.db.archive_rows('jobs',10)[0]['archived'])

    async def test_files_are_limited_to_author_and_generation_even_for_owner(self):
        folder=self.root/'media';folder.mkdir()
        for id,author in [(10,0),(OWNER,0),(-100,10),(-100,20)]:
            name=f'hermes-{id}-{author}.csv';(folder/name).write_text('value\nPRIVATE_FILE\n')
            self.db.execute("INSERT INTO jobs(contact_id,task,created,updated,artifact,context_sender_id) VALUES (?,'task',0,0,?,?)",(id,name,author))
        self.assertEqual([f['filename'] for f in self.rt.media(OWNER)],[f'hermes-{OWNER}-0.csv'])
        token=self.bind(-100,10)
        try:self.assertEqual([f['filename'] for f in self.rt.media(-100)],['hermes--100-10.csv'])
        finally:current_dialog.reset(token)
        self.db.archive_contexts();self.assertEqual(self.rt.media(OWNER),[]);self.assertEqual(len(self.rt.media()),4)
        with self.assertRaises(ValueError):await self.rt.send_file(10,'hermes-10-0.csv','')
        self.rt.telegram.send_media.assert_not_awaited()

    async def test_unassigned_owner_upload_is_never_shared_with_other_users(self):
        folder=self.root/'media';folder.mkdir();(folder/'private-owner.jpg').write_bytes(b'private')
        self.assertEqual(self.rt.media(10),[])
        self.assertEqual(self.rt.media(OWNER)[0]['filename'],'private-owner.jpg')
        self.db.archive_contexts();self.assertEqual(self.rt.media(OWNER),[])
        self.assertEqual(self.rt.media()[0]['filename'],'private-owner.jpg')

    async def test_hermes_cannot_import_fresh_file_from_other_namespace(self):
        with patch.dict(os.environ,{'HERMES_WORKSPACE':str(self.root/'helper')}):
            other=self.rt.hermes.workspace(20);other.mkdir(parents=True)
            file=other/'private.csv';file.write_text('SECRET\nowner data\n')
            token=self.bind(10)
            try:self.assertEqual(self.rt.import_artifact(1,str(file),time.time())[0],'')
            finally:current_dialog.reset(token)

    async def test_two_group_authors_share_public_context_but_keep_their_reply_targets(self):
        self.db.update_settings({'group_burst_window':0})
        self.rt.group_conversation.should_join=AsyncMock(return_value=True)
        contexts=[]
        async def answer(id,*args,**kwargs):
            contexts.append(self.rt.model.context(id));await asyncio.sleep(0)
            return 'Answer'
        self.rt.model.run=AsyncMock(side_effect=answer)
        self.assertTrue(self.rt.receive(-100,'SECRET_10',1,sender_id=10))
        self.assertTrue(self.rt.receive(-100,'SECRET_20',2,sender_id=20))
        await asyncio.gather(*list(self.rt.work))
        self.assertEqual(len(contexts),2)
        self.assertIn('SECRET_10',str(contexts[0]));self.assertIn('SECRET_20',str(contexts[0]))
        self.assertIn('SECRET_20',str(contexts[1]));self.assertIn('SECRET_10',str(contexts[1]))
        self.assertEqual(self.rt.telegram.send.await_count,2)

    async def test_owner_panel_can_open_archive_and_delete_one_scoped_memory(self):
        self.db.remember(10,'hobby','OLD_FACT');self.db.message(10,'user','OLD_MESSAGE',1,sender_id=10)
        self.db.archive_contexts();self.db.remember(10,'hobby','NEW_FACT')
        saved_store=getattr(app.state,'store',None);saved_runtime=getattr(app.state,'runtime',None)
        app.state.store=self.db;app.state.runtime=self.rt
        try:
            with patch.dict(os.environ,{'COOKIE_SECURE':'false','PUBLIC_ORIGINS':'http://test'}):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test',headers={'Origin':'http://test','Content-Type':'application/json'}) as c:
                    self.assertEqual((await c.get('/api/contacts/10/history?archive=true')).status_code,401)
                    c.cookies.set('alisa_session',create_session(self.db))
                    current=(await c.get('/api/contacts/10/history')).json();archive=(await c.get('/api/contacts/10/history?archive=true')).json()
                    self.assertEqual(current['messages'],[]);self.assertEqual(current['memories'][0]['value'],'NEW_FACT')
                    self.assertEqual(archive['messages'][0]['text'],'OLD_MESSAGE');self.assertEqual(archive['memories'][0]['value'],'OLD_FACT')
                    response=await c.delete('/api/contacts/10/memories/hobby?sender_id=0&generation=0');self.assertEqual(response.status_code,200)
                    self.assertEqual(self.db.context_memories(10)[0]['value'],'NEW_FACT')
        finally:app.state.store=saved_store;app.state.runtime=saved_runtime

    def test_speech_normalizes_orthography_without_changing_dialog_text(self):
        self.assertEqual(speech_text('Прывет! Щас всё харашо 😊 https://example.test'),'привет! сейчас всё хорошо')
