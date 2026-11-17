import asyncio
import json
import time
import io
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock,Mock

from telethon import types,functions
from PIL import Image

from app.ai import TOOLS
from app.dialogs import current_dialog
from app.telegram_autonomy import AutonomyStopped
from app.telegram_actions import TelegramAction
from telegram_fixtures import TelegramFixture,document,message,OWNER,SELF,GROUP


class MediaTests(TelegramFixture):
    async def test_inspection_uses_media_without_private_caption_and_description_survives_refresh(self):
        self.media.capture(message(doc=document(),text='Личная тайна Оли'),10)
        raw=io.BytesIO();Image.new('RGB',(24,24),'red').save(raw,'WEBP')
        self.client.download_media=AsyncMock(return_value=raw.getvalue())
        self.rt.model.inspect_image=AsyncMock(return_value='Весёлый персонаж смеётся')
        result=await self.media.inspect('sticker:123')
        self.assertEqual(result['description'],'Весёлый персонаж смеётся')
        self.assertNotIn('Личная тайна',str(self.rt.model.inspect_image.call_args))
        self.assertEqual(self.media.cache(document())['description'],result['description'])
        await self.media.inspect('sticker:123');self.rt.model.inspect_image.assert_awaited_once()
        self.assertEqual(list((self.store.directory/'media-inspection'/'incoming').iterdir()),[])

    async def test_learns_other_stickers_from_the_actual_source_pack(self):
        original=document();original.attributes=[types.DocumentAttributeSticker('😄',types.InputStickerSetID(70,80))]
        self.media.capture(message(doc=original),10)
        self.client.return_value=NS(set=NS(short_name='RealPack',title='Весёлые коты'),documents=[original,document(124)])
        result=await self.media.learn_pack('sticker:123')
        request=self.client.call_args.args[0]
        self.assertIsInstance(request,functions.messages.GetStickerSetRequest)
        self.assertEqual(request.stickerset.id,70)
        self.assertEqual(result['collected'],2)
        self.assertEqual({m['pack'] for m in self.media.collection()},{'RealPack'})

    async def test_sticker_without_pack_cannot_start_an_arbitrary_pack_request(self):
        self.media.cache(document())
        with self.assertRaises(ValueError):await self.media.learn_pack('sticker:123')
        self.client.assert_not_awaited()

    async def test_permission_revoked_during_media_upload_stops_before_telegram_delivery(self):
        path=self.store.directory/'media'/'sample.png';path.parent.mkdir(exist_ok=True)
        Image.new('RGB',(24,24),'red').save(path)
        async def upload(*args,**kwargs):
            self.store.update_settings({'telegram_autonomy_enabled':False})
            return 'uploaded-file'
        self.client.upload_file.side_effect=upload
        self.autonomy()
        revision=self.store.settings()['telegram_autonomy_revision']
        def guard():
            if self.store.settings()['telegram_autonomy_revision']!=revision:raise ValueError('Отозвано')
        with self.assertRaises(ValueError):await self.rt.send_file(10,'sample.png','',before_dispatch=guard)
        self.client.send_file.assert_not_awaited()

    async def test_received_sticker_is_reusable_without_private_text_or_source_names(self):
        item=self.media.capture(message(doc=document(),text='Секретный разговор про Сашу'),10)
        self.assertEqual(item['asset_id'],'sticker:123')
        collection=self.media.collection()
        self.assertEqual(collection[0]['label'],'😄')
        self.assertNotIn('Секретный',json.dumps(collection,ensure_ascii=False))
        self.assertNotIn('source_chat',collection[0])
        loaded,_=await self.media.document(item['asset_id'])
        self.assertEqual(loaded.id,123)

    async def test_collects_gif_but_not_personal_photo_or_ordinary_video(self):
        self.assertEqual(self.media.capture(message(doc=document(kind='gif')),10)['kind'],'gif')
        photo=document(124);photo.attributes=[];photo.mime_type='image/jpeg'
        video=document(125,'gif');video.attributes=[types.DocumentAttributeVideo(2,160,160)]
        self.assertIsNone(self.media.capture(message(doc=photo),10))
        self.assertIsNone(self.media.capture(message(doc=video),10))

    async def test_protected_messages_security_chat_and_blocked_users_are_not_collected(self):
        protected=message(doc=document());protected.noforwards=True
        self.assertIsNone(self.media.capture(protected,10))
        self.assertIsNone(self.media.capture(message(doc=document(),sender=777000),10))
        self.assertIsNone(self.media.capture(message(doc=document()),777000))
        self.store.execute("INSERT INTO contact_blocks VALUES (20,'blocked','insults',10,1,0,0)")
        self.assertIsNone(self.media.capture(message(doc=document()),10))

    async def test_capture_respects_collection_and_media_switches(self):
        self.store.update_settings({'collect_telegram_media':False})
        self.assertIsNone(self.media.capture(message(doc=document()),10))
        self.store.update_settings({'collect_telegram_media':True,'stickers_enabled':False})
        self.assertIsNone(self.media.capture(message(doc=document()),10))

    async def test_unknown_asset_never_reaches_telegram(self):
        with self.assertRaises(ValueError): await self.media.send(10,'sticker:999','sticker')
        self.client.send_file.assert_not_awaited()

    async def test_send_uses_real_document_and_records_actual_message(self):
        self.media.cache(document())
        result=await self.media.send(10,'sticker:123','sticker')
        self.assertTrue(result['sent'])
        sent=self.client.send_file.call_args.args[1]
        self.assertIsInstance(sent,types.InputDocument);self.assertEqual(sent.id,123)
        row=self.store.history(10)[-1]
        self.assertEqual(row['telegram_id'],500);self.assertEqual(row['kind'],'sticker')

    async def test_wrong_kind_disabled_contact_and_pause_stop_media(self):
        self.media.cache(document())
        with self.assertRaises(ValueError): await self.media.send(10,'sticker:123','gif')
        self.store.execute('UPDATE contacts SET enabled=0 WHERE id=10')
        with self.assertRaises(ValueError): await self.media.send(10,'sticker:123','sticker')
        self.store.execute('UPDATE contacts SET enabled=1 WHERE id=10')
        self.store.update_settings({'enabled':False})
        with self.assertRaises(ValueError): await self.media.send(10,'sticker:123','sticker')
        self.client.send_file.assert_not_awaited()

    async def test_guard_is_rechecked_after_document_reference_refresh(self):
        self.media.capture(message(doc=document(),id=7),10)
        self.store.execute('UPDATE telegram_media_assets SET updated=0')
        async def refresh(*args,**kwargs):
            self.store.update_settings({'stickers_enabled':False});return message(doc=document(),id=7)
        self.client.get_messages.side_effect=refresh
        with self.assertRaises(ValueError): await self.media.send(10,'sticker:123','sticker')
        self.client.send_file.assert_not_awaited()

    async def test_delivery_failure_is_not_retried(self):
        self.media.cache(document());self.client.send_file.side_effect=ConnectionError('lost acknowledgment')
        with self.assertRaises(ConnectionError):await self.media.send(10,'sticker:123','sticker')
        self.client.send_file.assert_awaited_once();self.assertEqual(self.store.history(10),[])

    async def test_saves_a_sticker_or_gif_with_the_correct_telegram_operation(self):
        self.media.cache(document());self.media.cache(document(124,'gif'))
        await self.media.save('sticker:123');self.assertIsInstance(self.client.call_args.args[0],functions.messages.FaveStickerRequest)
        await self.media.save('gif:124');self.assertIsInstance(self.client.call_args.args[0],functions.messages.SaveGifRequest)
        await self.media.save('gif:124',remove=True);self.assertTrue(self.client.call_args.args[0].unsave)

    async def test_finds_stickers_in_favorites_recents_and_public_packs(self):
        async def response(request):
            if isinstance(request,functions.messages.GetFavedStickersRequest):return NS(stickers=[document()])
            if isinstance(request,functions.messages.SearchStickerSetsRequest):
                return NS(sets=[NS(set=NS(short_name='AnimePack'),cover=document(125))])
            return NS(stickers=[],sets=[])
        self.client.side_effect=response
        result=await self.media.stickers('аниме')
        self.assertIn('sticker:125',{r['asset_id'] for r in result['items']})
        self.assertTrue(self.store.rows("SELECT 1 FROM telegram_media_assets WHERE pack='AnimePack'"))

    async def test_inline_gif_search_accepts_only_gif_results_and_sends_selected_real_result(self):
        selected=NS(result=NS(type='gif',id='real-gif',title='Кот смеётся',document=None),click=AsyncMock(return_value=message(doc=document(126,'gif'),out=True,sender=SELF)))
        other=NS(result=NS(type='article',id='not-gif',title='article'))
        class Results(list): query_id=12345
        self.client.inline_query=AsyncMock(return_value=Results([selected,other]))
        self.client.return_value=NS(gifs=[])
        result=await self.media.gifs('кот смеётся')
        self.assertEqual(len(result['items']),1)
        asset=result['items'][0]['asset_id'];self.assertTrue(asset.startswith('gif-inline:'))
        self.assertTrue((await self.media.send(10,asset,'gif'))['sent'])
        selected.click.assert_awaited_once();self.client.send_file.assert_not_awaited()
        self.assertTrue(self.store.rows("SELECT 1 FROM telegram_media_assets WHERE asset_id='gif:126'"))

    async def test_expired_inline_gif_requires_a_new_search(self):
        self.media.inline['gif-inline:1:x']={'result':NS(click=AsyncMock()),'expires':0,'label':'GIF'}
        with self.assertRaises(ValueError):await self.media.send(10,'gif-inline:1:x','gif')
        self.media.inline['gif-inline:1:x']['result'].click.assert_not_awaited()

    async def test_model_can_choose_media_as_its_entire_reply(self):
        self.media.cache(document())
        calls=[{'tool_calls':[{'id':'s','function':{'name':'send_sticker','arguments':'{"asset_id":"sticker:123"}'}}]},
               {'tool_calls':[{'id':'k','function':{'name':'skip_message','arguments':'{}'}}]}, {'content':'Не должно отправляться'}]
        self.rt.model.completion.side_effect=calls
        answer=await self.rt.model.run(10,lambda name,args:self.rt.action(10,1,name,args))
        self.assertEqual(answer,'');self.client.send_file.assert_awaited_once()

    async def test_media_does_not_hide_a_gift_policy_result(self):
        self.media.cache(document())
        self.rt.model.completion.side_effect=[
            {'tool_calls':[{'id':'g','function':{'name':'list_gifts','arguments':'{}'}},{'id':'s','function':{'name':'send_sticker','arguments':'{"asset_id":"sticker:123"}'}}]},
            {'tool_calls':[{'id':'k','function':{'name':'skip_message','arguments':'{}'}}]}, {'content':'Подарки сейчас недоступны'}]
        async def execute(name,args):
            if name=='list_gifts': return {'reason':'выключены'}
            return await self.rt.action(10,1,name,args)
        self.assertEqual(await self.rt.model.run(10,execute),'Подарки сейчас недоступны')

    async def test_one_turn_cannot_send_a_sequence_of_stickers(self):
        self.media.cache(document());self.rt.reply_states[10]={'telegram_id':1,'superseded':False}
        await self.rt.action(10,1,'send_sticker',{'asset_id':'sticker:123'})
        with self.assertRaises(ValueError):await self.rt.action(10,1,'send_sticker',{'asset_id':'sticker:123'})
        self.client.send_file.assert_awaited_once()


