from datetime import datetime, timezone
import time
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.owner_planning import catalog, decode_plan, plan_owner, validate_actions


class OwnerPlanningTests(unittest.IsolatedAsyncioTestCase):
    def test_archive_request_uses_a_real_bounded_publication_action(self):
        self.assertEqual(validate_actions([{'name':'manage_publications','args':{'action':'backfill','feed_id':2,'days':7}}],self.view)[0]['args']['days'],7)
        with self.assertRaises(ValueError):validate_actions([{'name':'manage_publications','args':{'action':'backfill','feed_id':2,'days':300}}],self.view)
        self.assertEqual(validate_actions([{'name':'manage_publications','args':{'action':'backfill','feed_id':1,'days':7,'query':'JB'}}],self.view)[0]['args']['query'],'JB')
        with self.assertRaises(ValueError):validate_actions([{'name':'manage_publications','args':{'action':'backfill','feed_id':1,'query':'x'*101}}],self.view)

    async def test_regular_task_and_settings_share_one_plan(self):
        plan,_=await self.plan('показывай печатание сразу и напомни вечером проверить новости',
            {'actions':[{'name':'configure_darya','args':{'settings':{'typing_start_delay':0}}},
                        {'name':'schedule_message','args':{'text':'Проверить новости','when':datetime.fromtimestamp(time.time()+3600,timezone.utc).isoformat()}}]})
        self.assertEqual([a['name'] for a in plan],['configure_darya','schedule_message'])

    def test_group_context_routes_settings_to_current_group_and_accepts_legacy_actions(self):
        view=self.view|{'context':{'kind':'group','chat_id':-100123,'topic_id':38}}
        plan=decode_plan({'content':json.dumps({'actions':[{'name':'configure_publications','args':{'settings':{'daily_limit':500}}},
            {'action':'topic_publications','enabled':False}]})},view)
        self.assertEqual(plan[0]['args']['destination'],'-100123')
        self.assertEqual(plan[1]['args'],{'group':'-100123','operation':{'action':'topic_publications','enabled':False}})

    def test_internal_owner_wrapper_is_never_a_public_group_operation(self):
        view=self.view|{'context':{'kind':'group','chat_id':-100123}}
        with self.assertRaises(ValueError):decode_plan({'content':'{"actions":[{"action":"owner_command","tool":"bash","args":{}}]}'},view)

    def setUp(self):
        self.view={'settings':{},'photos':[],'actor_id':1,'is_owner':True}

    async def plan(self, request, *answers):
        model=SimpleNamespace(completion=AsyncMock(side_effect=[{'content':json.dumps(a,ensure_ascii=False)} if isinstance(a,dict) else {'content':a} for a in answers]))
        result=await plan_owner(model,[request],self.view)
        return result,model

    def test_all_catalog_actions_have_schemas_without_unresolved_references(self):
        from app.control import OWNER_NAMES
        from app.ai import TOOLS
        self.assertEqual(set(catalog()),OWNER_NAMES|{t['function']['name'] for t in TOOLS})
        self.assertNotIn('$ref',json.dumps(catalog()))

    async def test_new_publication_phrase_uses_real_partial_configuration(self):
        plan,model=await self.plan('давай потолок выкладки поднимем до пяти сотен в сутки',
            {'actions':[{'name':'configure_publications','args':{'settings':{'daily_limit':500}}}]})
        self.assertEqual(plan[0]['args']['settings'],{'daily_limit':500})
        payload=json.loads(model.completion.call_args.args[0][1]['content'])
        self.assertEqual({x['name'] for x in payload['catalog']},set(catalog()))
        self.assertFalse(model.completion.call_args.kwargs['with_tools'])

    async def test_malformed_output_and_wrong_arguments_are_repaired_before_execution(self):
        plan,model=await self.plan('подними лимит до 500','Не могу разобрать',
            {'actions':[{'name':'configure_publications','args':{'settings':{'daily_limit':'500'}}}]},
            {'actions':[{'name':'configure_publications','args':{'settings':{'daily_limit':500}}}]})
        self.assertEqual(plan[0]['args']['settings']['daily_limit'],500)
        self.assertEqual(model.completion.await_count,3)

    async def test_action_schema_can_be_requested_without_asking_user(self):
        plan,model=await self.plan('спрячь мой номер ото всех',{'needs':['manage_privacy']},
            {'actions':[{'name':'manage_privacy','args':{'action':'set','field':'phone_number','scope':'nobody'}}]})
        self.assertEqual(plan[0]['name'],'manage_privacy')
        self.assertEqual(model.completion.await_count,2)

    async def test_missing_object_returns_specific_question(self):
        plan,_=await self.plan('подними лимит',{'question':'Какой лимит изменить и до какого значения?'})
        self.assertFalse(plan)
        self.assertIn('лимит',plan.question)

    async def test_unsupported_function_does_not_create_fake_setting(self):
        plan,_=await self.plan('сделай мне банковский перевод',{'unsupported':'Банковские переводы пока недоступны.'})
        self.assertFalse(plan)
        self.assertTrue(plan.unsupported)

    def test_strict_bounds_types_unknown_fields_and_conditional_required_parameters(self):
        for action in [
            {'name':'configure_publications','args':{'settings':{'daily_limit':501}}},
            {'name':'configure_publications','args':{'settings':{'enabled':'false'}}},
            {'name':'configure_publications','args':{'settings':{'imaginary_limit':500}}},
            {'name':'configure_publications','args':{'settings':{'excluded_topic_ids':[-1]}}},
            {'name':'configure_publications','args':{'settings':{'excluded_topic_ids':[38,38]}}},
            {'name':'configure_darya','args':{'settings':{'temperature':float('nan')}}},
            {'name':'manage_publications','args':{'action':'publish'}},
            {'name':'telegram_action','args':{'action':'send_message'}},
        ]:
            with self.subTest(action=action),self.assertRaises(ValueError):validate_actions([action],self.view)

    def test_read_only_account_commands_are_valid_without_checked_method(self):
        for name,args in [('get_telegram_profile',{'recipient':'current'}),('telegram_dialogs',{'limit':10}),('telegram_history',{'recipient':'@user_test'})]:
            self.assertEqual(validate_actions([{'name':name,'args':args}],self.view)[0]['name'],name)

    def test_reply_cannot_resume_without_current_authenticated_pending_request(self):
        with self.assertRaises(ValueError):decode_plan({'content':'{"actions":[],"resume":true}'},self.view)
        with self.assertRaises(ValueError):decode_plan({'content':'{"actions":[],"question":"Куда?","unsupported":"Нельзя"}'},self.view)

    async def test_provider_native_tools_are_never_executed_or_retried(self):
        model=SimpleNamespace(completion=AsyncMock(return_value={'tool_calls':[{'function':{'name':'bash'}}]}))
        with self.assertRaises(ValueError):await plan_owner(model,['измени лимит'],self.view)
        model.completion.assert_awaited_once()

    def test_common_equivalent_json_formats(self):
        for text in ['```json\n{"actions":[{"name":"set_reply_speed","arguments":{"speed":"fast"}}]}\n```',
                     '[{"name":"set_reply_speed","args":{"speed":"fast"}}]',
                     'План: {"actions":[{"name":"set_reply_speed","args":{"speed":"fast"}}]}']:
            self.assertEqual(decode_plan({'content':text},self.view)[0]['args'],{'speed':'fast'})
