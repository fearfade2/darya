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
from app.notebook import Notebook
from app.runtime import Runtime
from app.security import create_session
from app.store import Store


CHAT, OTHER, SELF = -1001234567890, -1001234567891, 50


class NotebookTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.db=Store(Path(self.tmp.name))
        for id in (10,20): self.db.upsert_contact(id,str(id))
        for id in (CHAT,OTHER): self.db.upsert_contact(id,'Группа',kind='group')
        self.db.update_settings({'enabled':True,'memory_enabled':True,'typing_enabled':False,'natural_enabled':False})
        self.rt=Runtime(self.db)
        self.rt.telegram.account_id=SELF
        self.rt.telegram.send=AsyncMock()
        self.rt.model.completion=AsyncMock()
        self.notes=self.rt.notebook
        self.user=self.db.message(10,'user','Люблю джаз, учусь играть на гитаре',1,sender_id=10)
        self.bot=self.db.message(10,'assistant','Какую песню хочешь научиться играть?',2)
        self.payload={'summary':[self.entry('Обсуждали музыку',self.user,'Люблю джаз')],
            'people':[self.entry('Любит джаз',self.user,'Люблю джаз')|{'person_id':10}],
            'threads':[self.entry('Учится играть на гитаре',self.user,'учусь играть на гитаре')|{'status':'open'}],
            'self_notes':[self.entry('Уже спросила о песне для гитары',self.bot,'Какую песню хочешь научиться играть?')]}
        self.respond(self.payload)

    async def asyncTearDown(self):
        await self.rt.stop()
        self.db.close()
        self.tmp.cleanup()

    def entry(self,text,id,quote):
        return {'text':text,'evidence':[{'id':id,'quote':quote}]}

    def respond(self,payload):
        self.rt.model.completion.return_value={'content':json.dumps(payload,ensure_ascii=False)}

    async def save(self):
        self.assertTrue(await self.notes.update(self.notes.key(10)))

    async def test_notes_are_grounded_persistent_and_exported_to_markdown(self):
        await self.save()
        self.assertEqual(Notebook(self.rt).view(10),self.payload)
        file=self.db.directory/'notebooks/10/generation-0/topic-0.md'
        self.assertIn('Любит джаз',file.read_text())
        self.assertIn('Сообщение '+str(self.user),file.read_text())
        self.assertEqual(file.stat().st_mode&0o777,0o600)
        self.rt.telegram.send.assert_not_awaited()
        self.assertFalse(self.rt.model.completion.call_args.kwargs['with_tools'])

    async def test_forged_quotes_missing_sources_and_wrong_author_are_discarded(self):
        for evidence,person in (([{'id':999,'quote':'Люблю джаз'}],10),
                                ([{'id':self.user,'quote':'Люблю хоккей'}],10),
                                ([{'id':self.user,'quote':'Люблю джаз'}],20)):
            self.respond({'people':[{'text':'Любит джаз','person_id':person,'evidence':evidence}]})
            self.assertFalse(await self.notes.update(self.notes.key(10)))
            self.assertEqual(self.notes.view(10),{})

    async def test_facts_cannot_be_attributed_from_another_person_or_forward(self):
        for forwarded in (False,True):
            self.db.group_message(CHAT,1,'Оля любит джаз',sender_id=20,forwarded=forwarded)
            self.respond({'people':[self.entry('Любит джаз',1,'любит джаз')|{'person_id':10}]})
            await self.notes.update(self.notes.key(CHAT))
            self.assertEqual(self.notes.view(CHAT),{})

    async def test_assistant_messages_cannot_establish_a_persons_preferences(self):
        self.respond({'people':[self.entry('Любит песни для гитары',self.bot,'песню хочешь научиться играть')|{'person_id':10}]})
        self.assertFalse(await self.notes.update(self.notes.key(10)))
        self.assertEqual(self.notes.view(10),{})

    async def test_private_notebook_does_not_enter_another_dialog_or_group(self):
        await self.save()
        self.assertIn('Любит джаз',str(self.rt.model.context(10)))
        self.assertNotIn('Любит джаз',str(self.rt.model.context(20)))
        self.assertNotIn('Любит джаз',str(self.rt.model.context(CHAT)))

    async def test_group_topics_and_other_groups_are_separate(self):
        for chat,topic,text in ((CHAT,390,'Люблю собирать приложения'),(CHAT,38,'Люблю готовить пиццу'),(OTHER,390,'Люблю ухаживать за попугаем')):
            self.db.group_message(chat,topic,text,sender_id=10,topic_id=topic)
        self.respond({'summary':[self.entry('Обсуждали сборку приложений',390,'Люблю собирать приложения')]})
        self.assertTrue(await self.notes.update(self.notes.key(CHAT,390)))
        self.assertIn('Обсуждали сборку приложений',str(self.notes.view(CHAT,390)))
        self.assertEqual(self.notes.view(CHAT,38),{})
        self.assertEqual(self.notes.view(OTHER,390),{})
        self.db.message(CHAT,'user','Люблю собирать приложения',390,sender_id=10)
        token=current_dialog.set(self.db.dialog_scope(CHAT,10))
        try:self.assertIn('Обсуждали сборку приложений',str(self.rt.model.context(CHAT)))
        finally:current_dialog.reset(token)

    async def test_a_later_notebook_is_not_used_for_an_earlier_queued_request(self):
        await self.save()
        self.assertEqual(self.notes.view(10,before=self.user),{})

    async def test_disabled_memory_and_preview_do_not_use_notes(self):
        await self.save()
        self.assertNotIn('Любит джаз',str(self.rt.model.context(10,include_history=False)))
        self.db.update_settings({'memory_enabled':False})
        self.assertEqual(self.notes.view(10),{})
        self.assertIn('Любит джаз',str(self.notes.view(10,include_disabled=True)))
        self.assertNotIn('Любит джаз',str(self.rt.model.context(10)))

    async def test_secret_and_location_notes_are_not_saved(self):
        for text,quote in (('Живёт в Симферополе','Симферополе'),('Пароль limon123','пароль limon123')):
            row=self.db.message(10,'user','Мой '+quote,3 if 'Пароль' in text else 4,sender_id=10)
            self.respond({'summary':[self.entry(text,row,quote)]})
            self.assertFalse(await self.notes.update(self.notes.key(10)))
            self.assertEqual(self.notes.view(10),{})
        data=json.loads(self.rt.model.completion.call_args.args[0][1]['content'])
        self.assertNotIn('limon123',str(data))

    async def test_tool_calls_and_configuration_payloads_do_not_change_authority(self):
        before=self.db.settings()
        self.rt.model.completion.return_value={'content':'{}','tool_calls':[{'function':{'name':'grant_control'}}]}
        self.assertFalse(await self.notes.update(self.notes.key(10)))
        self.respond({'settings':{'owner_id':20},'summary':self.payload['summary']})
        self.assertFalse(await self.notes.update(self.notes.key(10)))
        self.assertEqual(self.db.settings(),before)
        self.assertEqual(self.db.rows('SELECT * FROM controller_permissions'),[])
        self.rt.telegram.send.assert_not_awaited()

    async def test_malformed_response_retains_previous_notes(self):
        await self.save()
        self.rt.model.completion.return_value={'content':'not json'}
        self.assertFalse(await self.notes.update(self.notes.key(10)))
        self.assertEqual(self.notes.view(10),self.payload)

    async def test_invalid_citations_do_not_erase_existing_notes(self):
        await self.save()
        self.respond({'summary':[self.entry('Выдуманный факт',self.user,'Я такого не говорил')]})
        self.assertFalse(await self.notes.update(self.notes.key(10)))
        self.assertEqual(self.notes.view(10),self.payload)

    async def test_new_fact_can_replace_an_old_preference_and_close_a_topic(self):
        await self.save()
        new=self.db.message(10,'user','Теперь больше слушаю рок. Песню выучил.',3,sender_id=10)
        replacement={'people':[self.entry('Теперь слушает рок',new,'больше слушаю рок')|{'person_id':10}],
            'threads':[self.entry('Песня для гитары выучена',new,'Песню выучил')|{'status':'closed'}]}
        self.respond(replacement)
        await self.save()
        self.assertNotIn('Любит джаз',str(self.notes.view(10)))
        self.assertEqual(self.notes.view(10)['threads'][0]['status'],'closed')

    async def test_clear_prevents_reconstruction_from_old_messages(self):
        await self.save()
        self.notes.clear(10)
        self.assertEqual(self.notes.view(10),{})
        self.assertFalse(await self.notes.update(self.notes.key(10)))
        self.db.message(10,'user','Люблю готовить пиццу',3,sender_id=10)
        self.respond(self.payload)
        self.assertFalse(await self.notes.update(self.notes.key(10)))
        self.assertEqual(self.notes.view(10),{})

    async def test_forget_action_clears_derived_notes_without_erasing_other_dialogs(self):
        await self.save()
        self.db.remember(10,'музыка','джаз')
        await self.rt.action(10,1,'forget',{'key':'музыка'})
        self.assertEqual(self.notes.view(10),{})
        self.assertEqual(self.db.context_memories(10),[])
        self.assertEqual(self.db.context_history(20),[])

    async def test_clear_while_model_is_working_cancels_the_old_result(self):
        async def response(*args,**kwargs):
            self.notes.clear(10)
            return {'content':json.dumps(self.payload)}
        self.rt.model.completion.side_effect=response
        self.assertFalse(await self.notes.update(self.notes.key(10)))
        self.assertEqual(self.notes.view(10),{})

    async def test_archive_while_model_is_working_cancels_the_old_result(self):
        async def response(*args,**kwargs):
            self.db.archive_contexts()
            return {'content':json.dumps(self.payload)}
        self.rt.model.completion.side_effect=response
        self.assertFalse(await self.notes.update((10,0,0,0)))
        self.assertEqual(self.notes.view(10),{})

    async def test_pause_or_disable_memory_during_update_prevents_save(self):
        for key in ('enabled','memory_enabled'):
            self.db.update_settings({key:True})
            async def response(*args,**kwargs):
                self.db.update_settings({key:False})
                return {'content':json.dumps(self.payload)}
            self.rt.model.completion.side_effect=response
            self.assertFalse(await self.notes.update(self.notes.key(10)))
            self.db.update_settings({key:True})
        self.assertEqual(self.notes.view(10),{})

    async def test_edited_source_invalidates_its_old_quotes(self):
        await self.save()
        self.db.execute('UPDATE messages SET text=? WHERE id=?',('Не люблю джаз',self.user))
        notes=self.notes.view(10)
        self.assertNotIn('people',notes)
        self.assertNotIn('threads',notes)

    async def test_background_updates_are_batched_and_throttled(self):
        now=time.time()
        self.db.execute('DELETE FROM messages WHERE contact_id=10')
        self.respond({})
        for id in range(10,16):self.db.message(10,'user','Обсуждаем музыку',id,now=now-60,sender_id=10)
        await self.notes.tick(now)
        await asyncio.gather(*self.rt.work)
        self.rt.model.completion.assert_awaited_once()
        await Notebook(self.rt).tick(now+60)
        self.rt.model.completion.assert_awaited_once()
        self.rt.telegram.send.assert_not_awaited()

    async def test_markdown_export_does_not_follow_a_symlink(self):
        target=self.db.directory/'outside.md'
        target.write_text('Keep this')
        directory=self.db.directory/'notebooks/10/generation-0'
        directory.mkdir(parents=True)
        (directory/'topic-0.md').symlink_to(target)
        self.assertFalse(await self.notes.update(self.notes.key(10)))
        self.assertEqual(target.read_text(),'Keep this')

    async def test_initiative_uses_its_public_notebook(self):
        from datetime import datetime
        now=datetime.fromisoformat('2026-11-08T12:00:00+03:00').timestamp()
        self.db.update_settings({'timezone':'Europe/Moscow'})
        with patch('app.group_initiative.time.time',return_value=now):
            self.rt.telegram.ready=Mock(return_value=True)
            self.db.execute('UPDATE contacts SET proactive=1 WHERE id=?',(CHAT,))
            self.db.group_message(CHAT,1,'Люблю собирать приложения',sender_id=10,created=now-3600)
            self.db.group_message(CHAT,2,'Что сейчас собираешь?',sender_id=SELF,outgoing=True,created=now-1800)
            self.respond({'summary':[self.entry('Обсуждали сборку приложений',1,'Люблю собирать приложения')]})
            self.assertTrue(await self.notes.update(self.notes.key(CHAT)))
            self.rt.model.completion.return_value={'content':'{"message":null,"wait_minutes":30}'}
            self.assertFalse(await self.rt.group_initiative.consider(CHAT))
            data=json.loads(self.rt.model.completion.call_args.args[0][1]['content'])
            self.assertIn('Обсуждали сборку приложений',str(data['notebook']))
            self.assertNotIn('Любит джаз',str(data['notebook']))

    async def test_authenticated_owner_can_review_download_and_clear_notes(self):
        await self.save()
        previous_store=getattr(app.state,'store',None)
        previous_runtime=getattr(app.state,'runtime',None)
        app.state.store=self.db;app.state.runtime=self.rt
        try:
            with patch.dict(os.environ,{'PUBLIC_ORIGINS':'http://test'}):
              async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test',headers={'Origin':'http://test'}) as client:
                response=await client.get('/api/contacts/10/notebooks')
                self.assertEqual(response.status_code,401)
                client.cookies.set('alisa_session',create_session(self.db))
                response=await client.get('/api/contacts/10/notebooks')
                self.assertEqual(response.status_code,200)
                self.assertIn('Любит джаз',response.json()[0]['markdown'])
                response=await client.request('DELETE','/api/contacts/10/notebooks/0',json={})
                self.assertEqual(response.status_code,200)
                self.assertEqual((await client.get('/api/contacts/10/notebooks')).json(),[])
        finally:
            app.state.store=previous_store;app.state.runtime=previous_runtime

    async def test_failed_update_applies_exponential_backoff_and_ipv6_mapped_private_ips_rejected(self):
        from app.websites import _public_ip
        self.assertFalse(_public_ip('::ffff:127.0.0.1'))
        self.assertFalse(_public_ip('2002:7f00:0001::1'))
        self.assertTrue(_public_ip('1.1.1.1'))
        key = self.notes.key(10)
        self.rt.model.completion.return_value = {'content': 'invalid'}
        started = time.time()
        self.assertFalse(await self.notes.update(key))
        first_due = self.notes.record(key)['next_attempt']
        self.assertGreaterEqual(first_due, started + 290)
        self.assertFalse(await self.notes.update(key))
        second_due = self.notes.record(key)['next_attempt']
        self.assertGreaterEqual(second_due, started + 590)
