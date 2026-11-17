import asyncio
import json
import os
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from app.ai import Model
from app.runtime import Deferred, Runtime, allowed_time, due_time
from app.security import password_hash, password_matches
from app.store import Store


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name))
        self.store.upsert_contact(10, "Оля")
        self.store.upsert_contact(20, "Саша")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_incoming_telegram_deduplication_is_per_contact(self):
        self.assertTrue(self.store.message(10, "user", "Привет", 100))
        self.assertIsNone(self.store.message(10, "user", "Привет", 100))
        self.assertTrue(self.store.message(20, "user", "Привет", 100))
        self.assertEqual(len(self.store.history(10)), 1)

    def test_claim_task_once_and_uncertain_after_restart(self):
        task_id = self.store.schedule(10, "Напоминание", time.time())
        self.assertTrue(self.store.claim_task(task_id))
        self.assertFalse(self.store.claim_task(task_id))
        self.store.close()
        self.store = Store(Path(self.tmp.name))
        self.assertEqual(self.store.rows("SELECT status FROM tasks")[0]["status"], "uncertain")

    def test_memory_does_not_leak_into_another_context(self):
        self.store.remember(20, "secret", "Скрытая информация Саши")
        self.store.remember(10, "music", "Джаз")
        model = Model(self.store)
        context = json.dumps(model.context(10), ensure_ascii=False)
        self.assertNotIn("Скрытая информация", context)
        self.assertIn("Джаз", context)
        asyncio.run(model.close())

    def test_password_hash_salted_and_verifies(self):
        first, second = password_hash("a-good-password"), password_hash("a-good-password")
        self.assertNotEqual(first, second)
        self.assertTrue(password_matches("a-good-password", first))
        self.assertFalse(password_matches("wrong", first))

    def test_quiet_hours_cross_midnight(self):
        settings = self.store.settings()
        settings["timezone"] = "UTC"
        now = datetime.fromisoformat("2026-11-05T23:30:00+00:00").timestamp()
        self.assertEqual(datetime.fromtimestamp(allowed_time(now, settings), __import__('datetime').timezone.utc).hour, 9)
        self.assertEqual(allowed_time(now + 12 * 3600, settings), now + 12 * 3600)
        settings["quiet_start"] = settings["quiet_end"]
        self.assertEqual(allowed_time(now, settings), now)

    def test_schedule_requires_timezone_and_future(self):
        for value in ["not-a-date", "2030-01-01T12:00", "2020-01-01T12:00+00:00"]:
            with self.assertRaises(ValueError):
                due_time(value)


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name))
        self.store.upsert_contact(10, "Оля")
        self.store.upsert_contact(20, "Саша")
        self.store.update_settings(dict(enabled=True, min_interval=0, quiet_start=0, quiet_end=0))
        self.store.update_secrets({"ai_key": "test-key"})
        self.runtime = Runtime(self.store)
        self.runtime.telegram.ready = lambda: True
        self.runtime.telegram.send = AsyncMock(return_value=500)

    async def asyncTearDown(self):
        await self.runtime.stop()
        self.store.close()
        self.tmp.cleanup()

    async def test_cannot_cancel_other_contacts_reminder(self):
        task_id = self.store.schedule(20, "Other contact", time.time() + 3600)
        result = await self.runtime.action(10, 100, "cancel_reminder", {"task_id": task_id})
        self.assertFalse(result["cancelled"])
        self.assertEqual(self.store.rows("SELECT status FROM tasks")[0]["status"], "pending")

    async def test_scheduling_requires_contact_initiative(self):
        with self.assertRaises(ValueError):
            await self.runtime.action(10, 100, "schedule_message", {"text": "Hello", "when": datetime.fromtimestamp(time.time() + 3600, __import__('datetime').timezone.utc).isoformat()})

    async def test_stop_proactive_cancels_pending_reminders(self):
        self.store.execute("UPDATE contacts SET proactive=1 WHERE id=10")
        self.store.schedule(10, "Later", time.time() + 3600)
        await self.runtime.action(10, 100, "stop_proactive", {})
        self.assertFalse(self.store.contact(10)["proactive"])
        self.assertEqual(self.store.rows("SELECT status FROM tasks")[0]["status"], "cancelled")

    async def test_scheduler_sends_once(self):
        self.store.execute("UPDATE contacts SET proactive=1,next_proactive=? WHERE id=10", (time.time() + 86400,))
        self.store.schedule(10, "Напоминание", time.time() - 1)
        await self.runtime.tick()
        await self.runtime.tick()
        self.runtime.telegram.send.assert_awaited_once()
        self.assertEqual(self.store.rows("SELECT status FROM tasks")[0]["status"], "sent")

    async def test_connection_failure_does_not_retry_delivery(self):
        self.store.execute("UPDATE contacts SET proactive=1,next_proactive=? WHERE id=10", (time.time() + 86400,))
        self.store.schedule(10, "Напоминание", time.time() - 1)
        self.runtime.telegram.send.side_effect = OSError("Lost acknowledgement")
        await self.runtime.tick()
        await self.runtime.tick()
        self.runtime.telegram.send.assert_awaited_once()
        self.assertEqual(self.store.rows("SELECT status FROM tasks")[0]["status"], "uncertain")

    async def test_pause_during_generation_prevents_send(self):
        async def answer(*args):
            self.store.update_settings({"enabled": False})
            return "Reply"
        self.runtime.model.run = answer
        await self.runtime.reply(10, "Привет", 100)
        self.runtime.telegram.send.assert_not_awaited()

    async def test_hourly_cap_defers_send(self):
        self.store.update_settings({"hourly_limit": 1})
        self.store.message(20, "assistant", "Already sent")
        with self.assertRaises(Deferred):
            await self.runtime.send(10, "Too many")
        self.runtime.telegram.send.assert_not_awaited()

    async def test_zero_limits_allow_send_after_many_messages(self):
        self.store.update_settings({"hourly_limit": 0, "daily_contact_limit": 0})
        for _ in range(120):
            self.store.message(10, "assistant", "Already sent")
        await self.runtime.send(10, "Another reply")
        self.runtime.telegram.send.assert_awaited_once_with(10, "Another reply")

    async def test_unlimited_messages_still_respect_telegram_flood_wait(self):
        self.store.update_settings({"hourly_limit": 0, "daily_contact_limit": 0})
        self.runtime.flood_until = time.time() + 60
        with self.assertRaises(Deferred):
            await self.runtime.send(10, "Wait for Telegram")
        self.runtime.telegram.send.assert_not_awaited()

    async def test_daily_cap_can_work_without_an_hourly_cap(self):
        self.store.update_settings({"hourly_limit": 0, "daily_contact_limit": 1})
        self.store.message(10, "assistant", "Already sent")
        with self.assertRaises(Deferred):
            await self.runtime.send(10, "Too many today")
        self.runtime.telegram.send.assert_not_awaited()

    async def test_reconnect_requires_existing_session_and_waits_between_attempts(self):
        self.runtime.telegram.ready = lambda: False
        self.runtime.telegram.start = AsyncMock()
        await self.runtime.reconnect()
        self.runtime.telegram.start.assert_not_awaited()
        (self.store.directory / "account.session").touch()
        await self.runtime.reconnect()
        await self.runtime.reconnect()
        self.runtime.telegram.start.assert_awaited_once()
        self.runtime.next_reconnect = 0
        self.runtime.telegram.state = "code"
        await self.runtime.reconnect()
        self.runtime.telegram.start.assert_awaited_once()


class ApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from app.api import app
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"DATA_DIR": self.tmp.name, "COOKIE_SECURE": "false", "PUBLIC_ORIGINS": "http://test"})
        self.env.start()
        self.app = app
        self.lifespan = app.router.lifespan_context(app)
        await self.lifespan.__aenter__()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers={"Origin": "http://test"})
        self.password = (Path(self.tmp.name) / "first-login.txt").read_text().splitlines()[1]

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.lifespan.__aexit__(None, None, None)
        self.env.stop()
        self.tmp.cleanup()

    async def login(self):
        response = await self.client.post("/api/login", json={"password": self.password})
        self.assertEqual(response.status_code, 200)
        self.assertIn("HttpOnly", response.headers["set-cookie"])
        self.assertIn("SameSite=strict", response.headers["set-cookie"])

    async def test_status_requires_authentication(self):
        self.assertEqual((await self.client.get("/api/status")).status_code, 401)
        await self.login()
        self.assertEqual((await self.client.get("/api/status")).status_code, 200)

    async def test_gift_permission_patch_preserves_other_contact_settings(self):
        await self.login()
        db = self.app.state.store
        db.upsert_contact(10, 'Оля')
        db.execute('UPDATE contacts SET enabled=0,proactive=1 WHERE id=10')
        response = await self.client.patch('/api/contacts/10', json={'gift_allowed':True})
        self.assertEqual(response.status_code,200,response.text)
        contact = db.contact(10)
        self.assertEqual((contact['enabled'],contact['proactive'],contact['gift_allowed']),(0,1,1))

    async def test_wrong_origin_rejected_even_when_authenticated(self):
        await self.login()
        response = await self.client.patch("/api/settings", json={"enabled": False}, headers={"Origin": "https://malicious.example"})
        self.assertEqual(response.status_code, 403)

    async def test_api_key_never_returns_to_browser(self):
        await self.login()
        response = await self.client.post("/api/ai/config", json={"base_url": "https://api.example.com/v1", "model": "test", "key": "private-model-key"})
        self.assertEqual(response.status_code, 200)
        status = await self.client.get("/api/status")
        self.assertNotIn("private-model-key", status.text)
        self.assertTrue(status.json()["ai"]["configured"])
        self.assertEqual(os.stat(Path(self.tmp.name) / "secrets.json").st_mode & 0o777, 0o600)

    async def test_cannot_enable_before_configuration(self):
        await self.login()
        self.assertEqual((await self.client.patch("/api/settings", json={"enabled": True})).status_code, 400)

    async def test_message_limits_can_be_disabled_and_saved_with_other_rules(self):
        await self.login()
        self.app.state.store.update_settings({"hourly_limit": 30, "daily_contact_limit": 30})
        response = await self.client.patch("/api/settings", json={"hourly_limit": 0, "daily_contact_limit": 0, "min_interval": 10})
        self.assertEqual(response.status_code, 200)
        status = (await self.client.get("/api/status")).json()
        self.assertEqual(status["settings"]["hourly_limit"], 0)
        self.assertEqual(status["settings"]["daily_contact_limit"], 0)
        self.assertEqual(status["settings"]["min_interval"], 10)
        self.assertEqual((await self.client.patch("/api/settings", json={"hourly_limit": -1})).status_code, 422)

    async def mock_model_http(self, handler):
        model = self.app.state.runtime.model
        await model.http.aclose()
        model.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def test_probe_uses_unsaved_values_without_changing_configuration(self):
        await self.login()
        db = self.app.state.store
        settings_before, secrets_before = db.settings(), db.secrets()
        requests = []
        def handler(request):
            requests.append(request)
            self.assertEqual(str(request.url), "https://api.example.com/v1/chat/completions")
            self.assertEqual(request.headers["Authorization"], "Bearer unsaved-test-key")
            payload = json.loads(request.content)
            self.assertEqual(payload["model"], "unsaved-model")
            self.assertNotIn("tools", payload)
            return httpx.Response(200, json={"choices": [{"message": {"content": "готова"}}]})
        await self.mock_model_http(handler)
        response = await self.client.post("/api/ai/test", json={"base_url": "https://api.example.com/v1", "model": "unsaved-model", "key": "unsaved-test-key"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(requests), 1)
        self.assertEqual(db.settings(), settings_before)
        self.assertEqual(db.secrets(), secrets_before)
        self.assertNotIn("unsaved-test-key", response.text)

    async def test_probe_blank_key_uses_saved_key_for_same_provider(self):
        await self.login()
        self.app.state.store.update_secrets({"ai_key": "saved-test-key"})
        self.app.state.store.update_settings({"ai_base_url": "https://api.example.com/v1"})
        def handler(request):
            self.assertEqual(request.headers["Authorization"], "Bearer saved-test-key")
            return httpx.Response(200, json={"choices": [{"message": {"content": "готова"}}]})
        await self.mock_model_http(handler)
        response = await self.client.post("/api/ai/test", json={"base_url": "https://api.example.com/v1", "model": "test", "key": ""})
        self.assertEqual(response.status_code, 200)

    async def test_probe_supports_already_open_panel_request(self):
        await self.login()
        self.app.state.store.update_secrets({"ai_key": "saved-test-key"})
        def handler(request):
            self.assertEqual(request.headers["Authorization"], "Bearer saved-test-key")
            return httpx.Response(200, json={"choices": [{"message": {"content": "готова"}}]})
        await self.mock_model_http(handler)
        response = await self.client.post("/api/ai/test", json={})
        self.assertEqual(response.status_code, 200)

    async def test_probe_never_sends_saved_key_to_another_provider(self):
        await self.login()
        self.app.state.store.update_secrets({"ai_key": "saved-test-key"})
        def handler(request):
            self.fail("Saved key must not be sent to another API host")
        await self.mock_model_http(handler)
        response = await self.client.post("/api/ai/test", json={"base_url": "https://another.example.com/v1", "model": "test", "key": ""})
        self.assertEqual(response.status_code, 400)
        self.assertIn("API-ключ", response.json()["detail"])

    async def test_probe_reports_rejected_key_without_echoing_provider_body(self):
        await self.login()
        await self.mock_model_http(lambda request: httpx.Response(401, json={"error": {"message": "Echoed secret: unsaved-test-key"}}))
        response = await self.client.post("/api/ai/test", json={"base_url": "https://api.example.com/v1", "model": "test", "key": "unsaved-test-key"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("Провайдер не принял", response.json()["detail"])
        self.assertNotIn("unsaved-test-key", response.text)

    async def test_invalid_timezone_rejected(self):
        await self.login()
        self.assertEqual((await self.client.patch("/api/settings", json={"timezone": "NoSuch/Zone"})).status_code, 400)

    async def test_gifting_cannot_be_enabled_without_budgets(self):
        await self.login()
        self.assertEqual((await self.client.patch('/api/settings',json={'gifts_enabled':True})).status_code,400)
        self.assertFalse(self.app.state.store.settings()['gifts_enabled'])

    async def test_invalid_contact_profile_does_not_partially_disable_contact(self):
        await self.login()
        self.app.state.store.upsert_contact(10,'Оля')
        response = await self.client.patch('/api/contacts/10',json={'enabled':False,'proactive':False,'birthday':'invalid'})
        self.assertEqual(response.status_code,400)
        self.assertTrue(self.app.state.store.contact(10)['enabled'])

    async def test_media_upload_rejects_duplicates_and_paths(self):
        await self.login()
        response = await self.client.post('/api/media',json={'filename':'file.txt','base64':'aGVsbG8='})
        self.assertEqual(response.status_code,200)
        response = await self.client.post('/api/media',json={'filename':'file.txt','base64':'aGVsbG8='})
        self.assertEqual(response.status_code,400)
        response = await self.client.post('/api/media',json={'filename':'../private.txt','base64':'aGVsbG8='})
        self.assertEqual(response.status_code,400)
        self.assertEqual((Path(self.tmp.name)/'media/file.txt').read_text(),'hello')
        response = await self.client.delete('/api/media/file.txt',headers={'Content-Type':'application/json'})
        self.assertEqual(response.status_code,200)
        self.assertFalse((Path(self.tmp.name)/'media/file.txt').exists())

    async def test_discovery_can_select_model_when_field_is_empty(self):
        await self.login()
        before = self.app.state.store.settings()
        await self.mock_model_http(lambda req:httpx.Response(200,json={'data':[{'id':'claude-opus-5'},{'id':'claude-sonnet-4-6'}]}))
        response = await self.client.post('/api/ai/models',json={'base_url':'https://api.example.com/v1','model':None,'key':'probe-key'})
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json()['selected'],'claude-sonnet-4-6')
        self.assertEqual(before,self.app.state.store.settings())

    async def test_password_rotation_invalidates_other_sessions(self):
        await self.login()
        old_cookie = self.client.cookies.get("alisa_session")
        response = await self.client.post("/api/password", json={"current": self.password, "password": "a-new-password-123"})
        self.assertEqual(response.status_code, 200)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test", cookies={"alisa_session": old_cookie}) as old:
            self.assertEqual((await old.get("/api/status")).status_code, 401)
        self.assertEqual((await self.client.get("/api/status")).status_code, 200)

    async def test_qr_image_and_link_require_auth_and_expire(self):
        from types import SimpleNamespace
        self.assertEqual((await self.client.get('/api/telegram/qr-image')).status_code,401)
        self.assertEqual((await self.client.get('/api/telegram/qr-link')).status_code,401)
        await self.login()
        tg=self.app.state.runtime.telegram
        tg.state='qr';tg.qr_svg='<svg xmlns="http://www.w3.org/2000/svg"></svg>'
        tg.qr=SimpleNamespace(url='tg://login?token=FAKE_TOKEN_ONLY')
        tg.qr_expires=time.time()+30
        image=await self.client.get('/api/telegram/qr-image')
        self.assertEqual(image.status_code,200)
        self.assertEqual(image.headers['cache-control'],'no-store')
        self.assertTrue(image.headers['content-type'].startswith('image/svg+xml'))
        self.assertEqual((await self.client.get('/api/telegram/qr-link')).status_code,200)
        tg.qr_expires=0
        self.assertEqual((await self.client.get('/api/telegram/qr-image')).status_code,400)
        self.assertEqual((await self.client.get('/api/telegram/qr-link')).status_code,400)


if __name__ == "__main__":
    unittest.main()
