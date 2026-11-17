import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.publication_routing import repository_links,verify


class PublicationRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_incomplete_json_is_repaired_before_a_route_can_pass(self):
        model=SimpleNamespace(completion=AsyncMock(side_effect=[{'content':'{"topic_id":'},
            {'content':json.dumps({'topic_id':2,'confidence':'high','grounded':True,'reason':'Разбор'})}]))
        result=await verify(model,self.routes,self.sources,'Текст')
        self.assertEqual(result.topic_id,2);self.assertEqual(model.completion.await_count,2)
        self.assertGreaterEqual(model.completion.call_args.kwargs['max_tokens'],3000)

    def setUp(self):
        self.routes=[{'topic_id':44,'name':'Github','description':'Софт и локальные модели'},
                     {'topic_id':2,'name':'Обучение','description':'Руководства и сравнения'}]
        self.sources=[{'id':33,'title':'LM Studio, Strata vs Ollama?','text':'Какой лучше?',
                       'links':['https://reddit.com/r/LocalLLM/comments/abc/comparison']}]

    def model(self,**changes):
        response={'topic_id':2,'confidence':'high','grounded':True,'reason':'Содержательный разбор'}|changes
        return SimpleNamespace(completion=AsyncMock(return_value={'content':json.dumps(response)}))

    def test_repository_evidence_excludes_discussion_sites_and_github_navigation(self):
        sources=[{'links':['https://reddit.com/r/test/abc','https://example.github.io/tool',
                          'https://github.com/topics/llm','https://github.com/search?q=llm',
                          'https://github.com/example/project','https://gitlab.com/example/project']}]
        self.assertEqual(repository_links(sources),['https://github.com/example/project','https://gitlab.com/example/project'])

    async def test_known_program_names_never_replace_repository_evidence(self):
        decision=await verify(self.model(topic_id=44),self.routes,self.sources,'Сравнивают программы')
        self.assertIsNone(decision.topic_id)

    async def test_real_repository_can_be_routed_to_github(self):
        sources=[{'id':1,'title':'Релиз проекта','text':'Инструмент для анализа кода',
                  'links':['https://github.com/example/project']}]
        self.assertEqual((await verify(self.model(topic_id=44),self.routes,sources,'Инструмент анализа кода')).topic_id,44)

    def test_source_itself_can_provide_the_real_repository_url(self):
        self.assertEqual(repository_links([{'url':'https://github.com/example/project','links':[]}]),
                         ['https://github.com/example/project'])

    async def test_unsubstantiated_conclusions_are_not_published(self):
        decision=await verify(self.model(grounded=False),self.routes,self.sources,'Участники пришли к единому выводу')
        self.assertIsNone(decision.topic_id)

    async def test_no_matching_topic_and_uncertain_fit_are_rejected(self):
        for changes in [{'topic_id':None,'confidence':'low'},{'confidence':'low'}]:
            with self.subTest(changes=changes):
                self.assertIsNone((await verify(self.model(**changes),self.routes,self.sources,'Текст')).topic_id)

    async def test_unknown_banned_or_malformed_topic_never_passes_validation(self):
        for changes in [{'topic_id':38},{'topic_id':True},{'confidence':'sure'},{'grounded':'true'}]:
            with self.subTest(changes=changes),self.assertRaises(ValueError):
                await verify(self.model(**changes),self.routes,self.sources,'Текст')

    async def test_review_uses_primary_sources_and_has_no_distribution_or_first_guess(self):
        model=self.model()
        await verify(model,self.routes,self.sources,'Текст')
        payload=json.loads(model.completion.call_args.args[0][1]['content'])
        self.assertEqual(payload['sources'],self.sources)
        self.assertNotIn('topic_usage',payload)
        self.assertNotIn('selected_topic',payload)
        self.assertFalse(model.completion.call_args.kwargs['with_tools'])

    async def test_native_provider_tools_are_not_an_execution_channel(self):
        model=SimpleNamespace(completion=AsyncMock(return_value={'tool_calls':[{'name':'send'}]}))
        with self.assertRaises(ValueError):await verify(model,self.routes,self.sources,'Текст')
