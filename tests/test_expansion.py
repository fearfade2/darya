import asyncio
import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from telethon import functions, types

from app.ai import Model
from app.gifts import Gifts, PurchaseBlocked
from app.runtime import Runtime
from app.store import Store
from app.telegram import Telegram


class ExpansionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name))
        self.store.upsert_contact(10, 'Оля')
        self.store.upsert_contact(20, 'Саша')
        self.store.update_settings(dict(enabled=True, quiet_start=0, quiet_end=0, min_interval=0,
                                       gifts_enabled=True, gift_autonomous_enabled=True, gift_max_price=100, gift_monthly_budget=150))
        self.store.execute('UPDATE contacts SET gift_allowed=1')
        self.store.update_secrets({'ai_key': 'test'})
        self.rt = Runtime(self.store)
        self.rt.telegram.ready = lambda: True
        self.rt.telegram.gift_catalog = AsyncMock(return_value=[{'id':'1','stars':100,'available':True}])
        self.rt.telegram.stars_balance = AsyncMock(return_value=1000)
        self.rt.telegram.send_gift = AsyncMock()
        self.rt.telegram.send = AsyncMock(return_value=123)

    async def asyncTearDown(self):
        await self.rt.stop()
        self.store.close()
        self.tmp.cleanup()

    async def test_parallel_gifts_cannot_exceed_monthly_budget(self):
        results = await asyncio.gather(self.rt.gifts.send(10,'1','Привет'), self.rt.gifts.send(20,'1','Привет'), return_exceptions=True)
        self.assertEqual(sum(isinstance(r,dict) for r in results),1)
        self.assertEqual(self.rt.gifts.spent(),100)
        self.rt.telegram.send_gift.assert_awaited_once()

    async def test_requested_gift_uses_real_balance_and_saved_budget(self):
        self.store.update_settings({'gift_monthly_budget':100})
        self.rt.telegram.stars_balance.return_value=21
        self.rt.telegram.gift_catalog.return_value=[{'id':str(stars),'title':'подарок','stars':stars,'available':True} for stars in (100,15,25)]
        self.rt.model.completion=AsyncMock(return_value={'content':'{"gift_id":"15","message":"Для тебя"}'})
        result=await self.rt.gifts.send_requested(10,'любой',self.rt.model,lambda:None)
        self.assertTrue(result['sent']);self.assertEqual(result['stars'],15)
        data=json.loads(self.rt.model.completion.call_args.args[0][1]['content'])
        self.assertEqual([g['stars'] for g in data['gifts']],[15])
        self.rt.telegram.send_gift.assert_awaited_once()
        self.assertEqual(self.rt.telegram.send_gift.call_args.kwargs['expected_stars'],15)

    async def test_requested_gift_refuses_disabled_policy_before_model_or_catalog(self):
        self.store.update_settings({'gifts_enabled':False,'gift_monthly_budget':100})
        self.rt.model.completion=AsyncMock()
        with self.assertRaisesRegex(ValueError,'общим переключателем'):
            await self.rt.gifts.send_requested(10,'сердечко',self.rt.model,lambda:None)
        self.rt.telegram.gift_catalog.assert_not_awaited();self.rt.model.completion.assert_not_awaited()
        self.rt.telegram.send_gift.assert_not_awaited()

    async def test_requested_gift_invalid_selection_never_reserves_money(self):
        self.rt.model.completion=AsyncMock()
        for response in ({'content':'{"gift_id":"999"}'},{'content':'{"gift_id":null}'},
                         {'content':'```'},{'content':'{"gift_id":"1"}','tool_calls':[{}]}):
            with self.subTest(response=response):
                self.rt.model.completion.return_value=response
                with self.assertRaises(ValueError):
                    await self.rt.gifts.send_requested(10,'сердечко',self.rt.model,lambda:None)
        self.rt.telegram.send_gift.assert_not_awaited()
        self.assertEqual(self.store.rows('SELECT * FROM gift_orders'),[])

    async def test_requested_gift_rechecks_owner_before_payment(self):
        allowed=True
        def guard():
            if not allowed:raise ValueError('Владелец изменился')
        async def send(*args,expected_stars,before_payment):
            nonlocal allowed
            allowed=False
            before_payment()
            self.fail('Payment must not proceed')
        self.rt.telegram.send_gift.side_effect=send
        self.rt.model.completion=AsyncMock(return_value={'content':'{"gift_id":"1"}'})
        with self.assertRaises(PurchaseBlocked):
            await self.rt.gifts.send_requested(10,'любой',self.rt.model,guard)
        self.assertEqual(self.store.rows('SELECT status FROM gift_orders'),[{'status':'failed'}])
        self.assertEqual(self.rt.gifts.spent(),0)

    async def test_unknown_payment_is_reserved_and_not_retried(self):
        self.rt.telegram.send_gift.side_effect = OSError('Acknowledgment lost')
        for _ in range(2):
            with self.assertRaises(ValueError):
                await self.rt.gifts.send(10,'1','Привет')
        self.rt.telegram.send_gift.assert_awaited_once()
        self.assertEqual(self.rt.gifts.spent(),100)
        self.assertEqual(self.store.rows('SELECT status FROM gift_orders')[0]['status'],'uncertain')

    async def test_recipient_and_individual_price_are_enforced(self):
        self.store.execute('UPDATE contacts SET gift_allowed=0 WHERE id=10')
        with self.assertRaises(ValueError):
            await self.rt.gifts.send(10,'1','Привет')
        self.store.update_settings({'gift_max_price':99})
        with self.assertRaises(ValueError):
            await self.rt.gifts.send(20,'1','Привет')
        self.rt.telegram.send_gift.assert_not_awaited()

    async def test_insufficient_stars_never_reserve_or_purchase_a_gift(self):
        self.rt.telegram.stars_balance.return_value=99
        with self.assertRaises(ValueError):
            await self.rt.gifts.send(10,'1','Привет')
        self.assertEqual(self.rt.gifts.spent(),0)
        self.assertEqual(self.store.rows('SELECT * FROM gift_orders'),[])
        self.rt.telegram.send_gift.assert_not_awaited()

    async def test_pause_during_catalog_fetch_prevents_purchase(self):
        async def catalog():
            self.store.update_settings({'enabled':False})
            return [{'id':'1','stars':100,'available':True}]
        self.rt.telegram.gift_catalog.side_effect = catalog
        with self.assertRaises(ValueError):
            await self.rt.gifts.send(10,'1','Привет',automatic=True)
        self.rt.telegram.send_gift.assert_not_awaited()

    async def test_policy_is_checked_again_before_payment(self):
        async def send(*args, expected_stars, before_payment):
            self.store.update_settings({'gift_monthly_budget':0})
            before_payment()
        self.rt.telegram.send_gift.side_effect = send
        with self.assertRaises(PurchaseBlocked):
            await self.rt.gifts.send(10,'1','Привет')
        self.assertEqual(self.rt.gifts.spent(),0)
        self.assertEqual(self.store.rows('SELECT status FROM gift_orders')[0]['status'],'failed')

    async def test_cooldown_and_month_rollover(self):
        await self.rt.gifts.send(10,'1','Привет')
        self.store.update_settings({'gift_monthly_budget':1000})
        with self.assertRaises(ValueError):
            await self.rt.gifts.send(10,'1','Снова')
        self.store.execute("UPDATE gift_orders SET month='2020-01'")
        self.assertEqual(self.rt.gifts.spent(),0)

    async def test_payment_crossing_month_boundary_counts_new_month(self):
        self.rt.gifts.month = lambda:'2026-11'
        async def send(*args, expected_stars, before_payment):
            self.rt.gifts.month = lambda:'2026-12'
            before_payment()
        self.rt.telegram.send_gift.side_effect = send
        await self.rt.gifts.send(10,'1','Привет')
        self.assertEqual(self.store.rows('SELECT month FROM gift_orders')[0]['month'],'2026-12')
        self.assertEqual(self.rt.gifts.spent(),100)
        with self.assertRaises(ValueError):
            await self.rt.gifts.send(20,'1','Привет')

    async def test_discovery_normalizes_without_mutating_connection(self):
        before = self.store.settings()
        await self.rt.model.http.aclose()
        self.rt.model.http = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200,json={
            'data':[{'id':'beta'},{'id':'alpha'},{'id':'beta'},{'id':'sk-secret'},{'invalid':1}]})))
        self.assertEqual(await self.rt.model.discover(),['alpha','beta'])
        self.assertEqual(before,self.store.settings())

    async def test_saved_key_is_scoped_and_panel_can_override_cloud_key(self):
        self.store.update_settings({'ai_base_url':'https://api.example.com/v1'})
        self.store.update_secrets({'ai_key':'panel','ai_key_host':'api.example.com'})
        with patch.dict(os.environ,{'ALISA_AI_KEY':'cloud','ALISA_AI_KEY_HOST':'api.example.com'}):
            self.assertEqual(self.rt.model.key(),'panel')
            self.store.update_settings({'ai_base_url':'https://other.example.com/v1'})
            self.assertEqual(self.rt.model.key(),'')

    async def test_hermes_permission_and_contact_session_separation(self):
        with self.assertRaises(ValueError):
            await self.rt.submit_job('Task',10)
        self.store.execute('UPDATE contacts SET hermes_allowed=1 WHERE id=10')
        self.rt.hermes.run = AsyncMock(return_value='Готово')
        job = await self.rt.submit_job('Task',10)
        await asyncio.gather(*list(self.rt.work))
        row = self.store.rows('SELECT * FROM jobs WHERE id=?',(job['job_id'],))[0]
        self.assertEqual(row['status'],'completed')
        self.rt.hermes.run.assert_awaited_once_with('Task',10)
        self.assertNotEqual(self.rt.hermes.headers(10)['X-Hermes-Session-Key'],self.rt.hermes.headers(20)['X-Hermes-Session-Key'])

    async def test_hermes_error_response_never_becomes_a_completed_job(self):
        await self.rt.hermes.http.aclose()
        self.rt.hermes.http = httpx.AsyncClient(transport=httpx.MockTransport(lambda req:httpx.Response(200,json={
            'choices':[{'finish_reason':'error','message':{'content':'private-api-key'}}]})))
        job = await self.rt.submit_job('Task')
        await asyncio.gather(*list(self.rt.work))
        row = self.store.rows('SELECT * FROM jobs WHERE id=?',(job['job_id'],))[0]
        self.assertEqual(row['status'],'failed')
        self.assertNotIn('private-api-key',row['result'])

    async def test_media_traversal_and_symlinks_cannot_be_sent(self):
        root = self.store.directory/'media'
        root.mkdir()
        (root/'escape').symlink_to(self.store.secret_path)
        for name in ['../secrets.json','escape']:
            with self.assertRaises(ValueError):
                await self.rt.send_file(10,name,'')
        self.assertEqual(self.rt.media(),[])

    async def test_recurring_reminder_only_repeats_after_confirmed_send(self):
        self.store.update_settings({'gifts_enabled':False})
        self.store.execute('UPDATE contacts SET proactive=1,next_proactive=?',(time.time()+86400,))
        task = self.store.schedule(10,'Каждый день',time.time()-1)
        self.store.execute('UPDATE tasks SET repeat_hours=24 WHERE id=?',(task,))
        await self.rt.tick()
        rows = self.store.rows('SELECT * FROM tasks ORDER BY id')
        self.assertEqual([r['status'] for r in rows],['sent','pending'])
        self.assertEqual(rows[1]['repeat_hours'],24)
        self.assertGreaterEqual(rows[1]['due']-time.time(),24*3600-2)
        self.store.execute('UPDATE tasks SET due=? WHERE status="pending"',(time.time()-1,))
        self.rt.telegram.send.side_effect = OSError()
        await self.rt.tick()
        self.assertEqual(len(self.store.rows('SELECT * FROM tasks')),2)

    async def test_autonomous_gift_checks_respect_cadence(self):
        self.store.execute('UPDATE contacts SET gift_allowed=0 WHERE id=20')
        self.rt.model.completion = AsyncMock(return_value={'content':'{"gift_id":"1","message":"Для тебя"}'})
        await self.rt.gifts.tick(self.rt.model)
        await self.rt.gifts.tick(self.rt.model)
        self.rt.model.completion.assert_awaited_once()
        self.rt.telegram.send_gift.assert_awaited_once()
        self.assertEqual(self.store.rows('SELECT automatic FROM gift_orders')[0]['automatic'],1)

    async def test_uncertain_order_survives_restart_without_budget_release(self):
        self.store.execute('INSERT INTO gift_orders(contact_id,gift_id,stars,month,created) VALUES(?,?,?,?,?)',
                           (10,'1',100,self.rt.gifts.month(),time.time()))
        await self.rt.stop()
        self.store.close()
        self.store = Store(Path(self.tmp.name))
        self.rt = Runtime(self.store)
        self.assertEqual(self.store.rows('SELECT status FROM gift_orders')[0]['status'],'uncertain')
        self.assertEqual(self.rt.gifts.spent(),100)