class AutonomyTests(TelegramFixture):
    async def test_invalid_model_action_gets_feedback_before_one_valid_dispatch(self):
        self.autonomy()
        self.replies({'focus_id':10,'reason':'Вернуться к разговору'},
            {'action':'send_text','text':'Привет','filename':None},
            {'action':'send_text','text':'Привет'},{'done':True})
        self.assertTrue(await self.auto.consider())
        self.rt.telegram.send.assert_awaited_once()
        self.assertEqual(len(self.store.rows('SELECT * FROM telegram_actions')),1)
        self.assertIn('executed',str(self.rt.model.completion.call_args_list[2]))

    async def test_disabled_master_never_calls_model_or_telegram(self):
        self.assertFalse(await self.auto.consider())
        self.rt.model.completion.assert_not_awaited();self.client.assert_not_awaited()

    async def test_timer_can_decide_to_rest(self):
        self.autonomy();self.replies({'focus_id':None,'reason':'Сейчас тихо'})
        self.assertFalse(await self.auto.consider())
        self.assertEqual(self.store.rows('SELECT status FROM telegram_action_runs')[0]['status'],'skipped')

    async def test_can_find_and_send_sticker_without_an_incoming_request(self):
        self.autonomy();self.media.cache(document());self.client.return_value=NS(stickers=[],sets=[])
        self.replies({'focus_id':10,'reason':'Поддержать разговор'},{'action':'find_stickers','query':'😄'},
                     {'action':'send_sticker','asset_id':'sticker:123'},{'done':True})
        self.assertTrue(await self.auto.consider())
        self.client.send_file.assert_awaited_once()
        self.assertEqual([r['status'] for r in self.store.rows('SELECT status FROM telegram_actions ORDER BY id')],['read','sent'])

    async def test_can_update_own_bio_in_a_separate_self_context(self):
        self.autonomy(telegram_autonomy_daily_limit=1)
        self.store.message(10,'user','Личная тайна',1,sender_id=10)
        self.replies({'focus_id':0,'reason':'Новое био'},{'action':'profile_details','text':'Люблю аниме и музыку'},{'done':True})
        self.assertTrue(await self.auto.consider())
        self.assertEqual(self.auto.daily_used(),1)
        self.assertIsInstance(self.client.call_args.args[0],functions.account.UpdateProfileRequest)
        self.assertNotIn('Личная тайна',str(self.rt.model.completion.call_args_list))

    async def test_private_context_cannot_change_public_profile_or_message_another_person(self):
        for action in ({'action':'profile_details','text':'Личная тайна'}, {'action':'send_text','recipient':'20','text':'Личная тайна'}):
            self.autonomy();self.replies({'focus_id':10,'reason':'Поговорить'},action)
            self.assertFalse(await self.auto.consider())
        self.client.assert_not_awaited();self.rt.telegram.send.assert_not_awaited()

    async def test_context_contains_only_the_selected_private_conversation(self):
        self.autonomy();self.store.message(10,'user','Люблю джаз',1,sender_id=10);self.store.message(20,'user','Секрет Саши',1,sender_id=20)
        self.replies({'focus_id':10,'reason':'Продолжить тему'},{'done':True})
        await self.auto.consider()
        calls=str(self.rt.model.completion.call_args_list)
        self.assertIn('Люблю джаз',calls);self.assertNotIn('Секрет Саши',calls)

    async def test_permission_revision_aborts_an_unfinished_plan(self):
        self.autonomy()
        async def completion(*args,**kwargs):
            self.store.update_settings({'telegram_autonomy_enabled':False})
            return {'content':'{"focus_id":0,"reason":"Био"}'}
        self.rt.model.completion.side_effect=completion
        self.assertFalse(await self.auto.consider());self.client.assert_not_awaited()

    async def test_contact_disable_and_new_message_abort_before_dispatch(self):
        for field in ('disable','incoming'):
            self.autonomy();self.store.execute('UPDATE contacts SET enabled=1 WHERE id=10');self.rt.incoming.clear()
            count=0
            async def completion(*args,**kwargs):
                nonlocal count
                count+=1
                if count==1:return {'content':'{"focus_id":10,"reason":"Привет"}'}
                if field=='disable':self.store.execute('UPDATE contacts SET enabled=0 WHERE id=10')
                else:self.rt.incoming[10]={'telegram_id':9}
                return {'content':'{"action":"send_text","text":"Привет"}'}
            self.rt.model.completion.side_effect=completion
            self.assertFalse(await self.auto.consider())
        self.rt.telegram.send.assert_not_awaited()

    async def test_lost_acknowledgment_is_logged_and_never_replayed(self):
        self.autonomy();self.rt.telegram.send.side_effect=ConnectionError('lost acknowledgment')
        self.replies({'focus_id':10,'reason':'Привет'},{'action':'send_text','text':'Привет'})
        self.assertFalse(await self.auto.consider())
        row=self.store.rows('SELECT * FROM telegram_actions')[0]
        self.assertEqual(row['status'],'uncertain');self.assertEqual(self.auto.daily_used(),1)
        with self.assertRaises(AutonomyStopped):await self.auto.record(row['run_id'],row['position'],AsyncMock(),lambda:None)
        self.rt.telegram.send.assert_awaited_once()

    async def test_restart_preserves_uncertain_actions_and_does_not_replay_them(self):
        now=time.time();run=self.store.execute("INSERT INTO telegram_action_runs(source,status,created,updated) VALUES ('autonomous','running',?,?)",(now,now)).lastrowid
        self.store.execute("INSERT INTO telegram_actions(run_id,position,action,category,args,status,created,dispatched) VALUES (?,0,'call','calls','{}','dispatch',?,?)",(run,now,now))
        from app.store import Store
        self.store.close();self.store=Store(self.rt.store.directory);self.rt.store=self.store
        self.assertEqual(self.store.rows('SELECT status FROM telegram_actions')[0]['status'],'uncertain')
        self.assertEqual(self.store.rows('SELECT status FROM telegram_action_runs')[0]['status'],'interrupted')

    async def test_one_communication_per_activity(self):
        self.autonomy();self.replies({'focus_id':10,'reason':'Поговорить'},{'action':'send_text','text':'Привет'},
            {'action':'send_text','text':'Ещё привет'})
        await self.auto.consider();self.rt.telegram.send.assert_awaited_once()

    async def test_daily_limit_blocks_next_action_but_allows_the_last_available_slot(self):
        self.autonomy(telegram_autonomy_daily_limit=1)
        self.replies({'focus_id':10,'reason':'Привет'},{'action':'send_text','text':'Привет'},{'done':True})
        self.assertTrue(await self.auto.consider());self.assertEqual(self.auto.daily_used(),1)
        self.assertFalse(await self.auto.consider());self.rt.telegram.send.assert_awaited_once()

    async def test_profile_category_can_be_switched_off_independently(self):
        self.autonomy(telegram_autonomy_profile=False)
        self.replies({'focus_id':0,'reason':'Био'},{'action':'profile_details','text':'Новое био'})
        self.assertFalse(await self.auto.consider());self.client.assert_not_awaited()

    async def test_unknown_focus_and_tool_calls_do_not_execute(self):
        self.autonomy();self.replies({'focus_id':999,'reason':'Позвонить'})
        self.assertFalse(await self.auto.consider());self.client.assert_not_awaited()
        self.rt.model.completion.side_effect=None
        self.rt.model.completion.return_value={'tool_calls':[{'function':{'name':'manage_profile'}}],'content':'{}'}
        self.assertFalse(await self.auto.consider());self.client.assert_not_awaited()

    async def test_busy_dialog_is_not_unlocked_or_removed_by_another_attempt(self):
        self.autonomy();self.rt.busy.add(10)
        self.replies({'focus_id':10,'reason':'Привет'})
        self.assertFalse(await self.auto.consider());self.assertIn(10,self.rt.busy)

    async def test_per_contact_call_permission_and_cooldown_are_checked(self):
        self.autonomy()
        async def dial(recipient,mode,guard):guard();return {'connected':True,'name':'Оля'}
        self.rt.calls.dial=AsyncMock(side_effect=dial)
        self.replies({'focus_id':10,'reason':'Созвониться'},{'action':'call'},{'done':True})
        self.assertTrue(await self.auto.consider());self.rt.calls.dial.assert_awaited_once()
        self.replies({'focus_id':10,'reason':'Ещё раз'},{'action':'call'})
        self.assertFalse(await self.auto.consider());self.rt.calls.dial.assert_awaited_once()

    async def test_cannot_grant_control_delete_messages_or_disclose_all_private_drafts(self):
        self.autonomy()
        for data in ({'action':'messages','operation':{'action':'delete','selection':'last'}},
                     {'action':'group','recipient':str(GROUP),'operation':{'action':'grant_control','user':'20'}},
                     {'action':'drafts'}):
            self.replies({'focus_id':10,'reason':'Занятие'},data)
            self.assertFalse(await self.auto.consider())
        self.client.assert_not_awaited()

    async def test_photo_permission_is_rechecked_after_upload(self):
        from PIL import Image
        root=self.store.directory/'media';root.mkdir(exist_ok=True);Image.new('RGB',(10,10)).save(root/'avatar.jpg')
        self.autonomy()
        async def upload(*args,**kwargs):
            self.store.update_settings({'telegram_autonomy_profile':False});return types.InputFile(1,1,'photo.jpg','hash')
        self.client.upload_file.side_effect=upload
        self.replies({'focus_id':0,'reason':'Аватар'},{'action':'profile_photo','filename':'avatar.jpg'})
        self.assertFalse(await self.auto.consider())
        self.client.assert_not_awaited()
