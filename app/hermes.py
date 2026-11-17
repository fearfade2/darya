from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import httpx


class Hermes:
    def __init__(self, store):
        self.store = store
        self.url = os.getenv("HERMES_API_URL", "http://127.0.0.1:8642").rstrip("/")
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(180, connect=5), follow_redirects=False)

    def headers(self, contact_id=0):
        key = self.store.secrets().get("hermes_key", "")
        scope = self.store.dialog_scope(contact_id)
        return {"Authorization": "Bearer " + key, "X-Hermes-Session-Key": 'darya:dialog:' + ':'.join(map(str,scope))}

    def workspace(self, contact_id=None):
        from .dialogs import current_dialog
        scope = current_dialog.get() if contact_id is None else self.store.dialog_scope(contact_id)
        scope = scope or (0,0,0)
        return Path(os.getenv('HERMES_WORKSPACE','/data/workspace/alisa-hermes/workspace')) / ('dialog-'+'-'.join(map(str,scope)))

    async def status(self):
        try:
            response = await self.http.get(self.url + "/health", headers=self.headers(), timeout=5)
            return {"connected": response.status_code == 200, "configured": bool(self.store.secrets().get("hermes_key"))}
        except httpx.HTTPError:
            return {"connected": False, "configured": bool(self.store.secrets().get("hermes_key"))}

    async def run(self, task, contact_id=0):
        try:
            response = await self.http.post(self.url + "/v1/chat/completions", headers=self.headers(contact_id), json={
                "model": "hermes-agent", "stream": False, "model_options": {"max_tokens": 700}, "messages": [
                  {"role": "system", "content": "Ты помощник Дарьи. Выполни конкретное задание и сообщи фактический результат на русском. Твоя рабочая папка: " + str(self.workspace(contact_id)) + ". Все новые файлы сохраняй в ней. Файлы и диалоги других пользователей недоступны. Не управляй Telegram, платежами или другими аккаунтами через терминал: эти действия выполняет приложение Дарьи через свои инструменты."},
                  {"role": "user", "content": task}]})
        except httpx.HTTPError:
            raise ValueError("Hermes не ответил на задание") from None
        if response.status_code != 200:
            raise ValueError(f"Hermes вернул код {response.status_code}")
        try:
            choice = response.json()["choices"][0]
            message = choice["message"]
        except (ValueError, KeyError, TypeError, IndexError):
            raise ValueError("Hermes вернул неподдерживаемый ответ") from None
        if choice.get("finish_reason") in {"error", "cancelled"}:
            raise ValueError("Hermes не завершил задание. Проверьте лимит API провайдера")
        return str(message.get("content") or "").strip()[:24000]

    async def close(self):
        await self.http.aclose()
