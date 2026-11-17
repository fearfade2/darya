import json
import time
from unittest.mock import AsyncMock, patch

from app.private_initiative import PrivateInitiative
from app.settings import validate_settings
from telegram_fixtures import TelegramFixture, SELF, document, message


class PrivateInitiativeTests(TelegramFixture):
    def enable(self, **values):
        self.autonomy(private_initiative_enabled=True, **values)
        self.store.message(10,'user','Завтра расскажу, как прошёл экзамен',1,now=time.time()-7200,sender_id=10)
        self.store.message(10,'assistant','Буду ждать! Удачи тебе',2,now=time.time()-7100)

    def due(self):
        self.rt.private_initiative.state(10)
        self.store.execute('UPDATE private_initiatives SET next_check=0 WHERE contact_id=10')

    def dispatch(self, status='sent', ago=3600):
        now=time.time()-ago
        run=self.store.execute("INSERT INTO telegram_action_runs(source,focus_id,generation,created,updated) VALUES ('private_initiative',10,0,?,?)",(now,now)).lastrowid
        return self.store.execute("INSERT INTO telegram_actions(run_id,position,action,category,args,status,created,dispatched) VALUES (?,0,'send_text','messages','{}',?,?,?)",(run,status,now,now)).lastrowid

    async def test_variable_interval_is_persistent_and_new_activity_reschedules(self):
        self.enable()
        with patch('app.private_initiative.random.uniform',return_value=37):
            state=self.rt.private_initiative.state(10)
            self.assertAlmostEqual(state['next_check']-time.time(),37*60,delta=1)
        self.assertEqual(PrivateInitiative(self.rt).state(10),state)
        self.store.message(10,'user','Я сдал!',3,sender_id=10)
        self.assertNotEqual(self.rt.private_initiative.state(10)['activity_id'],state['activity_id'])

    async def test_continues_specific_conversation_without_global_focus_selection(self):
        self.enable();self.due()
        self.store.message(20,'user','Чужая личная история',1,sender_id=20)
        self.replies({'action':'send_text','text':'Вспомнила твой экзамен. Как всё прошло?'},{'done':True})
        self.assertTrue(await self.auto.consider(focus=10,private_initiative=True))
        self.rt.telegram.send.assert_awaited_once()
        prompt=str(self.rt.model.completion.call_args_list)
        self.assertIn('экзамен',prompt);self.assertNotIn('Чужая личная история',prompt)
        self.assertIn('не спрашивай, почему человек молчит',prompt)
        self.assertEqual(self.auto.daily_used(),1)
        self.assertGreater(self.rt.private_initiative.state(10)['next_check'],time.time())

    async def test_can_return_with_a_sticker_only(self):
        self.enable();self.due();self.media.cache(document())
        self.replies({'action':'send_sticker','asset_id':'sticker:123'},{'done':True})
        self.assertTrue(await self.auto.consider(focus=10,private_initiative=True))
        self.client.send_file.assert_awaited_once();self.rt.telegram.send.assert_not_awaited()

    async def test_can_rest_and_reschedules_without_sending(self):
        self.enable();self.due();self.replies({'done':True})
        self.assertFalse(await self.auto.consider(focus=10,private_initiative=True))
        self.rt.telegram.send.assert_not_awaited()
        self.assertGreater(self.rt.private_initiative.state(10)['next_check'],time.time())

    async def test_stops_after_two_unanswered_and_human_reply_reopens(self):
        self.enable();self.due();self.dispatch(ago=5000);self.dispatch(ago=3600)
        self.assertFalse(self.rt.private_initiative.eligible(10))
        self.assertFalse(await self.auto.consider(focus=10,private_initiative=True))
        self.rt.model.completion.assert_not_awaited()
        self.store.message(10,'user','Сдал отлично!',3,sender_id=10);self.due()
        self.assertTrue(self.rt.private_initiative.eligible(10))
        self.assertEqual(self.rt.private_initiative.pacing(10)['unanswered'],0)

    async def test_forwarded_message_does_not_clear_unanswered(self):
        self.enable();self.dispatch();self.dispatch(ago=5000)
        self.store.message(10,'user','Чужая цитата',3,sender_id=10,forwarded=True);self.due()
        self.assertFalse(self.rt.private_initiative.eligible(10))

    async def test_uncertain_delivery_waits_for_new_human_activity(self):
        self.enable();self.due();self.dispatch('uncertain')
        self.assertFalse(self.rt.private_initiative.eligible(10))
        self.assertIn('не подтверждено',self.rt.private_initiative.pacing(10)['reason'])

    async def test_own_dispatch_does_not_consume_last_available_limit_slot(self):
        self.enable(private_initiative_daily_limit=1,private_initiative_max_unanswered=1);self.due()
        self.replies({'action':'send_text','text':'Как прошёл экзамен?'},{'done':True})
        self.assertTrue(await self.auto.consider(focus=10,private_initiative=True))
        self.rt.telegram.send.assert_awaited_once()
        self.assertFalse(self.rt.private_initiative.pacing(10)['allowed'])

    async def test_global_autonomy_cannot_bypass_private_pacing(self):
        self.enable();self.due();self.dispatch();self.dispatch(ago=5000)
        self.replies({'focus_id':10,'reason':'Поговорить'})
        self.assertFalse(await self.auto.consider())
        self.rt.telegram.send.assert_not_awaited()

    async def test_opt_out_is_preserved_and_self_never_targeted(self):
        self.enable();self.due()
        self.store.execute('UPDATE contacts SET proactive=0 WHERE id=10')
        self.store.upsert_contact(10,'Оля','olga_test')
        self.assertFalse(self.rt.private_initiative.eligible(10))
        self.assertFalse(self.rt.private_initiative.eligible(SELF))

    async def test_pending_reply_call_and_incoming_postpone_initiative(self):
        self.enable();self.due()
        self.rt.incoming[10]={'telegram_id':3}
        self.assertFalse(self.rt.private_initiative.eligible(10));self.rt.incoming.clear()
        self.store.execute("INSERT INTO pending_replies(contact_id,telegram_id,first_message_id,due) VALUES (10,3,3,0)")
        self.assertFalse(self.rt.private_initiative.eligible(10))

    async def test_permission_revocation_during_generation_stops_send(self):
        self.enable();self.due()
        async def model(*args,**kwargs):
            self.store.update_settings({'private_initiative_enabled':False})
            return {'content':'{"action":"send_text","text":"Как экзамен?"}'}
        self.rt.model.completion.side_effect=model
        self.assertFalse(await self.auto.consider(focus=10,private_initiative=True))
        self.rt.telegram.send.assert_not_awaited()

    async def test_fresh_incoming_during_generation_reschedules(self):
        self.enable();self.due()
        async def model(*args,**kwargs):
            self.store.message(10,'user','Уже вернулся',3,sender_id=10)
            return {'content':'{"action":"send_text","text":"Как экзамен?"}'}
        self.rt.model.completion.side_effect=model
        self.assertFalse(await self.auto.consider(focus=10,private_initiative=True))
        self.rt.telegram.send.assert_not_awaited()

    async def test_current_voice_mode_is_present_in_initiative_context(self):
        self.enable();self.due();self.store.set_reply_mode(10,10,'voice')
        self.replies({'done':True})
        await self.auto.consider(focus=10,private_initiative=True)
        self.assertIn('"reply_mode": "voice"',str(self.rt.model.completion.call_args))

    async def test_archived_dialog_does_not_restart_old_topics(self):
        self.enable();self.due()
        self.store.execute('INSERT INTO dialog_contexts VALUES (10,0,1,?)',(time.time(),))
        self.assertFalse(self.rt.private_initiative.eligible(10))

    async def test_tick_schedules_only_one_due_dialog(self):
        self.enable();self.due();self.replies({'done':True})
        self.rt.private_initiative.tick()
        self.assertIsNotNone(self.auto.worker)
        first=self.auto.worker;self.rt.private_initiative.tick()
        self.assertIs(self.auto.worker,first)
        await first

    async def test_invalid_interval_is_rejected(self):
        with self.assertRaises(ValueError):
            validate_settings(self.store.settings(),{'private_initiative_min_minutes':121,'private_initiative_max_minutes':120})

    async def test_large_action_results_remain_valid_json(self):
        result=json.loads(self.auto.result_json({'text':'д'*24000}))
        self.assertTrue(result['truncated'])