class MigrationTests(unittest.TestCase):
    def test_old_database_keeps_contacts_and_gifting_defaults_to_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = sqlite3.connect(Path(tmp)/'alisa.sqlite')
            db.executescript("CREATE TABLE contacts(id INTEGER PRIMARY KEY,name TEXT,username TEXT DEFAULT '',enabled INTEGER DEFAULT 1,proactive INTEGER DEFAULT 0,last_activity REAL DEFAULT 0,next_proactive REAL DEFAULT 0); INSERT INTO contacts(id,name) VALUES(10,'Оля');")
            db.close()
            store = Store(Path(tmp))
            self.assertEqual(store.contact(10)['name'],'Оля')
            self.assertEqual(store.contact(10)['gift_allowed'],0)
            self.assertFalse(store.settings()['gifts_enabled'])
            self.assertEqual(store.settings()['gift_monthly_budget'],0)
            store.close()


class TelegramPaymentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name))

    async def asyncTearDown(self):
        self.store.close()
        self.tmp.cleanup()

    async def test_form_preparation_failure_cannot_start_a_payment(self):
        telegram=Telegram(self.store,None)
        telegram.ready=lambda:True
        telegram.client=AsyncMock(side_effect=OSError('Connection lost before payment'))
        telegram.client.get_input_entity.return_value=types.InputPeerUser(10,1)
        with self.assertRaises(PurchaseBlocked):
            await telegram.send_gift(10,1,'Привет',100,lambda:None)
        self.assertTrue(all(isinstance(call.args[0],functions.payments.GetPaymentFormRequest) for call in telegram.client.call_args_list))

    async def test_price_change_and_revoked_policy_block_final_charge(self):
        telegram = Telegram(self.store,None)
        telegram.ready = lambda: True
        telegram.client = AsyncMock()
        telegram.client.get_input_entity.return_value = types.InputPeerUser(10,1)
        form = types.payments.PaymentFormStarGift(12,types.Invoice('XTR',[types.LabeledPrice('gift',101)]))
        telegram.client.return_value = form
        for cost,guard in [(100,lambda:None),(101,lambda:(_ for _ in ()).throw(PurchaseBlocked('Disabled')))]:
            with self.assertRaises(PurchaseBlocked):
                await telegram.send_gift(10,1,'Привет',cost,guard)
        self.assertTrue(all(isinstance(call.args[0],functions.payments.GetPaymentFormRequest) for call in telegram.client.call_args_list))

