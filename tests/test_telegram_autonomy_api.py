import asyncio
import json
import os
from unittest.mock import AsyncMock,patch

import httpx

from app.api import app,COOKIE
from app.security import create_session
from telegram_fixtures import TelegramFixture,document


class TelegramApiTests(TelegramFixture):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.origins=patch.dict(os.environ,{'PUBLIC_ORIGINS':'http://127.0.0.1:8790'})
        self.origins.start()
        app.state.store=self.store;app.state.runtime=self.rt
        self.token=create_session(self.store)
        self.http=httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8790',
            headers={'Origin':'http://127.0.0.1:8790'},cookies={COOKIE:self.token})

    async def asyncTearDown(self):
        await self.http.aclose();self.origins.stop();await super().asyncTearDown()

    async def done(self,run_id):
        await asyncio.gather(*list(self.rt.work),return_exceptions=True)
        response=await self.http.get('/api/telegram/actions/'+str(run_id))
        return response.json()

    async def test_new_read_and_write_endpoints_require_panel_authentication(self):
        self.http.cookies.clear()
        for url in ('/api/telegram/autonomy','/api/telegram/media','/api/telegram/actions/1','/api/telegram/media/sticker:123/preview'):
            self.assertEqual((await self.http.get(url)).status_code,401)
        for url,data in (('/api/telegram/actions',{'action':'profile'}),('/api/telegram/command',{'text':'профиль'}),
            ('/api/telegram/media/search',{'kind':'sticker'}),('/api/telegram/autonomy/check',{})):
            self.assertEqual((await self.http.post(url,json=data)).status_code,401)
        self.client.assert_not_awaited()

    async def test_proxy_panel_serves_public_ui_assets_and_keeps_data_authenticated(self):
        self.http.cookies.clear()
        panel=await self.http.get('/api/telegram/panel')
        self.assertEqual(panel.status_code,200)
        self.assertIn('/api/telegram/panel-assets/telegram.js',panel.text)
        self.assertIn('tg-private-enabled',panel.text)
        for asset in ['main.js','telegram.js','telegram.css','style.css']:
            self.assertEqual((await self.http.get('/api/telegram/panel-assets/'+asset)).status_code,200)
        self.assertEqual((await self.http.get('/api/telegram/panel-assets/secrets.json')).status_code,404)
        self.assertEqual((await self.http.get('/api/status')).status_code,401)

    async def test_cross_origin_commands_are_rejected_without_starting_a_job(self):
        response=await self.http.post('/api/telegram/actions',json={'action':'send_text','recipient':'10','text':'Привет'},headers={'Origin':'https://other.example'})
        self.assertEqual(response.status_code,403)
        self.assertEqual(self.store.rows('SELECT * FROM telegram_action_runs'),[])

    async def test_invalid_or_extra_fields_do_not_start_a_job(self):
        for data in ({'action':'unknown'},{'action':'profile','text':'change name'},{'action':'call','owner_id':10}):
            self.assertIn((await self.http.post('/api/telegram/actions',json=data)).status_code,(400,422))
        self.assertEqual(self.store.rows('SELECT * FROM telegram_action_runs'),[])

    async def test_valid_panel_command_is_queued_and_reports_confirmed_delivery(self):
        response=await self.http.post('/api/telegram/actions',json={'action':'send_text','recipient':'10','text':'Привет'})
        self.assertEqual(response.status_code,200);self.assertTrue(response.json()['accepted'])
        result=await self.done(response.json()['run_id'])
        self.assertEqual(result['run']['status'],'complete')
        self.assertEqual(result['actions'][0]['status'],'sent')
        self.rt.telegram.send.assert_awaited_once()

    async def test_panel_command_does_not_enable_autonomy_or_change_gift_permissions(self):
        before=self.store.settings()
        response=await self.http.post('/api/telegram/actions',json={'action':'send_text','recipient':'10','text':'Привет'})
        await self.done(response.json()['run_id'])
        self.assertEqual(before,self.store.settings())

    async def test_session_revoked_during_preparation_stops_before_send(self):
        entered=asyncio.Event();release=asyncio.Event()
        original=self.client.get_entity.side_effect
        async def entity(value):
            entered.set();await release.wait();return await original(value)
        self.client.get_entity.side_effect=entity
        response=await self.http.post('/api/telegram/actions',json={'action':'send_text','recipient':'10','text':'Привет'})
        await entered.wait();self.store.execute('DELETE FROM sessions');release.set()
        await asyncio.gather(*list(self.rt.work),return_exceptions=True)
        self.assertEqual(self.store.rows('SELECT status FROM telegram_action_runs')[0]['status'],'failed')
        self.rt.telegram.send.assert_not_awaited()

    async def test_lost_delivery_is_visible_as_uncertain_and_not_retried(self):
        self.rt.telegram.send.side_effect=ConnectionError('lost acknowledgment')
        response=await self.http.post('/api/telegram/actions',json={'action':'send_text','recipient':'10','text':'Привет'})
        result=await self.done(response.json()['run_id'])
        self.assertEqual(result['run']['status'],'uncertain')
        self.assertEqual(result['actions'][0]['status'],'uncertain')
        self.rt.telegram.send.assert_awaited_once()

    async def test_freeform_command_can_request_clarification_without_any_action(self):
        self.replies({'question':'Кому позвонить?'})
        response=await self.http.post('/api/telegram/command',json={'text':'позвони ему'})
        result=await self.done(response.json()['run_id'])
        self.assertEqual(result['run']['status'],'clarification')
        self.assertEqual(result['run']['reason'],'Кому позвонить?')
        self.client.assert_not_awaited()

    async def test_freeform_command_uses_real_server_checked_action(self):
        self.replies({'actions':[{'action':'send_text','recipient':'10','text':'Привет'}]})
        response=await self.http.post('/api/telegram/command',json={'text':'напиши Оле привет'})
        result=await self.done(response.json()['run_id'])
        self.assertEqual(result['run']['status'],'complete');self.rt.telegram.send.assert_awaited_once()

    async def test_malformed_freeform_plan_does_not_execute_partially(self):
        self.replies({'actions':[{'action':'send_text','recipient':'10','text':'Привет'},{'action':'steal_session'}]})
        response=await self.http.post('/api/telegram/command',json={'text':'команда'})
        result=await self.done(response.json()['run_id'])
        self.assertEqual(result['run']['status'],'failed');self.rt.telegram.send.assert_not_awaited()

    async def test_freeform_search_results_can_inform_the_next_send(self):
        self.media.cache(document())
        self.replies({'actions':[{'action':'media_collection'}],'continue':True},
            {'actions':[{'action':'send_sticker','recipient':'10','asset_id':'sticker:123'}]})
        response=await self.http.post('/api/telegram/command',json={'text':'найди весёлый стикер и отправь Оле'})
        result=await self.done(response.json()['run_id'])
        self.assertEqual(result['run']['status'],'complete')
        self.assertEqual([a['status'] for a in result['actions']],['read','sent'])
        self.assertIn('sticker:123',str(self.rt.model.completion.call_args.args[0]))
        self.client.send_file.assert_awaited_once()

    async def test_private_initiative_controls_validate_and_return_status(self):
        response=await self.http.patch('/api/settings',json={'private_initiative_enabled':True,
            'private_initiative_min_minutes':15,'private_initiative_max_minutes':75})
        self.assertEqual(response.status_code,200)
        status=(await self.http.get('/api/telegram/autonomy')).json()
        self.assertTrue(status['private_initiative']['enabled'])
        invalid=await self.http.patch('/api/settings',json={'private_initiative_min_minutes':90})
        self.assertEqual(invalid.status_code,400)

    async def test_collection_does_not_expose_original_chat_or_document_access_hash(self):
        self.media.cache(document(),source_chat=20,source_message=4)
        response=await self.http.get('/api/telegram/media')
        self.assertEqual(response.status_code,200)
        self.assertNotIn('source_chat',response.text);self.assertNotIn('access_hash',response.text)
        self.assertNotIn('file_reference',response.text);self.assertNotIn('document',response.text)

    async def test_collection_filter_rejects_unknown_kind(self):
        self.assertEqual((await self.http.get('/api/telegram/media?kind=private_photos')).status_code,400)

    async def test_contact_call_permission_can_be_saved(self):
        response=await self.http.patch('/api/contacts/10',json={'calls_allowed':False})
        self.assertEqual(response.status_code,200);self.assertFalse(self.store.contact(10)['calls_allowed'])

    async def test_permission_change_advances_revision_and_is_visible(self):
        before=self.store.settings()['telegram_autonomy_revision']
        response=await self.http.patch('/api/settings',json={'telegram_autonomy_enabled':True,'telegram_autonomy_calls':False,'stickers_enabled':True})
        self.assertEqual(response.status_code,200)
        self.assertGreater(self.store.settings()['telegram_autonomy_revision'],before)
        response=await self.http.get('/api/telegram/autonomy')
        self.assertTrue(response.json()['enabled']);self.assertEqual(response.json()['daily_used'],0)

    async def test_check_button_requires_enabled_autonomy(self):
        response=await self.http.post('/api/telegram/autonomy/check',json={})
        self.assertEqual(response.status_code,400);self.rt.model.completion.assert_not_awaited()

    async def test_check_button_queues_one_independent_plan(self):
        self.autonomy();self.replies({'focus_id':None,'reason':'Отдохнуть'})
        response=await self.http.post('/api/telegram/autonomy/check',json={})
        self.assertTrue(response.json()['accepted'])
        await asyncio.gather(*list(self.rt.work),return_exceptions=True)
        self.assertEqual(self.store.rows('SELECT status FROM telegram_action_runs')[0]['status'],'skipped')
