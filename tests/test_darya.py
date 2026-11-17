import asyncio
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, Mock, patch

import httpx
from PIL import Image
from telethon import types

from app.ai import Model
from app.behavior import addressed
from app.runtime import Runtime
from app.store import Store
from app.telegram import Telegram
from app.vision import image_url, normalize, save_image
from app.websites import Websites, extract, validate_public_url
from app.security import create_session


def photo_bytes():
    output = io.BytesIO()
    Image.new("RGB", (100, 80), "red").save(output, "PNG")
    return output.getvalue()


class DaryaTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name))
        self.store.upsert_contact(10, "Оля")
        self.store.update_settings({"enabled": True, "natural_enabled": False, "min_interval": 0, "burst_window": 0.01})
        self.store.update_secrets({"ai_key": "test"})
        self.rt = Runtime(self.store)
        self.rt.telegram.send = AsyncMock(return_value=200)
        self.rt.telegram.ready = lambda: True

    async def asyncTearDown(self):
        await self.rt.stop()
        self.store.close()
        self.tmp.cleanup()

    def event(self, text="Что на фото?", group=False, photo=True, reply=None):
        return SimpleNamespace(raw_text=text, is_private=not group, is_group=group, is_reply=reply is not None,
            mentioned=False, chat_id=-100999, id=101,
            message=SimpleNamespace(photo=photo, document=None, file=SimpleNamespace(size=100)),
            get_sender=AsyncMock(return_value=types.User(id=10, first_name="Оля")),
            get_chat=AsyncMock(return_value=SimpleNamespace(title="Группа", username="")),
            get_reply_message=AsyncMock(return_value=reply))

    async def test_photo_caption_and_actual_image_reach_receive(self):
        receive = Mock()
        tg = Telegram(self.store, receive)
        tg.client = SimpleNamespace(download_media=AsyncMock(return_value=photo_bytes()))
        await tg.on_message(self.event())
        args, kwargs = receive.call_args
        self.assertEqual(args, (10, "Что на фото?", 101))
        saved = self.store.directory / kwargs["image_path"]
        self.assertEqual(saved.stat().st_mode & 0o777, 0o600)
        with Image.open(saved) as image:
            self.assertEqual(image.format, "JPEG")

    async def test_group_ignores_unaddressed_message_before_downloading(self):
        receive = Mock()
        tg = Telegram(self.store, receive)
        tg.client = SimpleNamespace(download_media=AsyncMock())
        await tg.on_message(self.event(text="Обсуждаем дашборд", group=True))
        receive.assert_not_called()
        tg.client.download_media.assert_not_awaited()

    async def test_group_name_and_reply_route_to_group_not_private_sender(self):
        receive = Mock()
        tg = Telegram(self.store, receive)
        tg.account_id = 50
        await tg.on_message(self.event(text="Дашка, привет", group=True, photo=False))
        self.assertEqual(receive.call_args.args[0], -100999)
        self.assertEqual(self.store.contact(-100999)["kind"], "group")
        receive.reset_mock()
        await tg.on_message(self.event(text="А почему?", group=True, photo=False, reply=SimpleNamespace(sender_id=50)))
        receive.assert_called_once()

    async def test_image_is_sent_to_model_and_description_survives_context(self):
        self.store.update_settings({"vision_model": "vision-test"})
        path = save_image(self.store.directory, photo_bytes())
        self.store.message(10, "user", "Какого цвета?", 101, image_path=path)
        def handler(request):
            payload = json.loads(request.content)
            self.assertEqual(payload["model"], "vision-test")
            self.assertNotIn("tools", payload)
            content = payload["messages"][-1]["content"]
            self.assertEqual(content[0]["text"], "Какого цвета?")
            self.assertTrue(content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,"))
            return httpx.Response(200, json={"choices": [{"message": {"content": "Красный прямоугольник"}}]})
        await self.rt.model.http.aclose()
        self.rt.model.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        await self.rt.model.prepare_images(10)
        context = json.dumps(self.rt.model.context(10), ensure_ascii=False)
        self.assertIn("Красный прямоугольник", context)
        self.assertNotIn("base64", context)

    async def test_group_context_excludes_private_memories_and_keeps_author(self):
        self.store.remember(10, "private", "Личный секрет")
        self.store.upsert_contact(-20, "Общий чат", kind="group")
        self.store.message(-20, "user", "Даша!", 2, sender_id=10, sender_name="Оля")
        context = json.dumps(self.rt.model.context(-20), ensure_ascii=False)
        self.assertNotIn("Личный секрет", context)
        self.assertIn("Оля", context)
        self.assertIn("групповой чат", context)

    async def test_group_reply_is_one_message_bound_to_the_current_request(self):
        self.store.upsert_contact(-20, 'Общий чат', kind='group')
        self.store.update_settings({'group_enabled': True, 'split_replies': True, 'typing_enabled': False,
                                    'reply_part_delay_min': 0, 'reply_part_delay_max': 0})
        self.store.message(-20, 'user', 'Даша, привет!', 11, sender_id=10)
        # Another participant spoke later: the reply must keep its original target.
        self.store.message(-20, 'user', 'Даша, а мне?', 12, sender_id=20)
        self.rt.telegram.send.side_effect = [200, 201]
        self.rt.model.run = AsyncMock(return_value='Привет!\n\nКак дела?')
        await self.rt.reply(-20, telegram_id=11, prepared=True)
        self.assertEqual(self.rt.telegram.send.await_count, 1)
        for call in self.rt.telegram.send.await_args_list:
            self.assertEqual(call.args[0], -20)
        self.assertEqual(call.kwargs['reply_to'],11)
        replies = [r for r in self.store.history(-20) if r['role'] == 'assistant']
        self.assertEqual([r['reply_to_telegram_id'] for r in replies], [11])
        self.assertTrue(all(r['context_sender_id'] == 10 for r in replies))

    async def test_private_text_reply_does_not_add_group_reply_behavior(self):
        self.store.message(10, 'user', 'Привет', 11, sender_id=10)
        self.rt.model.run = AsyncMock(return_value='Привет!')
        await self.rt.reply(10, telegram_id=11, prepared=True)
        self.assertNotIn('reply_to', self.rt.telegram.send.call_args.kwargs)

    async def test_telegram_transport_preserves_explicit_reply_target(self):
        tg = Telegram(self.store, Mock())
        tg.ready = Mock(return_value=True)
        tg.client = AsyncMock()
        tg.client.send_message.return_value = SimpleNamespace(id=200)
        self.store.upsert_contact(-20, 'Общий чат', kind='group')
        await tg.send(-20, 'Ответ', reply_to=11, immediate=True)
        self.assertEqual(tg.client.send_message.call_args.kwargs['reply_to'], 11)

    async def test_burst_is_recorded_fully_but_answered_once(self):
        self.rt.model.run = AsyncMock(return_value="Привет!")
        self.rt.receive(10, "Привет", 101)
        self.rt.receive(10, "Как дела?", 102)
        await asyncio.gather(*list(self.rt.work))
        self.rt.model.run.assert_awaited_once()
        self.rt.telegram.send.assert_awaited_once()
        self.assertEqual([m["text"] for m in self.store.history(10) if m["role"] == "user"], ["Привет", "Как дела?"])

    async def test_pause_during_read_delay_blocks_model_and_send(self):
        self.store.update_settings({"natural_enabled": True})
        self.rt.model.run = AsyncMock(return_value="Ответ")
        async def pause(seconds):
            self.store.update_settings({"enabled": False})
        with patch("app.runtime.asyncio.sleep", side_effect=pause):
            await self.rt.reply(10, "Привет", 101)
        self.rt.model.run.assert_not_awaited()
        self.rt.telegram.send.assert_not_awaited()

    async def test_reaction_only_reply_requires_successful_reaction(self):
        replies = iter([
            {"tool_calls": [{"id": "1", "function": {"name": "react", "arguments": '{"emoji":"❤"}'}}, {"id": "2", "function": {"name": "skip_message", "arguments": '{}'}}]},
            {"content": ""}])
        self.rt.model.completion = AsyncMock(side_effect=lambda *a, **k: next(replies))
        execute = AsyncMock(return_value={"reacted": True})
        self.assertEqual(await self.rt.model.run(10, execute), "")

    async def test_presence_turns_off_on_pause_without_posting(self):
        tg = self.rt.telegram
        tg.online = True
        tg.client = AsyncMock()
        tg.client.is_connected = Mock(return_value=True)
        tg.state = "connected"
        await tg.periodic_presence(False)
        request = tg.client.call_args.args[0]
        self.assertTrue(request.offline)
        tg.client.send_message.assert_not_awaited()

    async def test_disabled_reactions_never_call_telegram(self):
        self.store.update_settings({"reactions_enabled": False})
        self.rt.telegram.react = AsyncMock()
        with self.assertRaises(ValueError):
            await self.rt.action(10, 101, "react", {"emoji": "❤"})
        self.rt.telegram.react.assert_not_awaited()

    async def test_long_typing_uses_context_managers_and_sends_once(self):
        self.store.update_settings({"natural_enabled": True})
        tg = Telegram(self.store, Mock())
        tg.state = "connected"
        @asynccontextmanager
        async def action(*args):
            yield
        tg.client = SimpleNamespace(is_connected=lambda: True, action=Mock(side_effect=action), send_message=AsyncMock(return_value=SimpleNamespace(id=100)))
        with patch("app.telegram.asyncio.sleep", new=AsyncMock()):
            self.assertEqual(await tg.send(10, "а" * 300), 100)
        self.assertEqual(tg.client.action.call_count, 2)
        tg.client.send_message.assert_awaited_once()

    async def test_hermes_csv_is_imported_and_sent_to_its_contact(self):
        workspace = self.store.directory / "helper"
        workspace.mkdir()
        namespace = workspace / 'dialog-10-0-0'
        namespace.mkdir()
        source = namespace / "offers.csv"
        source.write_text("title,price\nLaptop,100\n")
        self.store.execute("UPDATE contacts SET hermes_allowed=1 WHERE id=10")
        self.rt.hermes.run = AsyncMock(return_value=f"Собраны данные. CSV: {source}")
        self.rt.telegram.send_media = AsyncMock(return_value=300)
        with patch.dict(os.environ, {"HERMES_WORKSPACE": str(workspace)}):
            job = await self.rt.submit_job("Собери объявления", 10)
            await asyncio.gather(*list(self.rt.work))
        saved = self.store.rows("SELECT * FROM jobs WHERE id=?", (job["job_id"],))[0]
        self.assertTrue(saved["artifact"])
        self.assertNotIn(str(workspace), saved["result"])
        self.assertTrue((self.store.directory / "media" / saved["artifact"]).exists())
        self.rt.telegram.send_media.assert_awaited_once()

    async def test_hermes_does_not_import_an_old_or_external_csv(self):
        workspace = self.store.directory / "helper"
        workspace.mkdir()
        namespace = workspace / 'dialog-0-0-0'
        namespace.mkdir()
        source = namespace / "old.csv"
        source.write_text("title\nOld\n")
        os.utime(source, (time.time() - 100, time.time() - 100))
        outside = self.store.directory / "private.csv"
        outside.write_text("private\nsecret\n")
        link = namespace / "link.csv"
        link.symlink_to(outside)
        with patch.dict(os.environ, {"HERMES_WORKSPACE": str(workspace)}):
            for path in [source, link]:
                artifact, result = self.rt.import_artifact(1, str(path), time.time())
                self.assertEqual(artifact, "")

    async def test_imported_hermes_csv_cannot_execute_spreadsheet_formula(self):
        workspace = self.store.directory / "helper"
        workspace.mkdir()
        namespace = workspace / 'dialog-0-0-0'
        namespace.mkdir()
        source = namespace / "offers.csv"
        source.write_text("title\n=cmd\n")
        with patch.dict(os.environ, {"HERMES_WORKSPACE": str(workspace)}):
            artifact, _ = self.rt.import_artifact(1, str(source), time.time())
        output = (self.store.directory / "media" / artifact).read_text(encoding="utf-8-sig")
        self.assertIn("'=cmd", output)

    async def test_hermes_export_is_not_visible_or_sendable_to_another_contact(self):
        directory = self.store.directory / "media"
        directory.mkdir()
        (directory / "private-export.csv").write_text("private\n")
        self.store.execute("INSERT INTO jobs(contact_id,task,created,updated,artifact) VALUES (?,?,?,?,?)", (10, "Export", time.time(), time.time(), "private-export.csv"))
        self.assertEqual(self.rt.media(20), [])
        self.assertEqual(self.rt.media(10)[0]["filename"], "private-export.csv")
        with self.assertRaises(ValueError):
            await self.rt.send_file(20, "private-export.csv", "")

    async def test_corrupt_image_and_symlink_are_rejected(self):
        with self.assertRaises(ValueError):
            normalize(b"not an image")
        path = save_image(self.store.directory, photo_bytes())
        link = self.store.directory / "incoming" / "link.jpg"
        link.symlink_to(self.store.directory / path)
        with self.assertRaises(ValueError):
            image_url(self.store.directory, "incoming/link.jpg")


class WebsiteTests(unittest.IsolatedAsyncioTestCase):
    def test_avito_cards_extract_title_price_location_and_link(self):
        html = '<title>Ноутбуки</title><div data-marker="item"><a data-marker="item-title" href="/moskva/macbook_123">MacBook</a><span data-marker="item-price">50 000 ₽</span><span data-marker="item-address">Москва</span></div>'
        result = extract(html, "https://www.avito.ru/moskva/noutbuki")
        self.assertEqual(result["items"][0], {"title": "MacBook", "price": "50 000 ₽", "location": "Москва", "url": "https://www.avito.ru/moskva/macbook_123", "description": ""})

    def test_generic_jsonld_product_and_graph_are_parsed(self):
        product = {"@graph": [{"@type": "Product", "name": "Книга", "url": "/book", "offers": {"price": "250", "priceCurrency": "RUB"}}]}
        result = extract('<script type="application/ld+json">'+json.dumps(product)+'</script>', "https://example.com")
        self.assertEqual(result["items"][0]["price"], "250 RUB")

    async def test_private_and_redirected_private_destinations_are_blocked(self):
        with patch("app.websites.socket.getaddrinfo", return_value=[(0, 0, 0, "", ("127.0.0.1", 443))]):
            with self.assertRaises(ValueError):
                await validate_public_url("https://example.com")
        tmp = tempfile.TemporaryDirectory()
        sites = Websites(SimpleNamespace(directory=Path(tmp.name), event=Mock()))
        await sites.http.aclose()
        sites.http = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(302, headers={"Location": "https://127.0.0.1/private"})))
        public = [(0,0,0,"",("93.184.216.34",443))]
        private = [(0,0,0,"",("127.0.0.1",443))]
        with patch("app.websites.socket.getaddrinfo", side_effect=[public, private]):
            with self.assertRaises(ValueError):
                await sites.fetch("https://example.com")
        await sites.close()
        tmp.cleanup()

    async def test_access_limit_is_reported_without_fake_listings(self):
        with tempfile.TemporaryDirectory() as tmp:
            sites = Websites(SimpleNamespace(directory=Path(tmp), event=Mock()))
            await sites.http.aclose()
            sites.http = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(403)))
            with patch("app.websites.validate_public_url", new=AsyncMock()):
                result = await sites.parse("https://www.avito.ru")
            self.assertTrue(result["blocked"])
            self.assertEqual(result["items"], [])
            await sites.close()

    async def test_csv_export_keeps_prices_and_neutralizes_formula(self):
        with tempfile.TemporaryDirectory() as tmp:
            sites = Websites(SimpleNamespace(directory=Path(tmp), event=Mock()))
            sites.fetch = AsyncMock(return_value={"title": "Каталог", "url": "https://example.com", "text": "", "blocked": False,
                "items": [{"title": "=danger", "price": "100 ₽", "location": "", "url": "https://example.com/1", "description": ""}]})
            result = await sites.parse("https://example.com", export=True)
            csv = (Path(tmp)/"media"/result["filename"]).read_text(encoding="utf-8-sig")
            self.assertIn("'=danger", csv)
            self.assertIn("100 ₽", csv)
            await sites.close()

    def test_name_matching_accepts_diminutives_but_rejects_substrings(self):
        for text in ["ДАРЬЯ!", "привет, Дашка", "Дашенька, смотри"]:
            self.assertTrue(addressed(text, "Дарья, Дашка, Дашенька"))
        self.assertFalse(addressed("дашборд", "Даш"))


class ApiDaryaTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from app.api import app
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"PUBLIC_ORIGINS": "http://test"})
        self.env.start()
        self.store = Store(Path(self.tmp.name))
        self.store.upsert_contact(10, "Оля")
        self.store.upsert_contact(20, "Саша")
        self.rt = Runtime(self.store)
        app.state.store, app.state.runtime = self.store, self.rt
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers={"Origin": "http://test"})
        self.client.cookies.set("alisa_session", create_session(self.store))

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.rt.stop()
        self.store.close()
        self.env.stop()
        self.tmp.cleanup()

    async def test_photo_is_authenticated_and_cannot_be_requested_for_another_contact(self):
        path = save_image(self.store.directory, photo_bytes())
        message_id = self.store.message(10, "user", "Фото", 1, image_path=path)
        result = await self.client.get(f"/api/contacts/10/photos/{message_id}")
        self.assertEqual(result.status_code, 200)
        self.assertTrue(result.headers["content-type"].startswith("image/jpeg"))
        self.assertEqual((await self.client.get(f"/api/contacts/20/photos/{message_id}")).status_code, 404)
        history = await self.client.get("/api/contacts/10/history")
        self.assertNotIn("image_path", history.text)
        self.assertTrue(history.json()["messages"][0]["has_image"])
        self.client.cookies.clear()
        self.assertEqual((await self.client.get(f"/api/contacts/10/photos/{message_id}")).status_code, 401)

    async def test_bad_delay_range_does_not_partially_change_settings(self):
        before = self.store.settings()
        result = await self.client.patch("/api/settings", json={"read_delay_min": 25, "read_delay_max": 2, "typing_cps": 10})
        self.assertEqual(result.status_code, 400)
        self.assertEqual(before, self.store.settings())

    async def test_parser_requires_auth_and_validates_crawl_limits(self):
        self.assertEqual((await self.client.post("/api/websites/parse", json={"url": "https://example.com", "pages": 4})).status_code, 422)
        self.client.cookies.clear()
        self.assertEqual((await self.client.post("/api/websites/parse", json={"url": "https://example.com"})).status_code, 401)

    async def test_panel_can_send_voice_as_reply_and_returns_telegram_id(self):
        self.store.message(10, 'user', 'Гс отправь', 619, sender_id=10)
        self.rt.send_voice_reply = AsyncMock(return_value=700)
        result = await self.client.post('/api/contacts/10/send', json={'text': 'Вот, голосом!', 'format': 'voice', 'reply_to': 619})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json(), {'ok': True, 'telegram_id': 700, 'format': 'voice'})
        self.assertEqual(self.rt.send_voice_reply.call_args.args, (10, 619, 'Вот, голосом!'))
        self.rt.send_voice_reply.call_args.kwargs['before_dispatch']()

    async def test_panel_voice_requires_auth_and_matching_current_reply_target(self):
        self.store.message(20, 'user', 'Гс отправь', 619, sender_id=20)
        self.rt.send_voice_reply = AsyncMock()
        payload = {'text': 'Привет', 'format': 'voice', 'reply_to': 619}
        self.assertEqual((await self.client.post('/api/contacts/10/send', json=payload)).status_code, 400)
        self.client.cookies.clear()
        self.assertEqual((await self.client.post('/api/contacts/20/send', json=payload)).status_code, 401)
        self.rt.send_voice_reply.assert_not_awaited()

    async def test_panel_group_voice_binds_target_author_and_rechecks_archive(self):
        from app.dialogs import current_dialog
        self.store.upsert_contact(-20, 'Группа', kind='group')
        self.store.message(-20, 'user', 'Гс отправь', 619, sender_id=10)
        self.store.message(-20, 'user', 'Даша, привет', 620, sender_id=20)
        async def send(*args, before_dispatch):
            self.assertEqual(current_dialog.get(), (-20, 10, 0))
            self.store.archive_contexts()
            before_dispatch()
        self.rt.send_voice_reply = AsyncMock(side_effect=send)
        response = await self.client.post('/api/contacts/-20/send', json={'text': 'Вот!', 'format': 'voice', 'reply_to': 619})
        self.assertEqual(response.status_code, 400)
        self.assertIsNone(current_dialog.get())


if __name__ == "__main__":
    unittest.main()
