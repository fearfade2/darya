import json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

from telethon import functions, types

from app.ai import TOOLS
from app.dialogs import current_dialog
from app.telegram_account import parse_account_command
from telegram_fixtures import TelegramFixture, document, message, OWNER, SELF, GROUP


class Results(list):
    query_id = 12345
    cache_time = 60


class MediaRequestTests(TelegramFixture):
    def source(self, chat=OWNER, text='сохрани', reply=None, id=55):
        row_id=self.store.message(chat,'user',text,id,sender_id=chat,reply_to_telegram_id=reply)
        return self.store.rows('SELECT * FROM messages WHERE id=?',(row_id,))[0]

    def photo_results(self):
        selected=NS(result=NS(type='photo',id='cat',title='Кот на диване',description='Кот отдыхает'),
                    click=AsyncMock(return_value=message(out=True,sender=SELF)))
        other=NS(result=NS(type='article',id='link',title='Статья'))
        self.client.inline_query=AsyncMock(return_value=Results([selected,other]))
        return selected

    async def test_owner_sticker_request_selects_real_media_and_has_no_second_reply(self):
        self.source(text='Стикер кинь')
        self.media.cache(document())
        plan=self.rt.control.parse(['Стикер кинь'])
        self.assertEqual(plan,[{'name':'telegram_action','args':{'action':'send_sticker'}}])
        answer=await self.rt.control.execute_plan(OWNER,55,plan)
        self.assertEqual(answer,'')
        self.assertEqual(self.client.send_file.call_args.args[1].id,123)
        self.rt.telegram.send.assert_not_awaited()

    async def test_high_level_delivery_is_deduplicated_before_another_search(self):
        self.source(text='Кинь стикер кота')
        self.media.stickers=AsyncMock(return_value={'items':[{'asset_id':'sticker:123','label':'Кот'}]})
        self.media.cache(document())
        self.rt.model.choose_telegram_media=AsyncMock(return_value='sticker:123')
        args={'action':'send_sticker','query':'кот'}
        for _ in range(2):
            await self.rt.control.action(OWNER,55,'telegram_action',args,trusted=True)
        self.media.stickers.assert_awaited_once()
        self.client.send_file.assert_awaited_once()

    async def test_uncertain_delivery_is_not_retried_with_a_new_selection(self):
        self.source(text='Кинь стикер')
        self.media.cache(document())
        self.client.send_file.side_effect=ConnectionError('lost acknowledgment')
        for _ in range(2):
            with self.assertRaises(ValueError):
                await self.rt.control.action(OWNER,55,'telegram_action',{'action':'send_sticker'},trusted=True)
        self.client.send_file.assert_awaited_once()

    async def test_owner_gif_without_id_uses_inline_result(self):
        self.source(text='Гифку кинь')
        selected=NS(result=NS(type='gif',id='gif',title='Happy',document=None),
                    click=AsyncMock(return_value=message(doc=document(127,'gif'),out=True,sender=SELF)))
        self.client.inline_query=AsyncMock(return_value=Results([selected]))
        self.client.return_value=NS(gifs=[])
        result=await self.rt.control.action(OWNER,55,'telegram_action',{'action':'send_gif'},trusted=True)
        self.assertTrue(result['sent'])
        selected.click.assert_awaited_once()

    async def test_inline_mp4_gifs_preserve_search_order_before_collection_fallback(self):
        self.media.cache(document(127,'gif'))
        results=Results([NS(result=NS(type='mpeg4_gif',id=str(i),title='GIF',document=None)) for i in [1,2]])
        self.client.inline_query=AsyncMock(return_value=results)
        self.client.return_value=NS(gifs=[])
        found=await self.media.gifs('кот')
        self.assertEqual([r['asset_id'] for r in found['items']],
                         ['gif-inline:12345:1','gif-inline:12345:2','gif:127'])

    async def test_save_reply_uses_actual_document_from_current_chat(self):
        self.source(reply=40)
        self.client.get_messages.return_value=message(id=40,chat=OWNER,doc=document(150,'gif'),sender=OWNER)
        result=await self.rt.control.action(OWNER,55,'telegram_action',{'action':'save_media'},trusted=True)
        self.assertEqual(result['asset_id'],'gif:150')
        self.assertIsInstance(self.client.call_args.args[0],functions.messages.SaveGifRequest)
        self.assertEqual(self.client.call_args.args[0].id.id,150)

    async def test_save_latest_received_media_uses_image_kind_and_current_generation(self):
        self.store.message(OWNER,'user','Посмотри стикер',40,sender_id=OWNER,image_kind='sticker')
        source=self.source()
        async def fetch(chat,ids):
            return message(id=ids,chat=chat,sender=OWNER,doc=document() if ids==40 else None)
        self.client.get_messages.side_effect=fetch
        result=await self.media.save_selected(OWNER,{},source)
        self.assertEqual(result['asset_id'],'sticker:123')

    async def test_save_never_falls_back_to_media_from_another_chat_or_cleared_context(self):
        self.store.message(20,'user','GIF',40,sender_id=20,image_kind='gif')
        self.store.message(OWNER,'user','GIF',41,sender_id=OWNER,image_kind='gif')
        self.store.execute('UPDATE messages SET context_generation=100 WHERE telegram_id=41')
        source=self.source()
        self.client.get_messages.return_value=message(id=55,chat=OWNER,sender=OWNER)
        with self.assertRaisesRegex(ValueError,'Ответь на нужный'):
            await self.media.save_selected(OWNER,{},source)
        self.client.get_messages.assert_awaited_once_with(OWNER,ids=55)
        self.client.assert_not_awaited()

    async def test_protected_or_wrong_reply_does_not_fall_back_to_another_asset(self):
        source=self.source(reply=40)
        for reply in [message(id=40,chat=20,doc=document()),message(id=40,chat=OWNER,doc=document())]:
            if reply.chat_id==OWNER:
                reply.noforwards=True
            self.client.get_messages.return_value=reply
            with self.assertRaises(ValueError):await self.media.save_selected(OWNER,{},source)
        self.client.assert_not_awaited()

    async def test_inline_only_gif_is_materialized_and_saved_without_posting(self):
        self.media.inline['gif-inline:1:x']={'result':NS(result=NS(content=NS(url='https://example.org/public.gif'))),
                                           'expires':10**12,'label':'GIF'}
        async def response(request):
            if isinstance(request,functions.messages.UploadMediaRequest):
                return types.MessageMediaDocument(document=document(180,'gif'))
            return True
        self.client.side_effect=response
        result=await self.media.save('gif-inline:1:x')
        self.assertEqual(result['asset_id'],'gif:180')
        upload=self.client.call_args_list[0].args[0]
        self.assertIsInstance(upload.peer,types.InputPeerSelf)
        self.assertIsInstance(self.client.call_args.args[0],functions.messages.SaveGifRequest)
        self.client.send_file.assert_not_awaited()
        self.client.send_message.assert_not_awaited()

    async def test_save_checks_telegram_confirmation(self):
        self.media.cache(document())
        self.client.return_value=False
        with self.assertRaisesRegex(ValueError,'не подтвердил'):await self.media.save('sticker:123')

    async def test_pic_search_is_read_only_and_keeps_only_photos(self):
        selected=self.photo_results()
        result=await self.media.execute(10,'find_photos',{'query':'кот отдыхает'})
        self.assertEqual(result['bot'],'@pic')
        self.assertEqual(len(result['items']),1)
        self.client.inline_query.assert_awaited_once_with('pic','кот отдыхает',entity=10)
        selected.click.assert_not_awaited()
        self.client.send_file.assert_not_awaited()

    async def test_high_level_photo_selects_and_clicks_the_real_pic_result(self):
        selected=self.photo_results()
        self.rt.model.choose_telegram_media=AsyncMock(return_value='photo-inline:12345:cat')
        result=await self.execute({'action':'send_photo','query':'кот отдыхает'},focus=10)
        self.assertTrue(result['sent'])
        selected.click.assert_awaited_once_with(10,reply_to=None)
        self.assertEqual(self.store.history(10)[-1]['kind'],'photo')

    async def test_pic_results_without_metadata_use_bot_ranking_without_another_model_request(self):
        selected=self.photo_results()
        selected.result.title=None
        selected.result.description=None
        self.rt.model.choose_telegram_media=AsyncMock(side_effect=ValueError('Provider unavailable'))
        result=await self.execute({'action':'send_photo','query':'кот спит на клавиатуре'},focus=10)
        self.assertTrue(result['sent'])
        self.rt.model.choose_telegram_media.assert_not_awaited()
        selected.click.assert_awaited_once_with(10,reply_to=None)

    async def test_media_selection_cannot_invent_an_id_or_send_twice(self):
        selected=self.photo_results()
        self.rt.model.choose_telegram_media=AsyncMock(return_value='photo-inline:999:made-up')
        with self.assertRaises(ValueError):await self.execute({'action':'send_photo','query':'кот'},focus=10)
        selected.click.assert_not_awaited()
        self.media.cache(document())
        self.rt.reply_states[10]={'media_sent':True}
        with self.assertRaises(ValueError):await self.media.send(10,'sticker:123','sticker')
        self.client.send_file.assert_not_awaited()

    async def test_photo_cannot_be_reused_in_another_chat_after_expiry_or_permission_revoke(self):
        selected=self.photo_results()
        result=await self.media.photos('кот',chat_id=10)
        asset=result['items'][0]['asset_id']
        with self.assertRaises(ValueError):await self.media.send(20,asset,'photo')
        self.store.execute('UPDATE contacts SET enabled=0 WHERE id=10')
        with self.assertRaises(ValueError):await self.media.send(10,asset,'photo')
        self.store.execute('UPDATE contacts SET enabled=1 WHERE id=10')
        self.media.inline[asset]['expires']=0
        with self.assertRaises(ValueError):await self.media.send(10,asset,'photo')
        selected.click.assert_not_awaited()

    async def test_model_media_reply_includes_photos_and_can_skip_extra_text(self):
        self.photo_results()
        asset=(await self.media.photos('кот',chat_id=10))['items'][0]['asset_id']
        def call(name,args,id):return {'id':id,'function':{'name':name,'arguments':json.dumps(args)}}
        self.rt.model.completion.side_effect=[{'content':None,'tool_calls':[call('send_photo',{'asset_id':asset},'a')]},
            {'content':None,'tool_calls':[call('skip_message',{},'b')]},{'content':'SKIP'}]
        result=await self.rt.model.run(10,lambda name,args:self.media.execute(10,name,args))
        self.assertEqual(result,'')
        self.assertIn('find_photos',{t['function']['name'] for t in TOOLS})

    async def test_my_profile_is_server_bound_to_requester_and_yours_to_darya(self):
        self.source(text='Какой у меня профиль')
        self.rt.account.profile=AsyncMock(return_value={'id':OWNER})
        for text in ['Какой у меня профиль','Покажи мой профиль','Нет мой','Нет, мой']:
            plan=self.rt.control.parse([text])
            await self.rt.control.action(OWNER,55,plan[0]['name'],plan[0]['args'],trusted=True)
            self.rt.account.profile.assert_awaited_with(str(OWNER),False)
        plan=parse_account_command('покажи свой профиль')
        await self.rt.control.action(OWNER,55,plan[0]['name'],plan[0]['args'],trusted=True)
        self.rt.account.profile.assert_awaited_with('me',False)

    async def test_media_parser_leaves_negation_and_contextual_photo_intent_to_planner(self):
        for text in ['Не кидай стикер','Фото котика не отправь','Ты умеешь кидать гифки?','Пришли фото по смыслу',
                     'Скинь стикер @olga_test','Гифку кинь ему']:
            self.assertIsNone(self.rt.control.parse([text]))
        self.assertEqual(self.rt.control.parse(['Фото котика кинь'])[0]['args'],{'action':'send_photo','query':'котика'})
        self.assertEqual(self.rt.control.parse(['Сохрани эту гифку себе'])[0]['args'],{'action':'save_media','kind':'gif'})

    async def test_save_reply_wins_over_a_model_supplied_search_query(self):
        source=self.source(reply=40)
        self.client.get_messages.return_value=message(id=40,chat=OWNER,doc=document(),sender=OWNER)
        self.media.select=AsyncMock()
        result=await self.media.save_selected(OWNER,{'query':'смешной','kind':'sticker'},source)
        self.assertEqual(result['asset_id'],'sticker:123')
        self.media.select.assert_not_awaited()
