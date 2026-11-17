import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock

from app.runtime import Runtime
from app.ai import ModelUnavailable
from app.store import Store


class PrivateGiftContextTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name))
        self.store.upsert_contact(10, 'Собеседник')
        self.store.update_settings({'enabled': True, 'gifts_enabled': True,
                                    'gift_max_price': 100, 'gift_monthly_budget': 100})
        self.store.execute('UPDATE contacts SET gift_allowed=1 WHERE id=10')
        self.rt = Runtime(self.store)
        self.rt.telegram.gift_catalog = AsyncMock(return_value=[
            {'id': '15', 'title': '🧸', 'stars': 15, 'available': True},
            {'id': '25', 'title': '❤️', 'stars': 25, 'available': True}])
        self.rt.telegram.stars_balance = AsyncMock(return_value=21)
        self.rt.telegram.send_gift = AsyncMock()

    async def asyncTearDown(self):
        await self.rt.stop()
        self.store.close()
        self.tmp.cleanup()

    async def inspect(self):
        return await self.rt.action(10, 100, 'list_gifts', {})

    async def test_disabled_global_switch_is_explained_even_with_recipient_permission(self):
        self.store.update_settings({'gifts_enabled': False})
        result = await self.inspect()
        self.assertIn('общим переключателем', result['reason'])
        self.assertEqual(result['remaining_budget'], 100)
        self.assertIsNone(result['balance'])
        self.rt.telegram.gift_catalog.assert_not_awaited()
        self.rt.telegram.stars_balance.assert_not_awaited()

    async def test_disabled_recipient_is_distinct_from_global_switch(self):
        self.store.execute('UPDATE contacts SET gift_allowed=0 WHERE id=10')
        result = await self.inspect()
        self.assertIn('получателя', result['reason'])
        self.assertNotIn('общим', result['reason'])
        self.rt.telegram.gift_catalog.assert_not_awaited()

    async def test_catalog_respects_real_balance_without_purchasing(self):
        before = self.store.settings()
        result = await self.inspect()
        self.assertTrue(result['available'])
        self.assertIsNone(result['reason'])
        self.assertEqual(result['balance'], 21)
        self.assertEqual([gift['id'] for gift in result['gifts']], ['15'])
        self.assertEqual(self.store.rows('SELECT * FROM gift_orders'), [])
        self.assertEqual(self.store.settings(), before)
        self.rt.telegram.send_gift.assert_not_awaited()

    async def test_insufficient_balance_is_distinct_from_permission(self):
        self.rt.telegram.stars_balance.return_value = 14
        result = await self.inspect()
        self.assertIn('недостаточно Stars', result['reason'])
        self.assertEqual(result['gifts'], [])
        self.assertFalse(result['available'])

    async def test_catalog_outside_budget_does_not_invent_insufficient_balance(self):
        self.store.update_settings({'gift_max_price': 10})
        result = await self.inspect()
        self.assertIn('бюджете', result['reason'])
        self.assertIsNone(result['balance'])
        self.rt.telegram.stars_balance.assert_not_awaited()

    def order(self, stars):
        self.store.execute("INSERT INTO gift_orders(contact_id,gift_id,stars,month,created,status) VALUES(?,?,?,?,?,'uncertain')",
                           (10, '15', stars, self.rt.gifts.month(), time.time()))

    async def test_uncertain_purchase_keeps_budget_reserved(self):
        self.order(100)
        result = await self.inspect()
        self.assertEqual(result['remaining_budget'], 0)
        self.assertIn('исчерпан', result['reason'])
        self.rt.telegram.gift_catalog.assert_not_awaited()

    async def test_cooldown_reports_when_it_ends_without_querying_telegram(self):
        self.order(15)
        result = await self.inspect()
        self.assertIn('пауза между подарками', result['reason'])
        self.assertIn(result['next_allowed'], result['reason'])
        self.assertEqual(result['remaining_budget'], 85)
        self.rt.telegram.gift_catalog.assert_not_awaited()

    async def test_paused_character_reports_pause(self):
        self.store.update_settings({'enabled': False})
        self.assertEqual((await self.inspect())['reason'], 'Дарья на паузе')
        self.rt.telegram.gift_catalog.assert_not_awaited()

    async def test_requested_only_mode_never_runs_spontaneous_purchase_checks(self):
        self.assertFalse(self.store.settings()['gift_autonomous_enabled'])
        self.rt.model.completion = AsyncMock()
        await self.rt.gifts.tick(self.rt.model)
        self.rt.model.completion.assert_not_awaited()
        self.rt.telegram.gift_catalog.assert_not_awaited()
        self.rt.telegram.send_gift.assert_not_awaited()

    async def test_requested_only_mode_blocks_automatic_send_before_catalog(self):
        with self.assertRaisesRegex(ValueError, 'Самостоятельные подарки выключены'):
            await self.rt.gifts.send(10, '15', 'Для тебя', automatic=True)
        self.rt.telegram.gift_catalog.assert_not_awaited()
        self.rt.telegram.send_gift.assert_not_awaited()

    async def test_current_semantic_request_can_send_even_with_autonomy_off(self):
        self.store.message(10, 'user', 'Дашуль, порадовала бы меня мишкой в профиле', 100, sender_id=10)
        self.rt.model.gift_requested = AsyncMock(return_value=True)
        result = await self.rt.action(10, 100, 'send_gift', {'gift_id': '15', 'message': 'Для тебя'})
        self.assertTrue(result['sent'])
        self.rt.model.gift_requested.assert_awaited_once()
        texts, _ = self.rt.model.gift_requested.call_args.args
        self.assertEqual(texts, ['Дашуль, порадовала бы меня мишкой в профиле'])
        self.rt.telegram.send_gift.assert_awaited_once()

    async def test_no_current_request_never_purchases(self):
        self.store.message(10, 'user', 'Мне нравятся такие подарки', 100, sender_id=10)
        self.rt.model.gift_requested = AsyncMock(return_value=False)
        with self.assertRaisesRegex(ValueError, 'просьба о покупке не подтверждена'):
            await self.rt.action(10, 100, 'send_gift', {'gift_id': '15', 'message': 'Для тебя'})
        self.rt.telegram.send_gift.assert_not_awaited()
        self.rt.telegram.gift_catalog.assert_not_awaited()

    async def test_old_forwarded_or_superseded_requests_cannot_buy(self):
        self.rt.model.gift_requested = AsyncMock(return_value=True)
        for kind in ['old', 'forwarded', 'superseded']:
            with self.subTest(kind=kind):
                self.store.execute('DELETE FROM messages')
                self.store.message(10, 'user', 'Подари мишку', 100, sender_id=10,
                                   now=time.time() - 301 if kind == 'old' else None,
                                   forwarded=kind == 'forwarded')
                if kind == 'superseded':
                    self.store.message(10, 'user', 'Передумал, не дари', 101, sender_id=10)
                with self.assertRaises(ValueError):
                    await self.rt.action(10, 100, 'send_gift', {'gift_id': '15', 'message': 'Для тебя'})
        self.rt.model.gift_requested.assert_not_awaited()
        self.rt.telegram.send_gift.assert_not_awaited()

    async def test_request_withdrawn_during_classification_never_purchases(self):
        self.store.message(10, 'user', 'Подари мишку', 100, sender_id=10)
        async def classify(*args):
            self.store.message(10, 'user', 'Не дари, я передумал', 101, sender_id=10)
            return True
        self.rt.model.gift_requested = AsyncMock(side_effect=classify)
        with self.assertRaisesRegex(ValueError, 'новое сообщение'):
            await self.rt.action(10, 100, 'send_gift', {'gift_id': '15', 'message': 'Для тебя'})
        self.rt.telegram.send_gift.assert_not_awaited()
        self.assertEqual(self.store.rows('SELECT * FROM gift_orders'), [])

    async def test_enabling_requested_gifts_does_not_replay_recent_previous_requests(self):
        self.store.update_settings({'gifts_enabled': False})
        self.store.message(10, 'user', 'Подари мишку', 100, sender_id=10)
        self.store.update_settings({'gifts_enabled': True, 'gift_autonomous_enabled': False})
        self.rt.model.gift_requested = AsyncMock(return_value=True)
        with self.assertRaisesRegex(ValueError, 'нужна новая просьба'):
            await self.rt.action(10, 100, 'send_gift', {'gift_id': '15', 'message': 'Для тебя'})
        self.rt.model.gift_requested.assert_not_awaited()
        self.rt.telegram.send_gift.assert_not_awaited()

    async def test_batch_spanning_enable_time_excludes_earlier_gift_request(self):
        self.store.update_settings({'gifts_enabled': False})
        start = self.store.message(10, 'user', 'Подари мишку', 100, sender_id=10)
        self.store.update_settings({'gifts_enabled': True, 'gift_autonomous_enabled': False})
        self.store.message(10, 'user', 'Как прошёл день?', 101, sender_id=10)
        state = {'first_message_id': start}
        self.rt.model.gift_requested = AsyncMock(return_value=False)
        handled, _ = await self.rt.requested_gift_reply(10, 101, state)
        self.assertFalse(handled)
        self.assertEqual(self.rt.model.gift_requested.call_args.args[0], ['Как прошёл день?'])
        self.rt.telegram.send_gift.assert_not_awaited()

    async def test_autonomy_disabled_while_invoice_is_prepared_stops_payment(self):
        self.store.update_settings({'gift_autonomous_enabled': True})
        async def prepare_invoice(*args, before_payment, **kwargs):
            self.store.update_settings({'gift_autonomous_enabled': False})
            before_payment()
            self.fail('Payment must not proceed')
        self.rt.telegram.send_gift.side_effect = prepare_invoice
        with self.assertRaisesRegex(ValueError, 'Самостоятельные подарки выключены'):
            await self.rt.gifts.send(10, '15', 'Для тебя', automatic=True)
        self.assertEqual(self.rt.gifts.spent(), 0)
        self.assertEqual(self.store.rows('SELECT status FROM gift_orders'), [{'status': 'failed'}])

    async def test_invalid_semantic_request_response_never_authorizes_purchase(self):
        self.rt.model.completion = AsyncMock()
        for content in ['{"requested":"true"}', '{"requested":true,"extra":1}', 'не знаю']:
            self.rt.model.completion.return_value = {'content': content}
            with self.assertRaises(ValueError):
                await self.rt.model.gift_requested(['подари мишку'], [])
        self.rt.telegram.send_gift.assert_not_awaited()

    def reply_fixture(self, text):
        self.store.update_settings({'natural_enabled': False, 'typing_enabled': False, 'min_interval': 0,
                                    'quiet_start': 0, 'quiet_end': 0})
        self.rt.telegram.ready = lambda: True
        self.rt.telegram.send = AsyncMock(return_value=500)
        self.rt.model.key = lambda: 'test'
        self.rt.model.run = AsyncMock(return_value='Старый ошибочный ответ: только реакция, подарки недоступны')
        self.store.message(10, 'user', text, 100, sender_id=10)

    async def test_reply_routes_gift_before_character_can_substitute_reaction(self):
        self.reply_fixture('Дашуль, подари мне мишку в профиле')
        self.rt.model.gift_requested = AsyncMock(return_value=True)
        self.rt.model.completion = AsyncMock(return_value={'content': '{"gift_id":"15","message":"Для тебя"}'})
        await self.rt.reply(10, telegram_id=100, prepared=True)
        self.rt.model.run.assert_not_awaited()
        self.rt.telegram.send_gift.assert_awaited_once()
        self.rt.telegram.send.assert_awaited_once()
        self.assertIn('настоящий Telegram-подарок', self.rt.telegram.send.call_args.args[1])
        self.assertEqual(self.store.rows('SELECT status FROM gift_orders'), [{'status': 'sent'}])

    async def test_unavailable_requested_gift_returns_actual_reason_without_reaction(self):
        self.reply_fixture('Подари сердечко')
        self.rt.model.gift_requested = AsyncMock(return_value=True)
        self.rt.model.completion = AsyncMock(return_value={'content': '{"gift_id":null}'})
        await self.rt.reply(10, telegram_id=100, prepared=True)
        self.rt.model.run.assert_not_awaited()
        self.rt.telegram.send_gift.assert_not_awaited()
        self.assertIn('Подарок не отправлен', self.rt.telegram.send.call_args.args[1])
        self.assertEqual(self.store.rows('SELECT * FROM gift_orders'), [])

    async def test_ordinary_conversation_uses_character_and_never_buys(self):
        self.reply_fixture('Как прошёл день?')
        self.rt.model.gift_requested = AsyncMock(return_value=False)
        await self.rt.reply(10, telegram_id=100, prepared=True)
        self.rt.model.run.assert_awaited_once()
        self.rt.telegram.send_gift.assert_not_awaited()
        self.rt.telegram.gift_catalog.assert_not_awaited()

    async def test_invalid_classifier_defers_reply_instead_of_inventing_gift_refusal(self):
        self.reply_fixture('Скинь мне подарок пожалуйста')
        self.rt.model.gift_requested = AsyncMock(side_effect=ValueError('Неполный ответ проверки'))
        await self.rt.reply(10, telegram_id=100, prepared=True)
        self.rt.model.run.assert_not_awaited()
        self.rt.telegram.send.assert_not_awaited()
        self.rt.telegram.send_gift.assert_not_awaited()
        self.assertEqual(self.store.rows('SELECT status FROM pending_replies'), [{'status': 'pending'}])

    async def test_gift_request_after_context_reset_uses_new_request_despite_old_refusal(self):
        self.store.execute('INSERT INTO dialog_contexts(contact_id,sender_id,generation,started) VALUES(10,0,1,?)', (time.time(),))
        self.store.message(10, 'user', 'Подари сердечко', 90, sender_id=10, now=time.time()-1800)
        self.store.message(10, 'assistant', 'Подарки тебе закрыты. Вернёмся к прежней теме.', 91, now=time.time()-1799)
        self.reply_fixture('Скинь мне подарок пожалуйста')
        self.rt.model.gift_requested = AsyncMock(return_value=True)
        self.rt.model.completion = AsyncMock(return_value={'content': '{"gift_id":"15","message":"Для тебя"}'})
        await self.rt.reply(10, telegram_id=100, prepared=True)
        self.assertEqual(self.rt.model.gift_requested.call_args.args[0], ['Скинь мне подарок пожалуйста'])
        self.rt.model.run.assert_not_awaited()
        self.rt.telegram.send_gift.assert_awaited_once()
        self.assertEqual(self.store.rows('SELECT status FROM gift_orders'), [{'status': 'sent'}])

    async def test_selection_failure_before_purchase_can_be_retried(self):
        self.reply_fixture('Подари мишку')
        self.rt.model.gift_requested = AsyncMock(return_value=True)
        self.rt.model.completion = AsyncMock(side_effect=ModelUnavailable('503'))
        state = {'committed': False}
        with self.assertRaises(ModelUnavailable):
            await self.rt.requested_gift_reply(10, 100, state)
        self.assertFalse(state['committed'])
        self.assertEqual(self.store.rows('SELECT * FROM gift_orders'), [])
        self.rt.telegram.send_gift.assert_not_awaited()

    async def test_context_keeps_old_topic_but_marks_both_new_messages(self):
        self.store.message(10, 'user', 'OLD_DISTRESS', 1, now=1700000000)
        self.store.message(10, 'assistant', 'OLD_GIFT_REFUSAL', 2, now=1700000060)
        start = self.store.message(10, 'user', 'Подари сердечко', 3, now=1700002400)
        self.store.message(10, 'user', 'Я про подарок в Telegram', 4, now=1700002401)
        messages = self.rt.model.context(10, current_from=start)
        timeline = json.loads(messages[1]['content'].split(': ', 1)[1])
        self.assertEqual([row['new'] for row in timeline], [False, False, True, True])
        self.assertEqual(timeline[0]['time_utc'], '2023-11-14T22:13:20Z')
        boundary = next(i for i, row in enumerate(messages) if row['content'].startswith('Далее новые'))
        self.assertEqual(messages[boundary + 1]['content'], 'Подари сердечко')
        self.assertIn('OLD_DISTRESS', str(messages[:boundary]))
        self.assertIn('OLD_GIFT_REFUSAL', str(messages[:boundary]))
        self.assertEqual(len(self.store.history(10)), 4)

    def call(self, name, args=None):
        return {'id': name, 'type': 'function',
                'function': {'name': name, 'arguments': json.dumps(args or {})}}

    async def test_reaction_and_skip_cannot_hide_gift_check_regardless_of_tool_order(self):
        self.store.message(10, 'user', 'Подари подарок', 100)
        self.store.update_settings({'gifts_enabled': False})
        self.rt.telegram.react = AsyncMock()
        for names in [('react', 'skip_message', 'list_gifts'), ('list_gifts', 'react', 'skip_message')]:
            with self.subTest(names=names):
                responses = [{'tool_calls': [self.call(name, {'emoji': '❤️'} if name == 'react' else {}) for name in names]},
                             {'content': 'Отправка подарков выключена общим переключателем.'}]
                self.rt.model.completion = AsyncMock(side_effect=responses)
                answer = await self.rt.model.run(10, lambda name, args: self.rt.action(10, 100, name, args))
                self.assertIn('общим переключателем', answer)
                tool_results = [json.loads(row['content']) for row in self.rt.model.completion.call_args.args[0]
                                if row['role'] == 'tool' and row['tool_call_id'] == 'list_gifts']
                self.assertIn('общим переключателем', tool_results[0]['reason'])
        self.rt.telegram.send_gift.assert_not_awaited()

    async def test_later_reaction_cannot_hide_failed_gift_send(self):
        self.store.message(10, 'user', 'Подари подарок', 100)
        self.store.update_settings({'gifts_enabled': False})
        self.rt.telegram.react = AsyncMock()
        self.rt.model.completion = AsyncMock(side_effect=[
            {'tool_calls': [self.call('send_gift', {'gift_id': '15', 'message': 'Для тебя'})]},
            {'tool_calls': [self.call('react', {'emoji': '❤️'}), self.call('skip_message')]},
            {'content': 'Отправка подарков выключена общим переключателем.'}])
        answer = await self.rt.model.run(10, lambda name, args: self.rt.action(10, 100, name, args))
        self.assertIn('общим переключателем', answer)
        self.rt.telegram.send_gift.assert_not_awaited()
