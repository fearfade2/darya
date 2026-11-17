import json
import time
from unittest.mock import AsyncMock, patch

from telethon import types

from app.dialogs import current_dialog
from telegram_fixtures import TelegramFixture, document, message, GROUP, SELF


def sticker_reply(text='Ахах, кот победил!', asset_id='sticker:123'):
    return {'tool_calls': [{'id': 'reply', 'function': {'name': 'reply_with_sticker',
            'arguments': json.dumps({'text': text, 'asset_id': asset_id}, ensure_ascii=False)}}]}


class ConversationStickerTests(TelegramFixture):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        # Yield during the optional pause without waiting wall-clock seconds.
        self.pause_random=patch('app.telegram_media.random.uniform',return_value=0)
        self.pause_random.start()
        self.addCleanup(self.pause_random.stop)
        self.store.update_settings({'online_during_reply': False, 'memory_enabled': False,
                                    'gifts_enabled': False, 'group_enabled': True})
        self.cache(123, '😄')
        self.cache(124, '🥰')
        self.client.send_file.return_value = message(id=501, out=True, sender=SELF)

    def cache(self, id, emoji, description=''):
        doc = document(id)
        doc.attributes = [types.DocumentAttributeSticker(emoji, types.InputStickerSetEmpty())]
        self.media.cache(doc)
        self.store.execute('UPDATE telegram_media_assets SET description=? WHERE asset_id=?',
                           (description, 'sticker:'+str(id)))

    def seed(self, chat=10):
        self.store.message(chat, 'assistant', 'Предыдущий ответ', 101, now=time.time()-600)

    async def test_context_choice_and_text_then_sticker_need_only_one_completion(self):
        self.seed()
        order = []
        async def text(*args, **kwargs):
            order.append('text')
            return 500
        async def sticker(*args, **kwargs):
            order.append('sticker')
            return message(id=501, out=True, sender=SELF)
        self.rt.telegram.send.side_effect = text
        self.client.send_file.side_effect = sticker
        self.rt.model.completion.return_value = sticker_reply()
        await self.rt.reply(10, text='Кот украл мой ужин ахаха', telegram_id=41, prepared=True)
        self.assertEqual(order, ['text', 'sticker'])
        self.rt.model.completion.assert_awaited_once()
        self.client.assert_not_awaited()  # No Telegram search.
        self.assertEqual(self.store.history(10)[-2]['text'], 'Ахах, кот победил!')
        self.assertEqual(self.store.history(10)[-1]['kind'], 'sticker')
        prompt = self.rt.model.completion.call_args.args[0][-1]['content']
        self.assertIn('Кот', str(self.rt.model.completion.call_args.args[0]))
        self.assertIn('sticker:123', prompt)
        self.assertIn('sticker:124', prompt)
        self.assertNotIn('source_chat', prompt)

    async def test_sticker_pause_starts_only_after_text_ack_and_precedes_media(self):
        self.seed()
        order=[]
        async def text(*args,**kwargs):
            order.append('text_ack')
            return 500
        async def pause(seconds):
            if not seconds:return
            self.rt.telegram.send.assert_awaited_once()
            self.client.send_file.assert_not_awaited()
            self.assertGreaterEqual(seconds,2)
            self.assertLessEqual(seconds,5)
            order.append('pause')
        async def sticker(*args,**kwargs):
            order.append('sticker')
            return message(id=501,out=True,sender=SELF)
        self.rt.telegram.send.side_effect=text
        self.client.send_file.side_effect=sticker
        self.rt.model.completion.return_value=sticker_reply()
        with patch('app.telegram_media.random.uniform',return_value=3.5), \
             patch('app.telegram_media.asyncio.sleep',side_effect=pause):
            await self.rt.reply(10,text='Ахаха',telegram_id=41,prepared=True)
        self.assertEqual(order,['text_ack','pause','sticker'])
        self.rt.model.completion.assert_awaited_once()

    async def test_switching_off_during_pause_keeps_text_and_cancels_sticker(self):
        self.seed()
        self.rt.model.completion.return_value=sticker_reply()
        async def pause(seconds):
            if not seconds:return
            self.rt.telegram.send.assert_awaited_once()
            self.store.update_settings({'stickers_enabled':False})
        with patch('app.telegram_media.random.uniform',return_value=3.5), \
             patch('app.telegram_media.asyncio.sleep',side_effect=pause):
            await self.rt.reply(10,text='Ахаха',telegram_id=41,prepared=True)
        self.rt.telegram.send.assert_awaited_once()
        self.client.send_file.assert_not_awaited()
        self.assertEqual(self.store.history(10)[-1]['text'],'Ахах, кот победил!')

    async def test_superseding_reply_during_pause_does_not_send_or_retry_old_sticker(self):
        self.seed()
        self.rt.model.completion.return_value=sticker_reply()
        async def pause(seconds):
            if not seconds:return
            self.rt.reply_states[10]['superseded']=True
        with patch('app.telegram_media.random.uniform',return_value=3.5), \
             patch('app.telegram_media.asyncio.sleep',side_effect=pause):
            await self.rt.reply(10,text='Ахаха',telegram_id=41,prepared=True)
        self.rt.telegram.send.assert_awaited_once()
        self.client.send_file.assert_not_awaited()
        self.assertEqual(self.store.rows('SELECT * FROM pending_replies'),[])

    async def test_plain_answer_does_not_add_a_random_sticker(self):
        self.seed()
        self.rt.model.completion.return_value = {'content': 'Проверь адрес сервера.'}
        await self.rt.reply(10, text='Как проверить endpoint?', telegram_id=41, prepared=True)
        self.rt.telegram.send.assert_awaited_once()
        self.client.send_file.assert_not_awaited()
        self.rt.model.completion.assert_awaited_once()

    async def test_no_known_emotion_or_description_means_no_offer(self):
        self.store.execute('DELETE FROM telegram_media_assets')
        self.cache(125, 'Стикер')
        self.cache(126, '📡')
        self.seed()
        self.assertEqual(self.media.conversation_offer(10), [])
        self.rt.model.completion.return_value = sticker_reply(asset_id='sticker:126')
        await self.rt.reply(10, text='Привет', telegram_id=41, prepared=True)
        self.rt.telegram.send.assert_awaited_once()
        self.client.send_file.assert_not_awaited()

    async def test_first_contact_answer_and_disabled_stickers_are_text_only(self):
        self.assertEqual(self.media.conversation_offer(10), [])
        self.seed()
        self.store.update_settings({'stickers_enabled': False})
        self.rt.model.completion.return_value = sticker_reply()
        await self.rt.reply(10, text='Ахаха', telegram_id=41, prepared=True)
        self.rt.telegram.send.assert_awaited_once()
        self.client.send_file.assert_not_awaited()

    async def test_unoffered_id_keeps_the_text_without_search_or_send(self):
        self.seed()
        self.rt.model.completion.return_value = sticker_reply(asset_id='sticker:999')
        await self.rt.reply(10, text='Ахаха', telegram_id=41, prepared=True)
        self.rt.telegram.send.assert_awaited_once()
        self.rt.model.completion.assert_awaited_once()
        self.client.send_file.assert_not_awaited()
        self.client.assert_not_awaited()

    async def test_gap_requires_both_replies_and_elapsed_time(self):
        old = time.time()-600
        self.store.message(10, 'assistant', '[Стикер: 🥰]', 100, 'sticker', now=old)
        self.store.message(10, 'assistant', 'Ответ', 101, now=old+1)
        self.assertEqual(self.media.conversation_offer(10), [])
        self.store.message(10, 'assistant', 'Ответ', 102, now=old+2)
        self.assertTrue(self.media.conversation_offer(10))
        self.store.execute("UPDATE messages SET created=? WHERE kind='sticker'", (time.time()-44,))
        self.assertEqual(self.media.conversation_offer(10), [])
        self.store.execute("UPDATE messages SET created=? WHERE kind='sticker'", (time.time()-46,))
        self.assertTrue(self.media.conversation_offer(10))

    async def test_alternating_emotions_remain_available_without_repeating_the_latest(self):
        old=time.time()-600
        self.store.message(10,'assistant','[Стикер: 😄]',100,'sticker',now=old-60)
        self.store.message(10,'assistant','[Стикер: 🥰]',101,'sticker',now=old)
        self.store.message(10,'assistant','Ответ',102,now=old+1)
        self.store.message(10,'assistant','Ответ',103,now=old+2)
        offered={row['asset_id'] for row in self.media.conversation_offer(10)}
        self.assertIn('sticker:123',offered)
        self.assertNotIn('sticker:124',offered)

    async def test_recent_sticker_emotions_are_excluded_even_for_another_document(self):
        self.cache(125, '😄')
        old = time.time()-600
        self.store.message(10, 'assistant', '[Стикер: 😄]', 100, 'sticker', now=old)
        for i in range(4):
            self.store.message(10, 'assistant', 'Ответ', 101+i, now=old+i+1)
        offered = {row['asset_id'] for row in self.media.conversation_offer(10)}
        self.assertNotIn('sticker:123', offered)
        self.assertNotIn('sticker:125', offered)
        self.assertIn('sticker:124', offered)
        # Cadence and exclusions are derived from stored deliveries, survive a new helper.
        self.assertEqual(offered, {row['asset_id'] for row in type(self.media)(self.rt).conversation_offer(10)})

    async def test_incoming_sticker_does_not_count_as_one_sent_by_darya(self):
        self.seed()
        self.store.message(10, 'user', '[Стикер: 😄]', 42, 'sticker')
        self.assertIn('sticker:123', {row['asset_id'] for row in self.media.conversation_offer(10)})

    async def test_explicit_sticker_request_bypasses_conversation_cooldown(self):
        self.store.message(10, 'assistant', '[Стикер: 😄]', 100, 'sticker')
        self.assertEqual(self.media.conversation_offer(10), [])
        result = await self.rt.action(10, 41, 'send_sticker', {'asset_id': 'sticker:123'})
        self.assertTrue(result['sent'])
        self.client.send_file.assert_awaited_once()

    async def test_switching_off_while_sending_text_cancels_optional_sticker(self):
        self.seed()
        async def text(*args, **kwargs):
            self.store.update_settings({'stickers_enabled': False})
            return 500
        self.rt.telegram.send.side_effect = text
        self.rt.model.completion.return_value = sticker_reply()
        await self.rt.reply(10, text='Ахаха', telegram_id=41, prepared=True)
        self.rt.telegram.send.assert_awaited_once()
        self.client.send_file.assert_not_awaited()

    async def test_new_message_supersedes_optional_sticker_after_text(self):
        self.seed()
        async def text(*args, **kwargs):
            self.rt.reply_states[10]['superseded'] = True
            return 500
        self.rt.telegram.send.side_effect = text
        self.rt.model.completion.return_value = sticker_reply()
        await self.rt.reply(10, text='Ахаха', telegram_id=41, prepared=True)
        self.client.send_file.assert_not_awaited()

    async def test_missing_text_ack_never_sends_sticker(self):
        self.seed()
        self.rt.telegram.send.side_effect = ConnectionError('lost text acknowledgment')
        self.rt.model.completion.return_value = sticker_reply()
        await self.rt.reply(10, text='Ахаха', telegram_id=41, prepared=True)
        self.client.send_file.assert_not_awaited()

    async def test_missing_sticker_ack_preserves_delivered_text_without_retry(self):
        self.seed()
        self.client.send_file.side_effect = ConnectionError('lost sticker acknowledgment')
        self.rt.model.completion.return_value = sticker_reply()
        await self.rt.reply(10, text='Ахаха', telegram_id=41, prepared=True)
        self.client.send_file.assert_awaited_once()
        self.rt.telegram.send.assert_awaited_once()
        self.rt.model.completion.assert_awaited_once()
        self.assertEqual(self.store.history(10)[-1]['text'], 'Ахах, кот победил!')
        self.assertEqual(self.store.rows('SELECT * FROM pending_replies'), [])

    async def test_group_sticker_replies_to_current_author_in_current_topic(self):
        self.seed(GROUP)
        self.store.group_message(GROUP, 7, 'Тема', topic_id=7)
        self.store.message(GROUP, 'user', 'Кот украл ужин', 41, sender_id=20, reply_to_telegram_id=7)
        self.rt.model.completion.return_value = sticker_reply()
        await self.rt.reply(GROUP, telegram_id=41, prepared=True)
        self.rt.telegram.send.assert_awaited_once()
        self.assertEqual(self.rt.telegram.send.call_args.kwargs['reply_to'], 41)
        self.assertEqual(self.client.send_file.call_args.kwargs['reply_to'], 41)
        row = self.store.rows("SELECT * FROM messages WHERE contact_id=? AND kind='sticker'", (GROUP,))[0]
        self.assertEqual(row['reply_to_telegram_id'], 41)
        self.assertEqual(self.store.group_topic(GROUP, 501), 7)

    async def test_group_cooldown_is_shared_between_authors_and_topics(self):
        self.store.message(GROUP, 'assistant', '[Стикер: 😄]', 100, 'sticker')
        token = current_dialog.set(self.store.dialog_scope(GROUP, 20))
        try:
            self.assertEqual(self.media.conversation_offer(GROUP), [])
        finally:
            current_dialog.reset(token)
        self.seed(10)
        self.assertTrue(self.media.conversation_offer(10))

    async def test_group_switch_is_rechecked_after_text_delivery(self):
        self.seed(GROUP)
        self.store.message(GROUP, 'user', 'Кот украл ужин', 41, sender_id=20)
        async def text(*args, **kwargs):
            self.store.update_settings({'group_enabled': False})
            return 500
        self.rt.telegram.send.side_effect = text
        self.rt.model.completion.return_value = sticker_reply()
        await self.rt.reply(GROUP, telegram_id=41, prepared=True)
        self.rt.telegram.send.assert_awaited_once()
        self.client.send_file.assert_not_awaited()

    async def test_voice_reply_and_existing_media_cannot_queue_extra_sticker(self):
        self.seed()
        self.rt.reply_states[10] = {'media_sent': True}
        self.rt.model.completion.return_value = sticker_reply()
        answer = await self.rt.model.run(10, AsyncMock())
        self.assertEqual(answer, 'Ахах, кот победил!')
        self.assertNotIn('conversation_sticker', self.rt.reply_states[10])
        self.rt.reply_states[10] = {}
        await self.rt.model.run(10, AsyncMock(), voice_reply=True)
        self.assertNotIn('conversation_sticker', self.rt.reply_states[10])

    async def test_empty_text_cannot_finish_reply_or_queue_a_sticker(self):
        self.seed()
        self.rt.reply_states[10] = {}
        self.rt.model.completion.side_effect = [sticker_reply(text=' '), {'content': 'Осмысленный ответ'}]
        answer = await self.rt.model.run(10, AsyncMock(), require_text=True)
        self.assertEqual(answer, 'Осмысленный ответ')
        self.assertNotIn('conversation_sticker', self.rt.reply_states[10])

    async def test_gift_result_keeps_text_when_finished_with_a_sticker(self):
        self.seed()
        self.rt.reply_states[10] = {'parts_sent': 0}
        self.rt.model.completion.side_effect = [
            {'tool_calls': [{'id': 'gift', 'function': {'name': 'list_gifts', 'arguments': '{}'}}]},
            sticker_reply(text='Подарки выключены в настройках.')]
        execute = AsyncMock(return_value={'reason': 'выключены'})
        answer = await self.rt.model.run(10, execute)
        self.assertEqual(answer, 'Подарки выключены в настройках.')
        execute.assert_awaited_once_with('list_gifts', {})
        self.client.send_file.assert_not_awaited()
