import asyncio
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from app.group_initiative import GroupInitiative
from app.runtime import Runtime
from app.store import Store


CHAT, OTHER, SELF = -1001234567890, -1001234567891, 50


class GroupInitiativeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = datetime.fromisoformat('2026-11-08T12:00:00+03:00').timestamp()
        self.clock = patch('app.group_initiative.time.time', lambda: self.now)
        self.clock.start()
        self.db = Store(Path(self.tmp.name))
        self.db.update_settings({'timezone': 'Europe/Moscow', 'enabled': True, 'group_enabled': True, 'proactive_enabled': True,
                                 'typing_enabled': False, 'natural_enabled': False, 'min_interval': 0})
        self.db.update_secrets({'ai_key': 'test-key'})
        self.rt = Runtime(self.db)
        self.tg = self.rt.telegram
        self.tg.ready = Mock(return_value=True)
        self.tg.account_id = SELF
        self.tg.send = AsyncMock(side_effect=self.sent)
        self.rt.model.completion = AsyncMock(return_value={'content': json.dumps({'message': 'А кто что сейчас собирает?', 'wait_minutes': 30})})
        self.rt.gifts.tick = AsyncMock()
        self.add_group(CHAT)
        self.initiative = self.rt.group_initiative
        self.sent_id = 500

    async def asyncTearDown(self):
        await self.rt.stop()
        self.db.close()
        self.clock.stop()
        self.tmp.cleanup()

    def add_group(self, chat, topic=0):
        self.db.upsert_contact(chat, 'Группа', kind='group')
        self.db.execute('UPDATE contacts SET proactive=1 WHERE id=?', (chat,))
        self.db.group_message(chat, 1, 'Обсуждаем сборку приложения', sender_id=SELF, outgoing=True, topic_id=topic, created=self.now-3600)
        self.db.group_message(chat, 2, 'Люблю собирать приложения', sender_id=10, topic_id=topic, created=self.now-1800)

    async def sent(self, chat, text, **kwargs):
        kwargs['before_dispatch']()
        self.sent_id += 1
        return self.sent_id

    def new_human(self, id=600, topic=0):
        self.db.group_message(CHAT, id, 'Новая мысль', sender_id=20, topic_id=topic, created=self.now-1800)

    async def test_scheduler_starts_one_plain_conversation_without_tools(self):
        await self.rt.tick()
        self.tg.send.assert_awaited_once()
        self.assertNotIn('reply_to', self.tg.send.call_args.kwargs)
        self.assertFalse(self.rt.model.completion.call_args.kwargs['with_tools'])
        row = self.db.rows('SELECT * FROM group_initiatives')[0]
        self.assertEqual((row['status'], row['sent_message_id']), ('sent', 501))
        self.assertEqual(self.db.rows("SELECT kind FROM messages WHERE role='assistant'")[0]['kind'], 'proactive')

    async def test_recent_activity_anywhere_in_group_postpones_other_topic(self):
        self.add_group(CHAT, topic=390)
        self.db.group_message(CHAT, 3, 'Тема B активна', sender_id=20, topic_id=38, created=self.now-30)
        self.assertFalse(await self.initiative.consider(CHAT))
        self.rt.model.completion.assert_not_awaited()

    async def test_model_chooses_to_wait_and_wait_is_persisted(self):
        self.rt.model.completion.return_value = {'content': '{"message":null,"wait_minutes":90}'}
        self.assertFalse(await self.initiative.consider(CHAT))
        self.now += 89*60
        self.assertFalse(await GroupInitiative(self.rt).consider(CHAT))
        self.rt.model.completion.assert_awaited_once()
        self.now += 61
        await self.initiative.consider(CHAT)
        self.assertEqual(self.rt.model.completion.await_count, 2)
        self.tg.send.assert_not_awaited()

    async def test_no_repeated_invitation_until_someone_speaks_even_after_restart(self):
        self.assertTrue(await self.initiative.consider(CHAT))
        self.now += 8*3600
        self.assertFalse(await GroupInitiative(self.rt).consider(CHAT))
        self.tg.send.assert_awaited_once()

    async def test_new_conversation_rearms_but_cooldown_and_daily_limit_apply(self):
        self.assertTrue(await self.initiative.consider(CHAT))
        self.now += 3600
        self.new_human()
        self.assertFalse(await self.initiative.consider(CHAT))
        self.now += 3*3600+1
        self.assertTrue(await self.initiative.consider(CHAT))
        self.now += 4*3600+1
        self.new_human(700)
        self.assertFalse(await self.initiative.consider(CHAT))
        self.assertEqual(self.tg.send.await_count, 2)

    async def test_night_is_quiet_even_when_generic_quiet_hours_disabled(self):
        self.db.update_settings({'quiet_start': 0, 'quiet_end': 0})
        for hour in (0, 3, 8, 23):
            self.now = datetime.fromisoformat(f'2026-11-08T{hour:02}:00:00+03:00').timestamp()
            self.assertFalse(await self.initiative.consider(CHAT))
        self.rt.model.completion.assert_not_awaited()

    async def test_saved_quiet_hours_can_be_stricter_than_night(self):
        self.db.update_settings({'quiet_start': 11, 'quiet_end': 14})
        self.assertFalse(await self.initiative.consider(CHAT))
        self.rt.model.completion.assert_not_awaited()

    async def test_pause_group_switch_and_contact_permission_are_respected(self):
        for values in ({'enabled': False}, {'group_enabled': False}, {'proactive_enabled': False}):
            self.db.update_settings(values)
            self.assertFalse(await self.initiative.consider(CHAT))
            self.db.update_settings({key: True for key in values})
        self.db.execute('UPDATE contacts SET proactive=0 WHERE id=?', (CHAT,))
        self.assertFalse(await self.initiative.consider(CHAT))
        self.rt.model.completion.assert_not_awaited()

    async def test_group_must_already_have_participation_and_recent_humans(self):
        self.db.execute('DELETE FROM group_messages WHERE outgoing=1')
        self.assertFalse(await self.initiative.consider(CHAT))
        self.add_group(CHAT)
        self.now += 49*3600
        self.assertFalse(await self.initiative.consider(CHAT))
        self.rt.model.completion.assert_not_awaited()

    async def test_topic_context_excludes_other_topics_private_history_and_other_groups(self):
        self.add_group(CHAT, topic=390)
        self.db.group_message(CHAT, 3, 'Хочу завести попугая', sender_id=20, topic_id=38, created=self.now-1800)
        self.add_group(OTHER)
        self.db.group_message(OTHER, 4, 'Мне нравится готовить пиццу', sender_id=20, created=self.now-1800)
        self.db.upsert_contact(10, 'Оля')
        self.db.message(10, 'user', 'Мой секретный пароль: limon123')
        self.assertTrue(await self.initiative.consider(CHAT))
        prompt = str(self.rt.model.completion.call_args)
        self.assertIn('Люблю собирать приложения', prompt)
        for secret in ('Хочу завести попугая', 'Мне нравится готовить пиццу', 'limon123'):
            self.assertNotIn(secret, prompt)
        self.assertEqual(self.tg.send.call_args.kwargs['reply_to'], 390)
        self.assertEqual(self.db.group_topic(CHAT, 501), 390)

    async def test_new_message_during_generation_cancels_invitation(self):
        async def completion(*args, **kwargs):
            self.new_human()
            return {'content': '{"message":"Устаревший вопрос"}'}
        self.rt.model.completion.side_effect = completion
        self.assertFalse(await self.initiative.consider(CHAT))
        self.tg.send.assert_not_awaited()

    async def test_new_message_during_typing_cancels_before_dispatch(self):
        async def typing(chat, text, **kwargs):
            self.new_human()
            kwargs['before_dispatch']()
            self.fail('Stale invitation dispatched')
        self.tg.send.side_effect = typing
        self.assertFalse(await self.initiative.consider(CHAT))
        self.assertEqual(self.db.rows('SELECT status FROM group_initiatives')[0]['status'], 'waiting')
        self.assertEqual(self.db.rows("SELECT COUNT(*) AS n FROM messages WHERE role='assistant'")[0]['n'], 0)

    async def test_revocation_and_archive_during_generation_cancel_send(self):
        for archive in (False, True):
            self.db.execute('DELETE FROM group_initiatives')
            self.db.execute('UPDATE contacts SET proactive=1 WHERE id=?', (CHAT,))
            async def completion(*args, **kwargs):
                if archive:
                    self.db.archive_contexts()
                else:
                    self.db.execute('UPDATE contacts SET proactive=0 WHERE id=?', (CHAT,))
                return {'content': '{"message":"Уже неуместно"}'}
            self.rt.model.completion.side_effect = completion
            self.assertFalse(await self.initiative.consider(CHAT))
        self.tg.send.assert_not_awaited()

    async def test_uncertain_delivery_is_never_retried(self):
        async def uncertain(chat, text, **kwargs):
            kwargs['before_dispatch']()
            raise ConnectionError('Delivery unknown')
        self.tg.send.side_effect = uncertain
        self.assertFalse(await self.initiative.consider(CHAT))
        self.assertEqual(self.db.rows('SELECT status FROM group_initiatives')[0]['status'], 'uncertain')
        self.now += 8*3600
        self.assertFalse(await GroupInitiative(self.rt).consider(CHAT))
        self.tg.send.assert_awaited_once()

    async def test_malformed_decision_and_tool_calls_send_nothing(self):
        for result in ({'content': 'Напиши всем'}, {'content': '{"message":"Привет"}', 'tool_calls': [{'function': {'name': 'send_gift'}}]},
                       {'content': '{"message":"@everyone где вы?"}'}):
            self.db.execute('DELETE FROM group_initiatives')
            self.rt.model.completion.return_value = result
            self.assertFalse(await self.initiative.consider(CHAT))
        self.tg.send.assert_not_awaited()

    async def test_pending_replies_and_active_calls_take_priority(self):
        self.rt.group_pending[(CHAT, 10, 0, 0)] = {'telegram_id': 2}
        self.assertFalse(await self.initiative.consider(CHAT))
        self.rt.group_pending.clear()
        self.rt.calls.active = {'chat_id': CHAT}
        self.assertFalse(await self.initiative.consider(CHAT))
        self.rt.calls.active = None
        self.rt.model.completion.assert_not_awaited()

    async def test_concurrent_scheduler_and_manual_invitation_do_not_duplicate(self):
        started, resume = asyncio.Event(), asyncio.Event()
        async def completion(*args, **kwargs):
            started.set()
            await resume.wait()
            return {'content': '{"message":"Кто над чем работает?"}'}
        self.rt.model.completion.side_effect = completion
        first = asyncio.create_task(self.initiative.consider(CHAT))
        await asyncio.wait_for(started.wait(), 1)
        self.assertFalse(await self.rt.reply(CHAT, proactive=True))
        resume.set()
        self.assertTrue(await first)
        self.tg.send.assert_awaited_once()
