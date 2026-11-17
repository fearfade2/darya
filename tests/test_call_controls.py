import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from app.api import app
from app.security import create_session
from app.store import Store


class CallControlsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Store(Path(self.tmp.name))
        self.previous = (getattr(app.state, 'store', None), getattr(app.state, 'runtime', None))
        self.join = AsyncMock(return_value={'connected': True})
        app.state.store = self.db
        app.state.runtime = SimpleNamespace(calls=SimpleNamespace(join=self.join))
        self.env = patch.dict('os.environ', {'PUBLIC_ORIGINS': 'http://test'})
        self.env.start()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test', headers={'Origin': 'http://test'})

    async def asyncTearDown(self):
        await self.client.aclose()
        app.state.store, app.state.runtime = self.previous
        self.env.stop()
        self.db.close()
        self.tmp.cleanup()

    async def test_join_requires_panel_authentication_and_allowed_origin(self):
        data = {'target': 'https://t.me/call/abcdef', 'mode': 'talk'}
        self.assertEqual((await self.client.post('/api/calls/join', json=data)).status_code, 401)
        self.client.cookies.set('alisa_session', create_session(self.db))
        self.assertEqual((await self.client.post('/api/calls/join', json=data, headers={'Origin': 'https://evil.test'})).status_code, 403)
        self.join.assert_not_awaited()

    async def test_join_uses_saved_mode_and_rechecks_session_before_connecting(self):
        self.client.cookies.set('alisa_session', create_session(self.db))
        self.db.update_settings({'call_mode': 'talk'})
        response = await self.client.post('/api/calls/join', json={'target': 'https://t.me/call/abcdef'})
        self.assertEqual(response.status_code, 200)
        args = self.join.call_args.args
        self.assertEqual(args[:2], ('https://t.me/call/abcdef', 'talk'))
        args[2]()
        self.db.execute('DELETE FROM sessions')
        with self.assertRaises(Exception) as error:
            args[2]()
        self.assertEqual(error.exception.status_code, 401)

    async def test_join_validates_mode_and_rejects_unexpected_input(self):
        self.client.cookies.set('alisa_session', create_session(self.db))
        for data in [{'target': 'https://t.me/call/abcdef', 'mode': 'dial'}, {'target': ''},
                     {'target': 'https://t.me/call/abcdef', 'code': 'execute'}]:
            self.assertEqual((await self.client.post('/api/calls/join', json=data)).status_code, 422)
        self.join.assert_not_awaited()

    async def test_frequent_initiative_settings_accept_zero_limit_and_five_seconds(self):
        from app.settings import Settings
        from pydantic import ValidationError
        values = {'call_initiative_seconds': 5, 'call_initiative_max_unanswered': 0}
        self.assertEqual(Settings(**values).model_dump(exclude_none=True), values)
        for invalid in [{'call_initiative_seconds': 4}, {'call_initiative_max_unanswered': -1},
                        {'call_initiative_max_unanswered': 21}]:
            with self.assertRaises(ValidationError):
                Settings(**invalid)
