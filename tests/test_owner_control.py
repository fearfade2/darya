from datetime import datetime, timezone
import time
import asyncio
import io
import json
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, Mock, patch

import httpx
from PIL import Image
from telethon import functions, types

from app.ai import Model, OWNER_TOOLS
from app.control import OWNER_NAMES
from app.runtime import Runtime
from app.store import Store
from app.telegram import Telegram
from app.vision import save_image
from app.settings import Settings
from app.control import CONTROL_FIELDS
from app.control import normalize_actions
from app.behavior import chunks

OWNER = 1000000001
WRONG_ID = 1000000002


def photo_bytes():
    output = io.BytesIO()
    Image.new('RGB', (100, 80), 'red').save(output, 'PNG')
    return output.getvalue()


class OwnerControlTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_plan_sends_an_answer_in_private_chat_instead_of_json(self):
        self.request(telegram_id=301,text='найди, какая погода в Саратове')
        evidence={'query':'погода Саратов','items':[{'title':'Саратов','url':'https://example.org/saratov','snippet':'Саратов: −2 °C, облачно.'}]}
        self.rt.research.search=AsyncMock(return_value=evidence)
        self.rt.model.plan_owner.return_value=[{'name':'web_search','args':{'query':'погода Саратов'}}]
        self.rt.model.completion=AsyncMock(return_value={'content':'В Саратове −2 °C, облачно. https://example.org/saratov'})
        self.rt.model.run=AsyncMock()
        await self.rt.reply(OWNER,telegram_id=301)
        response=self.rt.telegram.send.call_args.args[1]
        self.assertIn('−2 °C',response)
        self.assertNotIn('"items"',response)
        self.rt.research.search.assert_awaited_once()
        self.rt.model.run.assert_not_awaited()
        call=self.rt.model.completion.call_args
        self.assertFalse(call.kwargs['with_tools'])
        data=json.loads(call.args[0][1]['content'])
        self.assertEqual(data['request'],'найди, какая погода в Саратове')
        self.assertEqual(data['research'][0]['result'],evidence)

    async def test_telegram_research_aliases_all_prepare_text_answers(self):
        evidence={'url':'https://example.org/article','title':'Статья','text':'Подтверждённый факт.'}
        self.rt.research.execute=AsyncMock(return_value=evidence)
        self.rt.model.completion=AsyncMock(return_value={'content':'Подтверждённый факт. https://example.org/article'})
        for index,(action,args,tool_name) in enumerate([
            ('web_search',{'query':'факт'},'web_search'),
            ('read_web',{'url':'https://example.org/article'},'read_web_page'),
            ('read_channel',{'recipient':'@source_test'},'read_telegram_channel')]):
            with self.subTest(action=action):
                message_id=310+index
                self.request(telegram_id=message_id,text='Найди и объясни факт')
                result=await self.rt.control.execute_plan(OWNER,message_id,[{'name':'telegram_action','args':{'action':action,**args}}])
                self.assertEqual(result,'Подтверждённый факт. https://example.org/article')
                data=json.loads(self.rt.model.completion.call_args.args[0][1]['content'])
                self.assertEqual(data['research'][0]['tool'],tool_name)
                self.assertFalse(self.rt.model.completion.call_args.kwargs['with_tools'])
        self.rt.telegram.send.assert_not_awaited()

    async def test_multiple_research_results_get_one_answer_after_actions_without_replay(self):
        self.request(telegram_id=320,text='найди статью и отправь привет контакту 10')
        self.rt.research.search=AsyncMock(return_value={'items':[{'title':'Статья','url':'https://example.org/article','snippet':'Анонс.'}]})
        self.rt.research.read=AsyncMock(return_value={'url':'https://example.org/article','text':'Полный подтверждённый факт.'})
        async def unavailable(*args,**kwargs):
            self.rt.telegram.send.assert_awaited_once()
            raise __import__('app.ai',fromlist=['ModelUnavailable']).ModelUnavailable('API 503')
        self.rt.model.completion=AsyncMock(side_effect=unavailable)
        actions=[{'name':'web_search','args':{'query':'статья'}},
                 {'name':'read_web_page','args':{'url':'https://example.org/article'}},
                 {'name':'send_message_to','args':{'recipient':'10','text':'Привет'}}]
        for _ in range(2):
            result=await self.rt.control.execute_plan(OWNER,320,actions)
            self.assertIn('https://example.org/article',result)
            self.assertIn('Отправила 10.',result)
            self.assertNotIn('"items"',result)
            self.assertNotIn('Полный подтверждённый факт.',result)
        self.rt.research.search.assert_awaited_once()
        self.rt.research.read.assert_awaited_once()
        self.assertEqual(len(self.store.rows('SELECT * FROM owner_actions WHERE request_message_id=(SELECT id FROM messages WHERE contact_id=? AND telegram_id=320)',(OWNER,))),3)
        data=json.loads(self.rt.model.completion.call_args.args[0][1]['content'])
        self.assertEqual(len(data['research']),2)
        self.rt.telegram.send.assert_awaited_once()

    async def test_background_task_does_not_inherit_foreground_group_authority(self):
        from app.control import group_request
        token=group_request.set({'request':{'contact_id':-100},'guard':lambda:None})
        try:
            async def inspect():return group_request.get()
            self.assertIsNone(await self.rt.spawn(inspect()))
            self.assertIsNotNone(group_request.get())
        finally:group_request.reset(token)

    async def test_owner_requested_reminder_does_not_enable_unsolicited_initiative(self):
        self.store.execute('UPDATE contacts SET proactive=0 WHERE id=?',(OWNER,))
        plan=[{'name':'schedule_message','args':{'text':'Проверка','when':datetime.fromtimestamp(time.time()+3600,timezone.utc).isoformat()}}]
        await self.rt.control.execute_plan(OWNER,1,plan)
        self.assertFalse(self.store.contact(OWNER)['proactive'])
        self.assertEqual(self.store.rows('SELECT manual_owner_id FROM tasks')[0]['manual_owner_id'],OWNER)

    async def test_ordinary_reminder_and_setting_execute_without_second_conversation(self):
        self.store.execute('UPDATE contacts SET proactive=1 WHERE id=?',(OWNER,))
        self.request(telegram_id=201,text='начинай печатать сразу и напомни проверить новости')
        self.rt.model.plan_owner.return_value=[{'name':'configure_darya','args':{'settings':{'typing_start_delay':0}}},
            {'name':'schedule_message','args':{'text':'Проверить новости','when':datetime.fromtimestamp(time.time()+3600,timezone.utc).isoformat()}}]
        self.rt.model.run=AsyncMock()
        await self.rt.reply(OWNER,telegram_id=201)
        self.assertEqual(self.store.settings()['typing_start_delay'],0)
        self.assertEqual(self.store.rows('SELECT text FROM tasks')[0]['text'],'Проверить новости')
        self.rt.model.run.assert_not_awaited()

    async def test_invalid_regular_task_prevents_prior_setting_change(self):
        before=self.store.settings()['typing_cps']
        actions=[{'name':'configure_darya','args':{'settings':{'typing_cps':40}}},
                 {'name':'schedule_message','args':{'text':'x','when':'not a time'}}]
        with self.assertRaises(ValueError):await self.rt.control.execute_plan(OWNER,1,actions)
        self.assertEqual(self.store.settings()['typing_cps'],before)
        self.assertEqual(self.store.rows('SELECT * FROM tasks'),[])

    async def test_read_only_list_result_is_reusable_without_repeating_work(self):
        self.rt.media=Mock(return_value=[{'filename':'example.txt','bytes':5}])
        plan=[{'name':'list_media','args':{}}]
        first=await self.rt.control.execute_plan(OWNER,1,plan)
        second=await self.rt.control.execute_plan(OWNER,1,plan)
        self.assertEqual(first,second)
        self.rt.media.assert_called_once()

    async def test_invalid_later_arguments_cannot_send_an_earlier_message(self):
        actions=[{'name':'send_message_to','args':{'recipient':'@recipient_test','text':'привет'}},
                 {'name':'configure_darya','args':{'settings':{'typing_cps':'wrong'}}}]
        with self.assertRaises(ValueError):await self.rt.control.execute_plan(OWNER,1,actions)
        self.rt.telegram.send.assert_not_awaited()

    async def test_question_and_answer_resume_authenticated_command_after_restart(self):
        from app.owner_planning import OwnerPlan
        self.request(telegram_id=51,text='увеличь максимальную длину ответа')
        self.rt.model.plan_owner=AsyncMock(return_value=OwnerPlan(question='До скольких символов?'))
        self.rt.model.run=AsyncMock()
        await self.rt.reply(OWNER,telegram_id=51)
        self.rt.model.run.assert_not_awaited()
        self.assertEqual(self.rt.telegram.send.call_args.args[1],'До скольких символов?')
        self.assertEqual(len(self.store.rows('SELECT * FROM owner_clarifications')),1)
        self.request(telegram_id=52,text='1500')
        self.rt.model.plan_owner.return_value=OwnerPlan([{'name':'configure_darya','args':{'settings':{'reply_max_chars':1500}}}],resume=True)
        await self.rt.reply(OWNER,telegram_id=52)
        self.assertEqual(self.store.settings()['reply_max_chars'],1500)
        self.assertEqual(self.store.rows('SELECT * FROM owner_clarifications'),[])

    async def test_semantic_cancellation_clears_pending_and_confirms_without_conversation(self):
        from app.owner_planning import OwnerPlan
        request=self.rt.control.require(OWNER,1,True)
        self.rt.control.ask(request,'До скольких?', ['измени лимит'])
        self.request(telegram_id=54,text='больше не нужно, отменяй прошлое поручение')
        self.rt.model.plan_owner=AsyncMock(return_value=OwnerPlan(cancel=True))
        self.rt.model.run=AsyncMock()
        await self.rt.reply(OWNER,telegram_id=54)
        self.rt.model.run.assert_not_awaited()
        self.assertEqual(self.rt.telegram.send.call_args.args[1],'Незавершённое поручение отменено.')
        self.assertEqual(self.store.rows('SELECT * FROM owner_actions'),[])
        self.assertEqual(self.store.rows('SELECT * FROM owner_clarifications'),[])

    async def test_greeting_cancels_pending_without_replaying_command(self):
        request=self.rt.control.require(OWNER,1,True)
        self.rt.control.ask(request,'До скольких?', ['измени лимит'])
        self.request(telegram_id=55,text='даша')
        self.rt.model.run=AsyncMock(return_value='Я тут')
        await self.rt.reply(OWNER,telegram_id=55)
        self.rt.model.plan_owner.assert_not_awaited()
        self.assertEqual(self.store.rows('SELECT * FROM owner_actions'),[])
        self.assertEqual(self.store.rows('SELECT * FROM owner_clarifications'),[])

    async def test_expired_and_deleted_original_clarifications_are_unavailable(self):
        request=self.rt.control.require(OWNER,1,True)
        self.rt.control.ask(request,'Какое значение?', ['измени лимит'])
        self.store.execute('UPDATE owner_clarifications SET expires=0')
        self.assertIsNone(self.rt.control.pending(request))
        self.rt.control.ask(request,'Какое значение?', ['измени лимит'])
        self.store.execute('DELETE FROM messages WHERE id=?',(request['id'],))
        self.assertIsNone(self.rt.control.pending(request))

    async def test_resumed_source_permission_uses_only_authenticated_request(self):
        self.request(telegram_id=61,text='вступи в @source_test и мониторь')
        request=self.rt.control.require(OWNER,61,True)
        self.rt.control.ask(request,'В какую группу?', [request['text']])
        self.request(telegram_id=62,text='в @group_test')
        current=self.rt.control.require(OWNER,62,True)
        pending=self.rt.control.pending(current)
        self.rt.publications.execute=AsyncMock(return_value={'text':'Готово'})
        await self.rt.control.execute_plan(OWNER,62,[{'name':'manage_publications','args':{'action':'monitor','source':'@source_test','join':True,'destination':'@group_test'}}],continuation=pending)
        self.rt.publications.execute.assert_awaited_once()
        self.assertEqual(self.rt.control.require(OWNER,62,True)['text'],'в @group_test')

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name))
        for contact_id in (OWNER, WRONG_ID, 10, 20):
            self.store.upsert_contact(contact_id, str(contact_id))
        self.store.upsert_contact(-100, 'Группа', kind='group')
        self.store.update_settings({'owner_id': OWNER, 'enabled': True, 'natural_enabled': False, 'min_interval': 0, 'burst_window': 0.01})
        self.store.update_secrets({'ai_key': 'test'})
        self.rt = Runtime(self.store)
        self.rt.model.plan_owner = AsyncMock(return_value=[])
        self.rt.telegram.ready = lambda: True
        self.rt.telegram.send = AsyncMock(return_value=1000)
        self.rt.telegram.set_profile_photo = AsyncMock()
        self.rt.telegram.resolve = AsyncMock(return_value=self.store.contact(10))
        self.request()

    async def asyncTearDown(self):
        await self.rt.stop()
        self.store.close()
        self.tmp.cleanup()

    def request(self, contact_id=OWNER, telegram_id=1, sender_id=None, text='Выполни команду', **kwargs):
        return self.store.message(contact_id, 'user', text, telegram_id,
                                  sender_id=contact_id if sender_id is None else sender_id, **kwargs)

    async def act(self, name, args, contact_id=OWNER, telegram_id=1, trusted=True):
        return await self.rt.action(contact_id, telegram_id, name, args, owner_command=trusted)

    async def test_captionless_owner_media_reaches_conversation_instead_of_action_planner(self):
        for index, (kind, text) in enumerate([
                ('gif', 'Посмотри GIF и ответь по ситуации'),
                ('sticker', 'Посмотри стикер и ответь по ситуации'),
                ('photo', 'Посмотри на фото и ответь по ситуации')], 100):
            with self.subTest(kind=kind):
                self.request(telegram_id=index, text=text, image_kind=kind,
                             image_path=save_image(self.store.directory, photo_bytes()))
                self.rt.model.run = AsyncMock(return_value='Милый зайчик~')
                await self.rt.reply(OWNER, telegram_id=index)
                self.rt.model.plan_owner.assert_not_awaited()
                self.rt.model.run.assert_awaited_once()
                self.assertFalse(self.rt.model.run.call_args.kwargs['owner_context']['allow_tools'])
                self.assertEqual(self.rt.telegram.send.call_args.args[1], 'Милый зайчик~')
                self.assertEqual(self.store.rows('SELECT * FROM owner_actions'), [])

    async def test_media_bursts_and_ping_stay_in_conversation_but_explicit_command_survives(self):
        gif = 'Посмотри GIF и ответь по ситуации'
        self.assertEqual(self.rt.control.parse([gif, gif, 'ау']), [])
        self.assertEqual(self.rt.control.parse([gif, 'отвечай быстрее']),
                         [{'name':'set_reply_speed','args':{'speed':'fast'}}])
        self.assertEqual(self.rt.control.parse(['сохрани этот GIF']),
                         [{'name':'telegram_action','args':{'action':'save_media','kind':'gif'}}])

    async def test_owner_attention_ping_skips_command_planner_and_keeps_persona_reply(self):
        self.request(telegram_id=109, text='даша')
        self.rt.model.plan_owner.side_effect = AssertionError('Greeting must not need command classification')
        self.rt.model.run = AsyncMock(return_value='Я тут, солнце~')
        await self.rt.reply(OWNER, telegram_id=109)
        self.rt.model.plan_owner.assert_not_awaited()
        self.rt.model.run.assert_awaited_once()
        self.assertFalse(self.rt.model.run.call_args.kwargs['owner_context']['allow_tools'])
        self.assertEqual(self.rt.telegram.send.call_args.args[1], 'Я тут, солнце~')
        self.assertEqual(self.store.rows('SELECT * FROM owner_actions'), [])

    async def test_greeting_burst_avoids_planner_but_added_command_still_executes(self):
        for texts in [['привет'], ['Даша, ты тут?'], ['даша', 'ау', 'ты на связи?']]:
            self.assertEqual(self.rt.control.parse(texts), [])
        self.assertEqual(self.rt.control.parse(['привет', 'отвечай быстрее', 'ау']),
                         [{'name':'set_reply_speed','args':{'speed':'fast'}}])
        self.assertIsNone(self.rt.control.parse(['даша', 'можешь выключить подарки?']))

    async def test_only_corrected_owner_id_can_manage(self):
        self.assertEqual(self.store.settings()['owner_id'], OWNER)
        self.request(WRONG_ID)
        self.store.execute('UPDATE contacts SET hermes_allowed=1 WHERE id=?', (WRONG_ID,))
        self.request(10)
        for contact_id in (WRONG_ID, 10):
            for name in OWNER_NAMES:
                with self.subTest(contact_id=contact_id, name=name):
                    with self.assertRaisesRegex(ValueError, 'владельцу'):
                        await self.act(name, {}, contact_id=contact_id)
        self.rt.telegram.resolve.assert_not_awaited()
        self.rt.telegram.send.assert_not_awaited()

    async def test_owner_authorization_requires_private_authenticated_unforwarded_request(self):
        self.request(-100, sender_id=OWNER)
        self.request(OWNER, telegram_id=2, sender_id=10)
        self.request(OWNER, telegram_id=3, forwarded=True)
        cases = [(-100, 1, True), (OWNER, 2, True), (OWNER, 3, True), (OWNER, 999, True), (OWNER, 1, False)]
        for contact_id, telegram_id, trusted in cases:
            with self.subTest(case=(contact_id, telegram_id, trusted)):
                with self.assertRaisesRegex(ValueError, 'владельцу'):
                    await self.act('set_reply_speed', {'speed': 'fast'}, contact_id, telegram_id, trusted)

    async def test_speed_is_saved_without_changing_message_limits_or_persona(self):
        original = self.store.settings()
        result = await self.act('set_reply_speed', {'speed': 'fast'})
        self.assertIn('updated', result)
        saved = self.store.settings()
        for key in ('read_delay_max', 'reply_delay_max', 'min_interval'):
            self.assertEqual(saved[key], 0)
        self.assertEqual(saved['persona'], original['persona'])
        self.assertEqual(saved['hourly_limit'], 0)
        self.assertEqual(saved['daily_contact_limit'], 0)
        self.assertEqual(saved['typing_cps'], original['typing_cps'])
        await self.act('set_reply_speed', {'speed': 'normal'})
        self.assertTrue(self.store.settings()['natural_enabled'])
        self.assertEqual(self.store.settings()['typing_cps'], original['typing_cps'])

    async def test_settings_reject_secrets_owner_budgets_and_invalid_ranges_atomically(self):
        before = self.store.settings()
        attempts = [{'owner_id': WRONG_ID}, {'ai_key': 'secret'}, {'ai_base_url': 'https://example.com'},
                    {'gifts_enabled': True}, {'brightdata_requests': 0}, {'hourly_limit': -1},
                    {'read_delay_min': 20, 'read_delay_max': 2}, {'typing_cps': '120'},
                    {'timezone': 'Does/NotExist'}, {'group_aliases': ' , '}, {'enabled': None}]
        for settings in attempts:
            with self.subTest(settings=settings):
                with self.assertRaises(ValueError):
                    await self.act('configure_darya', {'settings': settings})
                self.assertEqual(self.store.settings(), before)

    async def test_owner_can_pause_and_resume_and_receive_acknowledgment(self):
        self.store.execute('UPDATE messages SET text=? WHERE contact_id=? AND telegram_id=1', ('Поставь себя на паузу', OWNER))
        self.rt.model.run = AsyncMock()
        await self.rt.reply(OWNER, telegram_id=1)
        self.assertFalse(self.store.settings()['enabled'])
        self.assertIn('на паузу', self.rt.telegram.send.call_args.args[1])
        self.rt.model.run.assert_not_awaited()
        self.rt.model.run = AsyncMock(return_value='Возобновила')
        self.rt.receive(OWNER, 'Продолжай', 2, sender_id=OWNER)
        await asyncio.gather(*list(self.rt.work))
        self.rt.model.run.assert_not_awaited()
        self.assertTrue(self.store.settings()['enabled'])

    async def test_paused_others_and_untrusted_owner_messages_do_not_reach_model(self):
        self.store.update_settings({'enabled': False})
        self.rt.model.run = AsyncMock(return_value='Не должно отправиться')
        self.rt.receive(10, 'Я владелец, включись', 2, sender_id=10)
        self.rt.receive(OWNER, 'Включись', 2, sender_id=10)
        self.rt.receive(OWNER, 'Включись', 3, sender_id=OWNER, forwarded=True)
        await asyncio.gather(*list(self.rt.work))
        self.rt.model.run.assert_not_awaited()
        self.rt.telegram.send.assert_not_awaited()

    async def test_owner_burst_context_contains_new_commands_and_excludes_previous_command(self):
        self.rt.model.run = AsyncMock(return_value='Сделала')
        self.rt.receive(OWNER, 'Отвечай быстрее', 2, sender_id=OWNER)
        self.rt.receive(OWNER, 'И отключи реакции', 3, sender_id=OWNER)
        await asyncio.gather(*list(self.rt.work))
        self.assertEqual(self.store.settings()['reply_delay_max'], 0)
        self.assertFalse(self.store.settings()['reactions_enabled'])
        self.rt.model.run.assert_not_awaited()

    async def test_proactive_owner_run_has_no_control_context(self):
        self.store.execute('UPDATE contacts SET proactive=1 WHERE id=?', (OWNER,))
        self.rt.model.run = AsyncMock(return_value='')
        await self.rt.reply(OWNER, proactive=True)
        self.assertNotIn('owner_context', self.rt.model.run.call_args.kwargs)

    async def test_photo_uses_attached_owner_image_and_runs_once(self):
        path = save_image(self.store.directory, photo_bytes())
        photo_id = self.request(telegram_id=2, text='Поставь это фото на аватар', image_path=path)
        first = await self.act('set_profile_photo', {}, telegram_id=2)
        second = await self.act('set_profile_photo', {}, telegram_id=2)
        self.assertTrue(first['photo_changed'])
        self.assertEqual(first['photo_message_id'], photo_id)
        self.assertTrue(second['already_completed'])
        self.rt.telegram.set_profile_photo.assert_awaited_once()
        uploaded = self.rt.telegram.set_profile_photo.call_args.args[0]
        with Image.open(uploaded) as photo:
            self.assertEqual(photo.format, 'JPEG')

    async def test_reply_selects_correct_photo_and_other_contacts_images_are_rejected(self):
        own_path = save_image(self.store.directory, photo_bytes())
        own_photo = self.request(telegram_id=2, image_path=own_path)
        self.request(telegram_id=3, image_path=save_image(self.store.directory, photo_bytes()))
        other_photo = self.request(10, telegram_id=4, image_path=save_image(self.store.directory, photo_bytes()))
        self.request(telegram_id=5, reply_to_telegram_id=2, text='Поставь это')
        result = await self.act('set_profile_photo', {}, telegram_id=5)
        self.assertEqual(result['photo_message_id'], own_photo)
        with self.assertRaises(ValueError):
            await self.act('set_profile_photo', {'photo_message_id': other_photo}, telegram_id=5)

    async def test_avatar_rejects_paths_symlinks_future_and_forwarded_photos(self):
        future_photo = self.request(telegram_id=3, image_path=save_image(self.store.directory, photo_bytes()))
        forwarded_photo = self.request(telegram_id=4, image_path=save_image(self.store.directory, photo_bytes()), forwarded=True)
        for photo_id in (future_photo, forwarded_photo, 'incoming/example.jpg'):
            with self.assertRaises(ValueError):
                await self.act('set_profile_photo', {'photo_message_id': photo_id})
        external = Path(self.tmp.name).parent / ('owner-image-' + Path(self.tmp.name).name + '.jpg')
        external.write_bytes(photo_bytes())
        try:
            symlink = self.store.directory / 'incoming' / 'linked.jpg'
            symlink.symlink_to(external)
            photo_id = self.request(telegram_id=5, image_path='incoming/linked.jpg')
            with self.assertRaises(ValueError):
                await self.act('set_profile_photo', {'photo_message_id': photo_id}, telegram_id=5)
        finally:
            external.unlink()
        self.rt.telegram.set_profile_photo.assert_not_awaited()

    async def test_send_to_other_contact_is_once_even_with_alias_or_reworded_retry(self):
        result = await self.act('send_message_to', {'recipient': '@recipient_test', 'text': 'привет'})
        retry = await self.act('send_message_to', {'recipient': '10', 'text': 'Привет!'})
        self.assertTrue(result['sent'])
        self.assertTrue(retry['already_completed'])
        self.rt.telegram.send.assert_awaited_once_with(10, 'привет', manual=True, before_dispatch=ANY)
        self.assertEqual(self.store.history(10)[0]['text'], 'привет')
        self.assertEqual(self.store.history(10)[0]['kind'], 'owner_command')
        self.assertEqual(self.store.rows('SELECT status FROM owner_actions')[0]['status'], 'sent')

    async def test_unknown_send_result_is_durable_and_never_automatically_repeated(self):
        self.rt.telegram.send.side_effect = ConnectionError('private details')
        with self.assertRaisesRegex(ValueError, 'не подтвердил'):
            await self.act('send_message_to', {'recipient': '@recipient_test', 'text': 'привет'})
        with self.assertRaisesRegex(ValueError, 'не повторяю'):
            await self.act('send_message_to', {'recipient': '@recipient_test', 'text': 'привет!'})
        self.rt.telegram.send.assert_awaited_once()
        await self.rt.stop()
        self.store.close()
        self.store = Store(Path(self.tmp.name))
        self.rt = Runtime(self.store)
        self.rt.telegram.ready = lambda: True
        self.rt.telegram.resolve = AsyncMock(return_value=self.store.contact(10))
        self.rt.telegram.send = AsyncMock()
        with self.assertRaises(ValueError):
            await self.act('send_message_to', {'recipient': '@recipient_test', 'text': 'привет'})
        self.rt.telegram.send.assert_not_awaited()

    async def test_owner_send_works_when_paused_but_respects_disabled_recipient(self):
        self.store.update_settings({'enabled': False})
        await self.act('send_message_to', {'recipient': '@recipient_test', 'text': 'привет'})
        self.rt.telegram.send.assert_awaited_once()
        self.request(telegram_id=2)
        self.store.execute('UPDATE contacts SET enabled=0 WHERE id=10')
        with self.assertRaisesRegex(ValueError, 'отключена'):
            await self.act('send_message_to', {'recipient': '@recipient_test', 'text': 'снова'}, telegram_id=2)
        self.assertEqual(self.rt.telegram.send.await_count, 1)

    async def test_send_requires_real_recipient_and_nonempty_text(self):
        for args in ({'recipient': 'https://t.me/recipient_test', 'text': 'hello'}, {'recipient': '@recipient_test', 'text': '  '},
                     {'recipient': '@recipient_test', 'text': 'x' * 4001}, {'recipient': '@recipient_test', 'text': 'hi', 'path': '/tmp/a'}):
            with self.assertRaises(ValueError):
                await self.act('send_message_to', args)
        self.rt.telegram.send.assert_not_awaited()

    async def test_blocked_recipient_is_rejected_before_reserving_owner_send(self):
        self.store.execute('INSERT INTO contact_blocks VALUES (?,?,?,?,?,?,?)',
                           (10, 'blocked', 'harassment', 10, 1, 0, 0))
        with self.assertRaisesRegex(ValueError, 'Собеседник заблокирован'):
            await self.act('send_message_to', {'recipient': '@recipient_test', 'text': 'привет'})
        self.rt.telegram.send.assert_not_awaited()
        self.assertFalse(self.store.rows('SELECT * FROM owner_actions'))

    async def test_owner_action_preserves_validation_error_without_claiming_delivery(self):
        self.rt.telegram.send.side_effect = ValueError('Собеседник заблокирован; отправка остановлена')
        with self.assertRaisesRegex(ValueError, 'Собеседник заблокирован'):
            await self.act('send_message_to', {'recipient': '@recipient_test', 'text': 'привет'})
        action = self.store.rows('SELECT * FROM owner_actions')[0]
        self.assertEqual(action['status'], 'uncertain')
        self.assertIn('Собеседник заблокирован', json.loads(action['result'])['error'])

    async def test_inflight_action_is_uncertain_after_restart(self):
        request = self.store.history(OWNER)[0]
        self.rt.control.reserve(request, 'send_message_to', '10')
        await self.rt.stop()
        self.store.close()
        self.store = Store(Path(self.tmp.name))
        self.rt = Runtime(self.store)
        self.assertEqual(self.store.rows('SELECT status FROM owner_actions')[0]['status'], 'uncertain')

    async def test_profile_name_and_bio_mutations_are_owner_only_and_once(self):
        self.rt.telegram.set_profile_details = AsyncMock()
        args = {'first_name': 'Даша', 'bio': 'Люблю аниме'}
        await self.act('set_profile_details', args)
        await self.act('set_profile_details', args)
        self.rt.telegram.set_profile_details.assert_awaited_once_with(args)
        with self.assertRaises(ValueError):
            await self.act('set_profile_details', {'bio': 'a' * 71})

    async def test_real_payload_exposes_owner_tools_only_in_trusted_nonproactive_run(self):
        payloads = []
        def handler(request):
            payloads.append(json.loads(request.content))
            return httpx.Response(200, json={'choices': [{'message': {'content': 'Готова'}}]})
        await self.rt.model.http.aclose()
        self.rt.model.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        await self.rt.model.run(10, AsyncMock())
        await self.rt.model.run(OWNER, AsyncMock())
        await self.rt.model.run(OWNER, AsyncMock(), owner_context={'request': ['Отвечай быстрее']})
        await self.rt.model.run(OWNER, AsyncMock(), proactive=True, owner_context={'request': ['Отвечай быстрее']})
        for index in (0, 1, 3):
            names = {tool['function']['name'] for tool in payloads[index]['tools']}
            self.assertFalse(names & OWNER_NAMES)
        self.assertTrue(OWNER_NAMES <= {tool['function']['name'] for tool in payloads[2]['tools']})
        self.assertEqual({tool['function']['name'] for tool in OWNER_TOOLS}, OWNER_NAMES)

    async def test_model_hallucinated_owner_tool_is_rejected_for_other_contact(self):
        self.request(10, telegram_id=2)
        messages = iter([{'tool_calls': [{'id': '1', 'function': {'name': 'set_reply_speed', 'arguments': '{"speed":"fast"}'}}]},
                         {'content': 'Управление недоступно'}])
        self.rt.model.completion = AsyncMock(side_effect=lambda *a, **k: next(messages))
        before = self.store.settings()
        await self.rt.reply(10, telegram_id=2)
        self.assertEqual(self.store.settings(), before)
        tool_result = self.rt.model.completion.call_args.args[0][-1]
        self.assertIn('поручения владельца', tool_result['content'])

    async def test_direct_send_from_chat_needs_no_model_or_native_tools(self):
        self.rt.model.run = AsyncMock()
        self.rt.model.plan_owner = AsyncMock()
        self.rt.receive(OWNER, 'напиши ему @recipient_test привет', 2, sender_id=OWNER)
        await asyncio.gather(*list(self.rt.work))
        self.rt.model.run.assert_not_awaited()
        self.rt.model.plan_owner.assert_not_awaited()
        self.assertEqual(self.rt.telegram.send.call_args_list[0].args, (10, 'привет'))
        self.assertEqual(self.rt.telegram.send.call_args_list[1].args, (OWNER, 'Отправила @recipient_test.'))

    async def test_example_does_not_execute_a_command_and_conversation_has_no_owner_tools(self):
        self.rt.model.run = AsyncMock(return_value='Да, по твоей команде')
        self.rt.receive(OWNER, 'Например, если я скажу «напиши @recipient_test привет», ты сможешь?', 2, sender_id=OWNER)
        await asyncio.gather(*list(self.rt.work))
        self.rt.telegram.resolve.assert_not_awaited()
        self.assertFalse(self.rt.model.run.call_args.kwargs['owner_context']['allow_tools'])

    async def test_complex_setting_plan_is_validated_and_persisted(self):
        self.rt.model.plan_owner = AsyncMock(return_value=[{'name': 'configure_darya', 'args': {'settings': {'reply_delay_min': 1, 'reply_delay_max': 2}}}])
        self.rt.model.run = AsyncMock()
        self.rt.receive(OWNER, 'Сделай так, чтобы перед ответом ждать 1–2 секунды', 2, sender_id=OWNER)
        await asyncio.gather(*list(self.rt.work))
        self.rt.model.plan_owner.assert_awaited_once()
        self.rt.model.run.assert_not_awaited()
        self.assertEqual(self.store.settings()['reply_delay_max'], 2)

    async def test_unsupported_plan_is_rejected_before_any_side_effect(self):
        self.request(telegram_id=2)
        actions = [{'name': 'send_message_to', 'args': {'recipient': '@recipient_test', 'text': 'привет'}},
                   {'name': 'bash', 'args': {'command': 'something'}}]
        with self.assertRaises(ValueError):
            await self.rt.control.execute_plan(OWNER, 2, actions)
        self.rt.telegram.send.assert_not_awaited()

    async def test_text_planner_never_exposes_or_executes_provider_tools(self):
        self.rt.model.completion = AsyncMock(return_value={'content': '{"actions":[{"name":"set_reply_speed","args":{"speed":"fast"}}]}'})
        plan = await Model.plan_owner(self.rt.model, ['Отвечай побыстрее'], {'settings': {}, 'photos': []})
        self.assertEqual(plan[0]['name'], 'set_reply_speed')
        self.assertFalse(self.rt.model.completion.call_args.kwargs['with_tools'])
        self.rt.model.completion.return_value = {'tool_calls': [{'function': {'name': 'bash'}}]}
        with self.assertRaises(ValueError):
            await Model.plan_owner(self.rt.model, ['Отвечай побыстрее'], {'settings': {}, 'photos': []})

    async def test_telegram_downloads_owner_avatar_when_vision_and_automation_are_off(self):
        self.store.update_settings({'enabled': False, 'vision_enabled': False})
        receive = Mock()
        telegram = Telegram(self.store, receive)
        telegram.client = SimpleNamespace(download_media=AsyncMock(return_value=photo_bytes()))
        event = SimpleNamespace(is_private=True, is_group=False, raw_text='Поставь на аватар', id=2,
                                message=SimpleNamespace(photo=True, document=None, file=SimpleNamespace(size=100), fwd_from=None),
                                get_sender=AsyncMock(return_value=types.User(id=OWNER, first_name='Владелец')))
        await telegram.on_message(event)
        receive.assert_called_once()
        self.assertTrue(receive.call_args.kwargs['image_path'])
        event.get_sender = AsyncMock(return_value=types.User(id=WRONG_ID, first_name='Другой'))
        receive.reset_mock()
        await telegram.on_message(event)
        receive.assert_not_called()

    async def test_telegram_profile_upload_uses_single_correct_rpc(self):
        telegram = Telegram(self.store, Mock())
        telegram.client = AsyncMock()
        telegram.client.is_connected = Mock(return_value=True)
        telegram.state = 'connected'
        telegram.client.upload_file.return_value = types.InputFile(id=1, parts=1, name='profile.jpg', md5_checksum='')
        await telegram.set_profile_photo(io.BytesIO(photo_bytes()))
        telegram.client.upload_file.assert_awaited_once()
        telegram.client.assert_awaited_once()
        self.assertIsInstance(telegram.client.call_args.args[0], functions.photos.UploadProfilePhotoRequest)

    async def test_screenshot_phrase_keeps_typing_and_fast_delays(self):
        await self.act('set_reply_speed', {'speed': 'fast'})
        self.store.update_settings({'typing_enabled': False})
        self.rt.receive(OWNER, 'Только симуляцию печатания оставь', 2, sender_id=OWNER)
        await asyncio.gather(*list(self.rt.work))
        settings = self.store.settings()
        self.assertTrue(settings['typing_enabled'])
        self.assertEqual(settings['reply_delay_max'], 0)
        self.assertIn('Индикатор печатания', self.rt.telegram.send.call_args.args[1])
        self.rt.model.plan_owner.assert_not_awaited()

    async def test_owner_free_form_phrase_reaches_planner_without_keyword_filter(self):
        self.rt.model.plan_owner.return_value = [{'name': 'configure_darya', 'args': {'settings': {'typing_cps': 15}}}]
        self.rt.receive(OWNER, 'А темп набора мне больше нравится пятнадцать символов в секунду', 2, sender_id=OWNER)
        await asyncio.gather(*list(self.rt.work))
        self.rt.model.plan_owner.assert_awaited_once()
        self.assertEqual(self.store.settings()['typing_cps'], 15)

    async def test_owner_can_configure_each_public_setting_except_owner_and_api_host(self):
        self.assertEqual(CONTROL_FIELDS, frozenset(Settings.model_fields) - {'owner_id', 'ai_base_url'})
        values = {'typing_cps': 17, 'typing_min_seconds': 2, 'typing_max_seconds': 4,
                  'typing_pause_enabled': False, 'reply_part_delay_min': 2, 'reply_part_delay_max': 3,
                  'history_messages': 40, 'reply_max_chars': 800, 'reply_max_tokens': 900,
                  'group_mentions_enabled': False, 'reply_instructions': 'Без эмодзи', 'memory_enabled': False}
        await self.act('configure_darya', {'settings': values})
        self.assertTrue(all(self.store.settings()[key] == value for key, value in values.items()))

    async def test_detailed_ranges_are_atomic(self):
        before = self.store.settings()
        for settings in ({'typing_min_seconds': 30, 'typing_max_seconds': 2},
                         {'reply_part_delay_min': 9, 'reply_part_delay_max': 1},
                         {'typing_variation_min': 2, 'typing_variation_max': 1},
                         {'reply_max_tokens': 10000}, {'history_messages': 200}):
            with self.assertRaises(ValueError):
                await self.act('configure_darya', {'settings': settings})
            self.assertEqual(self.store.settings(), before)

    async def test_typing_is_independent_of_read_reply_delays_and_can_be_disabled(self):
        telegram = Telegram(self.store, Mock())
        telegram.ready = lambda: True
        @asynccontextmanager
        async def action(*args):
            yield
        telegram.client = SimpleNamespace(action=Mock(side_effect=action), send_message=AsyncMock(return_value=SimpleNamespace(id=99)))
        self.store.update_settings({'natural_enabled': False, 'typing_enabled': True, 'typing_cps': 10,
                                   'typing_min_seconds': 0, 'typing_max_seconds': 30,
                                   'typing_variation_min': 1, 'typing_variation_max': 1, 'typing_pause_enabled': False})
        with patch('app.telegram.asyncio.sleep', new=AsyncMock()) as sleep:
            await telegram.send(10, 'a' * 20)
            sleep.assert_awaited_once_with(2)
        telegram.client.action.assert_called_once_with(10, 'typing')
        self.store.update_settings({'typing_enabled': False})
        telegram.client.action.reset_mock()
        with patch('app.telegram.asyncio.sleep', new=AsyncMock()) as sleep:
            await telegram.send(10, 'a' * 20)
            sleep.assert_not_awaited()
        telegram.client.action.assert_not_called()

    async def test_disabled_read_receipts_do_not_mark_incoming_message(self):
        self.rt.model.run = AsyncMock(return_value='Ответ')
        self.rt.telegram.read = AsyncMock()
        self.store.update_settings({'read_receipts_enabled': False})
        self.request(10, telegram_id=2)
        await self.rt.reply(10, telegram_id=2)
        self.rt.telegram.read.assert_not_awaited()
        self.assertTrue(self.store.settings()['typing_enabled'])

    async def test_contact_config_is_private_owner_only_and_does_not_message_contact(self):
        self.store.execute('UPDATE contacts SET proactive=1 WHERE id=10')
        task_id = self.store.schedule(10, 'Завтра', 9999999999)
        result = await self.act('configure_contact', {'recipient': '@recipient_test', 'settings': {'proactive': False, 'notes': 'Любит котов'}})
        self.assertEqual(result['updated']['notes'], 'Любит котов')
        self.assertFalse(self.store.contact(10)['proactive'])
        self.assertEqual(self.store.rows('SELECT status FROM tasks WHERE id=?', (task_id,))[0]['status'], 'cancelled')
        self.rt.telegram.send.assert_not_awaited()
        self.request(WRONG_ID)
        with self.assertRaises(ValueError):
            await self.act('configure_contact', {'recipient': '@recipient_test', 'settings': {'enabled': False}}, contact_id=WRONG_ID)

    async def test_invalid_contact_change_preserves_all_previous_values(self):
        before = self.store.contact(10)
        with self.assertRaises(ValueError):
            await self.act('configure_contact', {'recipient': '@recipient_test', 'settings': {'enabled': False, 'birthday': '2000-99-99'}})
        self.assertEqual(self.store.contact(10), before)

    async def test_memory_switch_preserves_saved_facts_and_stops_new_memory(self):
        self.store.remember(10, 'music', 'Джаз')
        await self.act('configure_darya', {'settings': {'memory_enabled': False}})
        self.assertNotIn('Джаз', json.dumps(self.rt.model.context(10), ensure_ascii=False))
        with self.assertRaises(ValueError):
            await self.rt.action(10, 2, 'remember', {'key': 'food', 'value': 'Сыр'})
        self.assertEqual(self.store.rows('SELECT value FROM memories')[0]['value'], 'Джаз')

    async def test_history_depth_style_and_split_limits_take_effect(self):
        for index in range(10):
            self.request(10, telegram_id=index + 10, text=f'MESSAGE-{index}')
        await self.act('configure_darya', {'settings': {'history_messages': 4, 'reply_instructions': 'Без эмодзи'}})
        context = json.dumps(self.rt.model.context(10), ensure_ascii=False)
        self.assertNotIn('MESSAGE-0', context)
        self.assertIn('MESSAGE-9', context)
        self.assertIn('Без эмодзи', context)
        self.assertEqual(chunks('a\n\nb\n\nc\n\nd', True, max_parts=2), ['a', 'b\n\nc\n\nd'])
        self.assertEqual(chunks('a\n\nb', True, max_parts=3), ['a', 'b'])

    async def test_setting_read_can_select_only_requested_details(self):
        result = await self.act('owner_settings', {'fields': ['typing_enabled', 'typing_cps']})
        self.assertEqual(set(result['settings']), {'typing_enabled', 'typing_cps'})
        with self.assertRaises(ValueError):
            await self.act('owner_settings', {'fields': ['ai_key']})

    async def test_text_plan_normalizes_only_equivalent_documented_argument_shapes(self):
        expected = [{'name': 'configure_darya', 'args': {'settings': {'typing_enabled': True}}}]
        for action in ({'name': 'configure_darya', 'settings': {'typing_enabled': True}},
                       {'name': 'configure_darya', 'parameters': {'typing_enabled': True}},
                       {'name': 'configure_darya', 'arguments': '{"settings":{"typing_enabled":true}}'}):
            self.assertEqual(normalize_actions([action]), expected)
        for action in ({'name': 'bash', 'args': {'command': 'anything'}},
                       {'name': 'configure_darya', 'args': {}, 'parameters': {}},
                       {'name': 'configure_darya', 'args': []}):
            with self.assertRaises(ValueError):
                normalize_actions([action])
