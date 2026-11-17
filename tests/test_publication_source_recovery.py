from unittest.mock import AsyncMock

from telethon import types,utils

from telegram_fixtures import TelegramFixture,NOW


class PublicationSourceRecoveryTests(TelegramFixture):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.source=types.Channel(200,'Public API',types.ChatPhotoEmpty(),NOW,access_hash=123,broadcast=True,username='public_api')
        self.peer=utils.get_peer_id(self.source)
        original=self.client.get_entity.side_effect
        async def resolve(value):
            if value==self.peer:raise ValueError('Could not find input entity')
            if value=='public_api':return self.source
            return await original(value)
        self.client.get_entity.side_effect=resolve

    async def remember_source(self):
        feed=await self.rt.publications.save({'name':'API','topic':'API','destination':'@group_test',
            'sources':[{'kind':'telegram_chat','value':str(self.peer)}]})
        self.rt.publications.remember(self.rt.publications.feed(feed['feed_id']),str(self.peer),
            {'title':'Public post','text':'A public post','url':'https://t.me/public_api/101','payload':{}})

    async def test_cold_source_is_restored_from_its_actual_public_post(self):
        await self.remember_source()
        result=await self.rt.publications.telegram_sources.resolve_source(str(self.peer),lambda:None)
        self.assertEqual(utils.get_peer_id(result),self.peer)
        self.client.get_dialogs.assert_not_awaited()

    async def test_changed_username_cannot_retarget_a_saved_source(self):
        await self.remember_source()
        self.source.id=201
        self.client.get_dialogs.return_value=[]
        with self.assertRaises(ValueError):
            await self.rt.publications.telegram_sources.resolve_source(str(self.peer),lambda:None)

    async def test_private_source_can_be_restored_from_same_dialog_id(self):
        self.source.username=None
        self.client.get_dialogs.return_value=[type('Dialog',(),{'id':self.peer,'entity':self.source})()]
        result=await self.rt.publications.telegram_sources.resolve_source(str(self.peer),lambda:None)
        self.assertEqual(utils.get_peer_id(result),self.peer)
