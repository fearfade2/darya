import asyncio
from datetime import datetime, timezone
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock

from telethon import types, utils

from app.runtime import Runtime
from app.store import Store

OWNER=1000000001
SELF=90
GROUP=-1000000000100
NOW=datetime.now(timezone.utc)


def document(id=123,kind='sticker'):
    attributes=([types.DocumentAttributeSticker('😄',types.InputStickerSetEmpty())] if kind=='sticker' else
                [types.DocumentAttributeAnimated(),types.DocumentAttributeVideo(2.0,160,160)])
    return types.Document(id,456,b'reference',NOW,'image/webp' if kind=='sticker' else 'video/mp4',100,1,attributes,thumbs=[])


def message(id=500,doc=None,chat=10,sender=20,out=False,text=''):
    peer=types.PeerChannel(100) if chat==GROUP else types.PeerUser(chat)
    return types.Message(id,peer,NOW,text,out=out,from_id=types.PeerUser(sender),
                         media=types.MessageMediaDocument(document=doc) if doc else None)


class TelegramFixture(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.store=Store(Path(self.tmp.name))
        for id,name,username in [(10,'Оля','olga_test'),(20,'Саша','sasha_test'),(OWNER,'Владелец','owner_test'),(SELF,'Дарья','darya_test')]:
            self.store.upsert_contact(id,name,username)
        self.store.upsert_contact(GROUP,'Группа','group_test','group')
        self.store.execute('UPDATE contacts SET proactive=1')
        self.store.update_settings({'owner_id':OWNER,'enabled':True,'quiet_start':0,'quiet_end':0,'timezone':'UTC',
            'natural_enabled':False,'typing_enabled':False,'min_interval':0})
        self.store.update_secrets({'ai_key':'test'})
        self.rt=Runtime(self.store)
        self.rt.telegram.ready=Mock(return_value=True)
        self.rt.telegram.account_id=SELF
        self.rt.telegram.name='Дарья'
        self.users={id:types.User(id,access_hash=123,first_name=str(id),username=f'user_{id}') for id in (10,20,OWNER,SELF,777000)}
        self.users[SELF].is_self=True
        self.channel=types.Channel(100,'Группа',types.ChatPhotoEmpty(),NOW,access_hash=123,megagroup=True,username='group_test')
        self.client=AsyncMock(return_value=True)
        async def entity(value):
            if value in {'me','self'}: return self.users[SELF]
            if value in {GROUP,'@group_test','group_test'}: return self.channel
            aliases={'@olga_test':10,'@sasha_test':20,'@owner_test':OWNER}
            return self.users[aliases.get(value,int(value) if isinstance(value,(int,str)) and str(value).isdigit() else 10)]
        self.client.get_entity.side_effect=entity
        self.client.get_input_entity.side_effect=lambda e:utils.get_input_peer(e) if isinstance(e,(types.User,types.Channel)) else types.InputPeerUser(int(e),123)
        self.client.get_messages=AsyncMock(return_value=[])
        self.client.send_file=AsyncMock(return_value=message(out=True,sender=SELF))
        self.client.send_message=AsyncMock(return_value=message(out=True,sender=SELF,text='Текст'))
        self.rt.telegram.client=self.client
        self.rt.telegram.send=AsyncMock(return_value=500)
        self.rt.telegram.read=AsyncMock()
        self.rt.calls.voice.release=AsyncMock()
        self.rt.model.completion=AsyncMock()
        self.media=self.rt.telegram_media
        self.actions=self.rt.telegram_actions
        self.auto=self.rt.telegram_autonomy

    async def asyncTearDown(self):
        await self.rt.stop()
        self.store.close();self.tmp.cleanup()

    def autonomy(self,**values):
        self.store.update_settings({'telegram_autonomy_enabled':True,**values})

    def replies(self,*values):
        import json
        self.rt.model.completion.side_effect=[{'content':json.dumps(v,ensure_ascii=False)} for v in values]

    async def execute(self,args,guard=lambda:None,autonomous=False,focus=0):
        async def record(fingerprint,operation): return await operation()
        return await self.actions.execute(args,guard,record,focus_id=focus,autonomous=autonomous)
