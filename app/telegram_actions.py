"""Typed Telegram operations shared by owner commands and independent plans."""
from __future__ import annotations

import asyncio
import copy
from datetime import datetime, timezone
import inspect
import json
import secrets
import time
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from telethon import functions, types, utils

from .dialogs import current_dialog
from .privacy import redact_locations
from .session_guard import SECURITY_PEER
from .telegram_account import safe_message, handle


# Arguments and categories are checked on the server, independently of a model's plan.
ACTION_FIELDS = {
    'web_search': {'query','limit'}, 'read_web': {'url'}, 'read_channel': {'recipient','limit'},
    'profile': {'recipient','include_photo'}, 'dialogs': {'limit'}, 'history': {'recipient','query','limit','before_id'},
    'contacts': {'limit'}, 'search': {'query','limit'}, 'channel_info': {'recipient'},
    'collect_media': {'recipient','limit'}, 'media_collection': {'query','kind','limit'},
    'inspect_media': {'asset_id'}, 'learn_sticker_pack': {'asset_id'},
    'find_stickers': {'query','limit'}, 'find_gifs': {'query','limit'}, 'find_photos': {'recipient','query','limit'}, 'sticker_sets': {'limit'},
    'stickers': {'pack','limit'}, 'send_sticker': {'recipient','asset_id','query'}, 'send_gif': {'recipient','asset_id','query'},
    'send_photo': {'recipient','asset_id','query'},
    'save_media': {'asset_id','remove','query','kind','message_id'}, 'send_text': {'recipient','text','message_id'},
    'send_voice': {'recipient','text'}, 'send_media': {'recipient','filename','text'},
    'reaction': {'recipient','message_id','emoji','remove'}, 'message_reactions': {'recipient','message_id','limit'},
    'poll': {'recipient','text','options','multiple','anonymous','quiz','correct_option'},
    'vote_poll': {'recipient','message_id','option_indices'}, 'close_poll': {'recipient','message_id'},
    'poll_results': {'recipient','message_id'}, 'save_note': {'text'}, 'save_message': {'recipient','message_ids'},
    'drafts': set(), 'draft': {'recipient','text','message_id'},
    'scheduled': {'recipient'}, 'schedule': {'recipient','text','when'},
    'send_scheduled': {'recipient','message_ids'}, 'cancel_scheduled': {'recipient','message_ids'},
    'folders': set(), 'folder': {'folder_id','title','recipients','exclude_recipients','folder_options'},
    'delete_folder': {'folder_id'}, 'stories': {'recipient'}, 'story_archive': {'limit'},
    'publish_story': {'filename','text','audience','period','pinned'},
    'edit_story': {'story_id','text','audience'}, 'delete_story': {'story_id'},
    'story_reaction': {'recipient','story_id','emoji','remove'}, 'hide_stories': {'recipient','enabled'},
    'create_group': {'title','recipients','description','forum'}, 'create_channel': {'title','description'},
    'join': {'recipient'}, 'leave': {'recipient'}, 'call': {'recipient','mode'},
    'accept_call': {'recipient','mode'}, 'hang_up': set(), 'call_mode': {'mode'}, 'call_status': set(),
    'profile_details': {'first_name','last_name','text'}, 'profile_photo': {'filename'},
    'profile_settings': {'operation'}, 'privacy': {'operation'}, 'dialog': {'recipient','operation'},
    'messages': {'recipient','operation'}, 'contact': {'recipient','operation'},
    'group': {'recipient','operation'},
}
READ_ACTIONS = frozenset({'web_search','read_web','read_channel','profile','dialogs','history','contacts','search','channel_info','collect_media',
    'media_collection','find_stickers','find_gifs','find_photos','sticker_sets','stickers','message_reactions',
    'poll_results','drafts','scheduled','folders','stories','story_archive','call_status','inspect_media','learn_sticker_pack'})
GLOBAL_READS = frozenset({'dialogs','contacts','drafts'})
SELF_ACTIONS = frozenset({'save_note','folders','folder','delete_folder','story_archive','publish_story',
    'edit_story','delete_story','create_group','create_channel','profile_details','profile_photo','profile_settings','privacy'})
DESTRUCTIVE = frozenset({'leave','delete_folder','delete_story','cancel_scheduled'})


class TelegramAction(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    action: str
    recipient: str | None = Field(default=None, max_length=512)
    text: str | None = Field(default=None, max_length=4000)
    url: str | None = Field(default=None,max_length=2000)
    query: str = Field(default='', max_length=100)
    limit: int = Field(default=15, ge=1, le=50)
    before_id: int = Field(default=0, ge=0)
    include_photo: bool = False
    filename: str | None = Field(default=None, max_length=180)
    asset_id: str | None = Field(default=None, max_length=180)
    kind: Literal['all','sticker','gif'] = 'all'
    pack: str | None = Field(default=None, max_length=64)
    message_id: int | None = Field(default=None, gt=0)
    message_ids: list[int] | None = Field(default=None, min_length=1, max_length=50)
    emoji: str | None = Field(default=None, max_length=32)
    remove: bool = False
    options: list[str] | None = Field(default=None, min_length=2, max_length=10)
    multiple: bool = False
    anonymous: bool = True
    quiz: bool = False
    correct_option: int | None = Field(default=None, ge=0, le=9)
    option_indices: list[int] | None = Field(default=None, max_length=10)
    when: str | None = Field(default=None, max_length=80)
    folder_id: int | None = Field(default=None, ge=2, le=255)
    title: str | None = Field(default=None, min_length=1, max_length=128)
    recipients: list[str] | None = Field(default=None, max_length=100)
    exclude_recipients: list[str] | None = Field(default=None, max_length=100)
    folder_options: dict[str,bool] | None = None
    story_id: int | None = Field(default=None, gt=0)
    audience: Literal['contacts','everyone','close_friends'] | None = None
    period: Literal[21600,43200,86400,172800] = 86400
    pinned: bool = False
    enabled: bool | None = None
    description: str = Field(default='', max_length=255)
    forum: bool = False
    mode: Literal['listen','talk'] = 'talk'
    first_name: str | None = Field(default=None, min_length=1, max_length=64)
    last_name: str | None = Field(default=None, max_length=64)
    operation: dict | None = None

    def checked(self):
        if self.action not in ACTION_FIELDS or self.model_fields_set-{'action'}-ACTION_FIELDS[self.action]:
            raise ValueError('Неизвестное действие Telegram или лишние параметры')
        fields = ACTION_FIELDS[self.action]
        required = {'read_web':['url'],'read_channel':['recipient'],'send_text':['text'],'send_voice':['text'],'schedule':['text','when'],
            'send_media':['filename'],'profile_photo':['filename'],'publish_story':['filename'],
            'inspect_media':['asset_id'],'learn_sticker_pack':['asset_id'],
            'reaction':['message_id'],'message_reactions':['message_id'],'poll':['text','options'],
            'vote_poll':['message_id','option_indices'],'close_poll':['message_id'],'poll_results':['message_id'],
            'save_note':['text'],'save_message':['message_ids'],'send_scheduled':['message_ids'],
            'cancel_scheduled':['message_ids'],'folder':['folder_id','title'],'delete_folder':['folder_id'],
            'edit_story':['story_id'],'delete_story':['story_id'],'story_reaction':['story_id'],
            'hide_stories':['enabled'],'create_group':['title'],'create_channel':['title'],
            'stickers':['pack'],'profile_settings':['operation'],'privacy':['operation'],
            'dialog':['operation'],'messages':['operation'],'contact':['operation'],'group':['operation']}.get(self.action,[])
        if any(getattr(self,key) is None for key in required):
            raise ValueError('Не хватает параметров: '+', '.join(required))
        if self.action in {'reaction','story_reaction'} and not self.remove and not self.emoji:
            raise ValueError('Выбери emoji или remove=true')
        if self.action in {'search','web_search'} and not self.query.strip():
            raise ValueError('Укажи запрос поиска')
        if self.action in {'find_photos','send_photo'} and not self.asset_id and not self.query.strip():
            raise ValueError('Для фото нужен предмет или сюжет поиска')
        if self.text is not None and self.action in {'send_text','send_voice','schedule','poll','save_note'} and not self.text.strip():
            raise ValueError('Текст не должен быть пустым')
        if self.options and (any(not 1<=len(x.strip())<=100 for x in self.options) or len(set(self.options))!=len(self.options)):
            raise ValueError('Варианты опроса: разные непустые тексты до 100 символов')
        if self.action=='poll':
            if len(self.text)>300 or self.quiz and (self.multiple or self.correct_option is None or self.correct_option>=len(self.options)):
                raise ValueError('Проверь вопрос и правильный вариант викторины')
            if not self.quiz and self.correct_option is not None:
                raise ValueError('Правильный ответ задаётся только для викторины')
        if self.message_ids and (any(type(x) is not int or x<=0 for x in self.message_ids) or len(self.message_ids)!=len(set(self.message_ids))):
            raise ValueError('Нужны положительные ID сообщений')
        if self.option_indices and any(type(x) is not int or not 0<=x<=9 for x in self.option_indices):
            raise ValueError('Нужны номера вариантов опроса, начиная с нуля')
        if self.folder_options and set(self.folder_options)-{'contacts','non_contacts','groups','broadcasts','bots','exclude_muted','exclude_read','exclude_archived'}:
            raise ValueError('Неизвестное правило папки чатов')
        if self.action=='edit_story' and self.text is None and self.audience is None:
            raise ValueError('Укажи новое описание или аудиторию истории')
        if self.action=='profile_details' and not self.model_fields_set & {'first_name','last_name','text'}:
            raise ValueError('Укажи имя, фамилию или био')
        return self


def action_category(op):
    a=op.action
    if a in {'profile_details','profile_photo','profile_settings','privacy'}:
        return 'profile'
    if a in {'call','accept_call','hang_up','call_mode','call_status'}:
        return 'calls'
    if a in {'stories','story_archive','publish_story','edit_story','delete_story','story_reaction','hide_stories'}:
        return 'stories'
    if a in {'group','create_group','create_channel','join','leave','channel_info'}:
        return 'groups'
    if a in {'folders','folder','delete_folder','draft','drafts','save_note','save_message','dialog','contact','scheduled','cancel_scheduled'}:
        return 'organize'
    if a in {'web_search','read_web','read_channel','profile','dialogs','history','contacts','search','collect_media','media_collection','find_stickers','find_gifs','find_photos','sticker_sets','stickers','save_media','inspect_media','learn_sticker_pack'}:
        return 'discovery'
    return 'messages'


def action_tools(tool):
    schema=TelegramAction.model_json_schema()
    properties=schema['properties']
    properties['action']={'type':'string','enum':list(ACTION_FIELDS)}
    properties['asset_id']['description']='Необязателен для send_sticker/send_gif/send_photo/save_media: без него сервер ищет по query и выбирает реальное медиа. save_media без query сохраняет медиа из Reply или последнее в текущем чате.'
    properties['query']['description']='Короткий сюжет поиска. Для фото find_photos/send_photo использует @pic. Не передавай личные данные переписки.'
    properties['recipient']['description']='@username/ID адресата; current — текущий собеседник, me — аккаунт Дарьи. Для «мой профиль» автором команды является current.'
    properties['operation']['description']='Параметры существующей команды manage_profile/manage_privacy/manage_dialog/manage_messages/manage_contact/manage_group. Для group — AdminOperation, без group/recipient. Не выдаёт права управления Дарьей.'
    return [tool('telegram_action', 'Расширенное действие Telegram по текущей команде владельца: поиск людей и каналов, профили, сообщения/медиа, опросы, реакции, коллекция стикеров/GIF, истории, черновики, отложенные сообщения, папки, группы/каналы и звонки. Используй реальные ID из предыдущих результатов. operation применяется к существующему действию аккаунта или группы.', properties, ['action'])]


class TelegramActions:
    def __init__(self, runtime):
        self.runtime,self.store,self.telegram=runtime,runtime.store,runtime.telegram
        self.discovered={}

    async def request(self, request, guard=lambda:None):
        guard()
        result=await asyncio.wait_for(self.telegram.client(request),25)
        if result is False:
            raise ValueError('Telegram не подтвердил действие')
        return result

    async def target(self, recipient, guard):
        entity,peer,chat_id=await self.runtime.account.resolve(recipient,guard)
        if isinstance(entity,types.User) and (entity.deleted or entity.id==SECURITY_PEER):
            raise ValueError('Этот пользователь недоступен')
        return entity,peer,chat_id

    def contact(self, entity, chat_id):
        contact=self.store.upsert_contact(chat_id,utils.get_display_name(entity),getattr(entity,'username','') or '',
            'private' if isinstance(entity,types.User) else 'group')
        if not contact['enabled'] or self.store.dialog_blocked(chat_id):
            raise ValueError('Общение с этим контактом выключено')
        return contact

    async def send(self, chat_id, label, operation, guard):
        async with self.runtime.send_lock:
            guard()
            self.telegram.require_unblocked(chat_id)
            if not self.store.contact(chat_id)['enabled']:
                raise ValueError('Отправка этому контакту выключена')
            self.runtime.require_active_dialog(chat_id)
            self.runtime.rate_check(chat_id,time.time(),self.store.settings())
            result=await operation()
            if isinstance(result,list):
                result=result[0] if len(result)==1 else None
            message_id=getattr(result,'id',None)
            if type(message_id) is not int:
                raise ValueError('Telegram не подтвердил отправку; автоматически не повторяю')
            self.store.message(chat_id,'assistant',label,message_id,'telegram_action')
            self.runtime.telegram_media.capture(result,chat_id)
            return {'sent':True,'telegram_id':message_id,'chat_id':chat_id}

    def public_file(self, filename):
        root=(self.store.directory/'media').resolve()
        if not filename or Path(filename).name!=filename:
            raise ValueError('Выбери файл из медиатеки')
        original=root/filename
        path=original.resolve()
        if original.is_symlink() or path.parent!=root or not path.is_file() or path.stat().st_size>20*1024*1024:
            raise ValueError('Файл недоступен или слишком большой')
        if self.store.rows('SELECT 1 FROM jobs WHERE artifact=?',(filename,)) or self.store.rows('SELECT 1 FROM dialog_artifacts WHERE filename=?',(filename,)):
            raise ValueError('Файл отдельного диалога нельзя использовать в публичном профиле или истории')
        return path

    async def upload(self, filename, guard):
        path=self.public_file(filename)
        if path.suffix.lower() in {'.jpg','.jpeg','.png','.webp'}:
            from .vision import normalize
            raw=await asyncio.to_thread(normalize,path.read_bytes())
            guard()
            file=await self.telegram.client.upload_file(raw,file_name='story.jpg')
            return types.InputMediaUploadedPhoto(file)
        if path.suffix.lower()!='.mp4':
            raise ValueError('Для истории выбери изображение или MP4')
        process=await asyncio.create_subprocess_exec('ffprobe','-v','error','-show_streams','-show_format','-of','json',str(path),stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
        try:
            output,_=await asyncio.wait_for(process.communicate(),15)
        except BaseException:
            if process.returncode is None:
                process.kill(); await process.wait()
            raise
        data=json.loads(output)
        stream=next((s for s in data.get('streams',[]) if s.get('codec_type')=='video'),None)
        duration=float(data.get('format',{}).get('duration',0))
        if process.returncode or not stream or not 0<duration<=60:
            raise ValueError('История MP4 должна быть длиной до 60 секунд')
        guard()
        file=await self.telegram.client.upload_file(path)
        return types.InputMediaUploadedDocument(file,'video/mp4',[types.DocumentAttributeVideo(duration,int(stream['width']),int(stream['height']),supports_streaming=True)])

    @staticmethod
    def audience(value):
        if value=='everyone': return [types.InputPrivacyValueAllowAll()]
        if value=='close_friends': return [types.InputPrivacyValueAllowCloseFriends()]
        return [types.InputPrivacyValueAllowContacts()]

    async def execute(self, args, guard, record, *, focus_id=0, autonomous=False, source=None, requester_id=0):
        op=TelegramAction.model_validate(args).checked()
        guard()
        a=op.action
        recipient=op.recipient or (str(focus_id) if focus_id else 'me')
        if recipient == 'current':
            recipient = str(requester_id or focus_id or self.store.settings()['owner_id'])
        async def mutate(fingerprint,operation):
            async def checked():
                guard()
                return await operation()
            return await record(fingerprint,checked)
        if a in {'web_search','read_web','read_channel'}:
            values=({'query':op.query,'limit':min(op.limit,10)} if a=='web_search' else
                    {'url':op.url or ''} if a=='read_web' else {'channel':op.recipient or '', 'limit':min(op.limit,20)})
            result=await self.runtime.research.execute({'web_search':'web_search','read_web':'read_web_page','read_channel':'read_telegram_channel'}[a],values,guard)
            guard();return result
        if a=='profile':
            result=await self.runtime.account.profile(recipient,op.include_photo); guard(); return result
        if a in {'dialogs','history'}:
            data=({'limit':op.limit} if a=='dialogs' else {'recipient':recipient,'query':op.query,'limit':op.limit,'before_id':op.before_id})
            return await self.runtime.account.execute('telegram_dialogs' if a=='dialogs' else 'telegram_history',data,guard,record)
        if a=='contacts':
            result=await self.request(functions.contacts.GetContactsRequest(0),guard)
            guard()
            return {'contacts':[{'id':u.id,'name':utils.get_display_name(u),'username':u.username or ''} for u in result.users if u.id!=SECURITY_PEER][:op.limit]}
        if a=='search':
            result=await self.request(functions.contacts.SearchRequest(op.query,op.limit),guard); guard()
            for e in list(result.users)+list(result.chats):
                eid=utils.get_peer_id(e)
                if eid==SECURITY_PEER or getattr(e,'bot',False) or getattr(e,'deleted',False): continue
                self.discovered[eid]={'id':eid,'name':utils.get_display_name(e),'username':getattr(e,'username','') or '',
                    'kind':'private' if isinstance(e,types.User) else 'group'}
            if len(self.discovered)>200: self.discovered=dict(list(self.discovered.items())[-200:])
            return {'users':[{'id':u.id,'name':utils.get_display_name(u),'username':u.username or ''} for u in result.users if u.id!=SECURITY_PEER],
                'chats':[{'id':utils.get_peer_id(c),'name':getattr(c,'title',''),'username':getattr(c,'username','') or '',
                    'kind':'channel' if getattr(c,'broadcast',False) else 'group'} for c in result.chats]}
        if a in {'find_stickers','find_gifs','find_photos','media_collection'}:
            values={'query':op.query,'limit':min(op.limit,20)}
            if a=='media_collection': values['kind']=op.kind
            search_chat = focus_id
            if a == 'find_photos' and op.recipient:
                _,_,search_chat = await self.target(recipient,guard)
            result=await self.runtime.telegram_media.execute(search_chat,a,values,guard); guard(); return result
        if a=='save_media':
            fingerprint=json.dumps(args,sort_keys=True,ensure_ascii=False)
            return await mutate(fingerprint,lambda:self.runtime.telegram_media.save_selected(focus_id,args,source,guard))
        if a=='inspect_media': return await self.runtime.telegram_media.inspect(op.asset_id,guard)
        if a=='learn_sticker_pack': return await self.runtime.telegram_media.learn_pack(op.asset_id,guard)
        if a=='sticker_sets':
            result=await self.request(functions.messages.GetAllStickersRequest(0),guard); guard()
            return {'sets':[{'id':s.id,'short_name':s.short_name,'title':s.title,'count':s.count} for s in result.sets[:op.limit]]}
        if a=='stickers':
            if not op.pack or not op.pack.replace('_','').isalnum(): raise ValueError('Укажи short_name набора')
            result=await self.request(functions.messages.GetStickerSetRequest(types.InputStickerSetShortName(op.pack),0),guard); guard()
            return {'items':[item for d in result.documents[:op.limit] if (item:=self.runtime.telegram_media.cache(d,pack=op.pack))]}
        if a in {'call','accept_call','hang_up','call_mode','call_status'}:
            if a=='call_status': return self.runtime.calls.status()
            if a=='call': return await mutate(recipient,lambda:self.runtime.calls.dial(recipient,op.mode,guard))
            if a=='accept_call': return await mutate(recipient,lambda:self.runtime.calls.accept(op.recipient or (str(focus_id) if focus_id else None),op.mode,guard))
            if a=='hang_up': return await mutate('current',self.runtime.calls.leave)
            return await mutate('current',lambda:self.runtime.calls.mode(op.mode))
        if a=='profile_details':
            values={key:value for key,value in {'first_name':op.first_name,'last_name':op.last_name,'about':op.text}.items() if value is not None}
            async def change():
                await self.request(functions.account.UpdateProfileRequest(**values),guard)
                self.runtime.account.profiles.clear()
                return {'changed':True,'text':'Обновила имя и описание профиля.'}
            return await mutate('self',change)
        if a=='profile_photo':
            path=self.public_file(op.filename)
            if path.suffix.lower() not in {'.jpg','.jpeg','.png','.webp'}: raise ValueError('Для аватара выбери изображение')
            from .vision import normalize
            photo=await asyncio.to_thread(normalize,path.read_bytes())
            async def change():
                guard(); await self.telegram.set_profile_photo(photo,before_dispatch=guard); self.runtime.account.profiles.clear()
                return {'changed':True,'text':'Обновила аватар.'}
            return await mutate(op.filename,change)
        if a in {'profile_settings','privacy','dialog','messages','contact'}:
            names={'profile_settings':'manage_profile','privacy':'manage_privacy','dialog':'manage_dialog','messages':'manage_messages','contact':'manage_contact'}
            values=dict(op.operation)
            if a in {'dialog','messages','contact'}:
                if 'recipient' in values and values['recipient']!=recipient: raise ValueError('Адресаты действия не совпадают')
                values['recipient']=recipient
            return await self.runtime.account.execute(names[a],values,guard,record)
        if a=='group':
            from .group_admin import AdminOperation
            command=AdminOperation.model_validate(op.operation).checked()
            if command.action in {'grant_control','revoke_control','list_controllers'}:
                raise ValueError('Разрешения управления Дарьей выдаются отдельной командой настоящего владельца')
            actor = requester_id or self.store.settings()['owner_id']
            return await mutate(recipient,lambda:self.runtime.group_admin.execute(recipient,command,guard,actor_id=actor))
        if a in {'folders','folder','delete_folder'}:
            result=await self.request(functions.messages.GetDialogFiltersRequest(),guard); guard()
            filters=getattr(result,'filters',result)
            if a=='folders':
                return {'folders':[{'id':f.id,'title':getattr(getattr(f,'title',None),'text',str(getattr(f,'title',''))),
                    'included':[utils.get_peer_id(p) for p in getattr(f,'include_peers',[])],
                    'excluded':[utils.get_peer_id(p) for p in getattr(f,'exclude_peers',[])]} for f in filters if hasattr(f,'title')]}
            existing=next((f for f in filters if getattr(f,'id',None)==op.folder_id),None)
            if a=='delete_folder':
                if not existing: return {'changed':False,'text':'Такой папки уже нет.'}
                async def change():
                    await self.request(functions.messages.UpdateDialogFilterRequest(op.folder_id),guard)
                    return {'changed':True,'text':'Папка чатов удалена.'}
                return await mutate(str(op.folder_id),change)
            if len(op.title)>12: raise ValueError('Название папки должно быть не длиннее 12 символов')
            includes=([ (await self.target(r,guard))[1] for r in op.recipients] if op.recipients is not None else list(getattr(existing,'include_peers',[])))
            excludes=([ (await self.target(r,guard))[1] for r in op.exclude_recipients] if op.exclude_recipients is not None else list(getattr(existing,'exclude_peers',[])))
            filter_=types.DialogFilter(op.folder_id,types.TextWithEntities(op.title,[]),list(getattr(existing,'pinned_peers',[])),includes,excludes,
                emoticon=getattr(existing,'emoticon',None),color=getattr(existing,'color',None),title_noanimate=getattr(existing,'title_noanimate',None),
                **{key:(op.folder_options or {}).get(key,getattr(existing,key,None)) for key in ['contacts','non_contacts','groups','broadcasts','bots','exclude_muted','exclude_read','exclude_archived']})
            async def change():
                await self.request(functions.messages.UpdateDialogFilterRequest(op.folder_id,filter_),guard)
                return {'changed':True,'folder_id':op.folder_id,'text':'Папка чатов сохранена.'}
            return await mutate(str(op.folder_id),change)
        if a in {'create_group','create_channel'}:
            users=[]
            for r in op.recipients or []:
                e,_,_=await self.target(r,guard)
                if not isinstance(e,types.User) or e.bot: raise ValueError('В группу приглашаются пользователи')
                self.telegram.require_unblocked(e.id)
                contacts=self.store.rows('SELECT enabled FROM contacts WHERE id=?',(e.id,))
                if contacts and not contacts[0]['enabled']: raise ValueError('Общение с одним из участников выключено')
                users.append(utils.get_input_user(e))
            async def create():
                if a=='create_group' and users and not op.forum:
                    response=await self.request(functions.messages.CreateChatRequest(users,op.title),guard)
                else:
                    response=await self.request(functions.channels.CreateChannelRequest(op.title,op.description,
                        broadcast=a=='create_channel',megagroup=a=='create_group',forum=op.forum if a=='create_group' else None),guard)
                chats=getattr(response,'chats',None) or getattr(getattr(response,'updates',None),'chats',[])
                if not chats: raise ValueError('Telegram не подтвердил создание чата')
                chat=chats[0]; chat_id=utils.get_peer_id(chat); self.contact(chat,chat_id)
                if users and isinstance(chat,types.Channel):
                    # Creation succeeded even if a later invitation is unavailable.
                    try: await self.request(functions.channels.InviteToChannelRequest(utils.get_input_channel(chat),users),guard)
                    except Exception: return {'created':True,'chat_id':chat_id,'text':'Чат создан; приглашения участников не подтверждены.'}
                return {'created':True,'chat_id':chat_id,'text':'Создала '+('канал' if a=='create_channel' else 'группу')+': '+op.title}
            return await mutate(op.title,create)
        if a in {'publish_story','edit_story','delete_story','story_archive'}:
            _,peer,_=await self.target('me',guard)
            if a=='story_archive':
                result=await self.request(functions.stories.GetStoriesArchiveRequest(peer,0,op.limit),guard); guard()
                return {'stories':self.story_rows(getattr(result,'stories',[]))}
            if a=='publish_story':
                media=await self.upload(op.filename,guard)
                async def publish():
                    result=await self.request(functions.stories.SendStoryRequest(peer,media,self.audience(op.audience or 'contacts'),
                        caption=op.text or '',random_id=secrets.randbits(63),period=op.period,pinned=op.pinned),guard)
                    ids=[getattr(getattr(u,'story',None),'id',None) for u in getattr(result,'updates',[])]
                    return {'published':True,'story_ids':[i for i in ids if i],'text':'История опубликована.'}
                return await mutate(op.filename,publish)
            async def change():
                request=(functions.stories.DeleteStoriesRequest(peer,[op.story_id]) if a=='delete_story' else
                    functions.stories.EditStoryRequest(peer,op.story_id,caption=op.text,
                        privacy_rules=self.audience(op.audience) if op.audience else None))
                await self.request(request,guard)
                return {'changed':True,'story_id':op.story_id,'text':'История удалена.' if a=='delete_story' else 'История обновлена.'}
            return await mutate(str(op.story_id),change)
        if a=='drafts':
            result=await self.request(functions.messages.GetAllDraftsRequest(),guard); guard()
            drafts=[]
            for u in getattr(result,'updates',[]):
                if isinstance(u,types.UpdateDraftMessage) and utils.get_peer_id(u.peer)!=SECURITY_PEER:
                    drafts.append({'chat_id':utils.get_peer_id(u.peer),'text':redact_locations(getattr(u.draft,'message',''))[:1000]})
            return {'drafts':drafts}
        if a=='save_note':
            recipient='me'
        entity,peer,chat_id=await self.target(recipient,guard)
        guard()
        if a=='channel_info':
            if not isinstance(entity,types.Channel): raise ValueError('Выбери канал или супергруппу')
            result=await self.request(functions.channels.GetFullChannelRequest(utils.get_input_channel(entity)),guard); guard()
            return {'id':chat_id,'title':entity.title,'username':entity.username or '',
                'about':redact_locations(result.full_chat.about),'members':result.full_chat.participants_count,
                'linked_chat_id':getattr(result.full_chat,'linked_chat_id',None),'broadcast':bool(entity.broadcast)}
        if a=='collect_media':
            messages=await asyncio.wait_for(self.telegram.client.get_messages(entity,limit=op.limit),20); guard()
            items=[item for m in messages if safe_message(m) and (item:=self.runtime.telegram_media.capture(m,chat_id))]
            return {'collected':len(items),'items':items}
        if a in {'stories','story_reaction','hide_stories'}:
            if a=='stories':
                result=await self.request(functions.stories.GetPeerStoriesRequest(peer),guard); guard()
                return {'stories':self.story_rows(getattr(result.stories,'stories',[]))}
            async def change():
                request=(functions.stories.TogglePeerStoriesHiddenRequest(peer,op.enabled) if a=='hide_stories' else
                    functions.stories.SendReactionRequest(peer,op.story_id,types.ReactionEmpty() if op.remove else types.ReactionEmoji(op.emoji)))
                await self.request(request,guard)
                return {'changed':True,'text':'Настройка историй обновлена.' if a=='hide_stories' else 'Реакция на историю отправлена.'}
            return await mutate(str(op.story_id or chat_id),change)
        if a in {'join','leave'}:
            async def membership():
                if a=='join':
                    if not isinstance(entity,types.Channel): raise ValueError('Для вступления нужна группа или канал')
                    await self.request(functions.channels.JoinChannelRequest(utils.get_input_channel(entity)),guard)
                elif isinstance(entity,types.Channel):
                    await self.request(functions.channels.LeaveChannelRequest(utils.get_input_channel(entity)),guard)
                elif isinstance(entity,types.Chat):
                    await self.request(functions.messages.DeleteChatUserRequest(entity.id,types.InputUserSelf(),revoke_history=False),guard)
                else: raise ValueError('Выбери группу или канал')
                if a=='join': self.contact(entity,chat_id)
                return {'changed':True,'chat_id':chat_id,'text':'Вступила в чат.' if a=='join' else 'Вышла из чата.'}
            return await mutate(str(chat_id),membership)
        self.contact(entity,chat_id)
        if a in {'send_sticker','send_gif','send_photo'}:
            kind={'send_sticker':'sticker','send_gif':'gif','send_photo':'photo'}[a]
            async def send_selected():
                asset_id=op.asset_id or await self.runtime.telegram_media.select(chat_id,kind,op.query,guard)
                topic=self.store.group_topic(chat_id,source.get('telegram_id')) if source and source.get('contact_id')==chat_id else 0
                return await self.runtime.telegram_media.send(chat_id,asset_id,kind,guard,automated=autonomous,
                    **({'reply_to':topic} if topic else {}))
            return await mutate(json.dumps({'chat':chat_id,**args},sort_keys=True,ensure_ascii=False),send_selected)
        if a in {'send_text','save_note'}:
            async def send_text():
                message_id=await self.runtime.send(chat_id,op.text,proactive=autonomous and chat_id!=self.telegram.account_id,
                    automated=autonomous,before_dispatch=guard,reply_to=op.message_id or
                        (self.store.group_topic(chat_id,source.get('telegram_id')) if source and source.get('contact_id')==chat_id else None))
                return {'sent':True,'telegram_id':message_id,'chat_id':chat_id,'text':'Сообщение отправлено.'}
            return await mutate(str(chat_id),send_text)
        if a=='send_voice':
            async def send_voice():
                original_id=source.get('telegram_id') if source and source.get('contact_id')==chat_id else None
                message_id=await self.runtime.send_voice_reply(chat_id,original_id,op.text,proactive=autonomous,before_dispatch=guard,manual=not autonomous)
                return {'sent':True,'telegram_id':message_id,'text':'Голосовое отправлено.'}
            return await mutate(str(chat_id),send_voice)
        if a=='send_media':
            return await mutate(op.filename,lambda:self.runtime.send_file(chat_id,op.filename,op.text or '',automated=autonomous,before_dispatch=guard))
        if a=='draft':
            async def change():
                await self.request(functions.messages.SaveDraftRequest(peer,op.text or '',reply_to=types.InputReplyToMessage(op.message_id) if op.message_id else None),guard)
                return {'changed':True,'text':'Черновик сохранён.' if op.text else 'Черновик очищен.'}
            return await mutate(str(chat_id),change)
        if a in {'scheduled','send_scheduled','cancel_scheduled','schedule'}:
            if a=='schedule':
                from .runtime import due_time
                when=datetime.fromtimestamp(due_time(op.when),timezone.utc)
                async def schedule():
                    guard()
                    if autonomous:
                        task_id=self.store.schedule(chat_id,op.text,when.timestamp())
                        return {'scheduled':True,'task_id':task_id,'text':'Запланировала сообщение; перед отправкой проверю текущие разрешения.'}
                    actor = requester_id or self.store.settings()['owner_id']
                    if actor != self.store.settings()['owner_id'] and not self.store.contact(chat_id)['proactive']:
                        raise ValueError('В панели пока не разрешены самостоятельные сообщения этому контакту')
                    async with self.runtime.send_lock:
                        guard()
                        self.telegram.require_unblocked(chat_id)
                        self.runtime.rate_check(chat_id,time.time(),self.store.settings())
                        message=await self.telegram.client.send_message(entity,op.text,parse_mode=None,link_preview=False,schedule=when)
                        if not isinstance(message,types.Message): raise ValueError('Telegram не подтвердил отложенное сообщение')
                        return {'scheduled':True,'scheduled_message_id':message.id,'text':'Сообщение отложено в Telegram.'}
                return await mutate(str(chat_id),schedule)
            scheduled=await self.request(functions.messages.GetScheduledHistoryRequest(peer,0),guard); guard()
            if a=='scheduled':
                return {'messages':[{'message_id':m.id,'text':redact_locations(m.message or '')[:1000],'date':m.date.isoformat()} for m in scheduled.messages if safe_message(m)]}
            if not set(op.message_ids)<={m.id for m in scheduled.messages if safe_message(m)}:
                raise ValueError('Отложенные сообщения не найдены')
            async def change():
                cls=functions.messages.SendScheduledMessagesRequest if a=='send_scheduled' else functions.messages.DeleteScheduledMessagesRequest
                await self.request(cls(peer,op.message_ids),guard)
                return {'changed':True,'text':'Отложенные сообщения отправлены.' if a=='send_scheduled' else 'Отложенные сообщения отменены.'}
            return await mutate(','.join(map(str,op.message_ids)),change)
        if a=='save_message':
            selected=await asyncio.wait_for(self.telegram.client.get_messages(entity,ids=op.message_ids),20); guard()
            if not selected or any(not isinstance(m,types.Message) or not safe_message(m) or getattr(m,'noforwards',False) for m in selected):
                raise ValueError('Эти сообщения нельзя сохранить')
            if set(m.id for m in selected)!=set(op.message_ids) or any(utils.get_peer_id(m.peer_id)!=chat_id for m in selected):
                raise ValueError('Telegram вернул сообщения другого чата или не все выбранные сообщения')
            async def save():
                await self.telegram.client.forward_messages('me',op.message_ids,from_peer=entity)
                return {'saved':True,'text':'Сохранила сообщения в Избранном.'}
            return await mutate(','.join(map(str,op.message_ids)),save)
        if a=='poll':
            poll=types.Poll(secrets.randbits(63),types.TextWithEntities(op.text,[]),
                [types.PollAnswer(types.TextWithEntities(t,[]),bytes([i])) for i,t in enumerate(op.options)],hash=0,
                public_voters=not op.anonymous,multiple_choice=op.multiple,quiz=op.quiz)
            media=types.InputMediaPoll(poll,correct_answers=[op.correct_option] if op.quiz else None)
            return await mutate(str(chat_id),lambda:self.send(chat_id,'[Опрос] '+op.text,lambda:self.telegram.client.send_file(entity,media),guard))
        if a in {'reaction','message_reactions','vote_poll','close_poll','poll_results'}:
            message=await asyncio.wait_for(self.telegram.client.get_messages(entity,ids=op.message_id),20); guard()
            if not isinstance(message,types.Message) or not safe_message(message): raise ValueError('Сообщение недоступно')
            if a=='message_reactions':
                result=await self.request(functions.messages.GetMessageReactionsListRequest(peer,op.message_id,op.limit),guard); guard()
                return {'count':result.count,'reactions':[{'user_id':utils.get_peer_id(r.peer_id),'reaction':getattr(r.reaction,'emoticon',getattr(r.reaction,'document_id',''))} for r in result.reactions]}
            if a=='reaction':
                async def react():
                    await self.request(functions.messages.SendReactionRequest(peer,op.message_id,reaction=[] if op.remove else [types.ReactionEmoji(op.emoji)]),guard)
                    return {'changed':True,'text':'Реакция отправлена.' if not op.remove else 'Реакция убрана.'}
                return await mutate(str(op.message_id),react)
            poll=getattr(message,'poll',None)
            if poll is None: raise ValueError('Это сообщение не является опросом')
            if a=='poll_results':
                result=await self.request(functions.messages.GetPollResultsRequest(peer,op.message_id),guard); guard()
                return {'poll_id':poll.poll.id,'options':[getattr(x.text,'text',str(x.text)) for x in poll.poll.answers],
                    'results':result.to_dict()}
            if a=='vote_poll':
                if any(i>=len(poll.poll.answers) for i in op.option_indices) or not poll.poll.multiple_choice and len(op.option_indices)>1:
                    raise ValueError('Проверь выбранные варианты опроса')
                async def vote():
                    await self.request(functions.messages.SendVoteRequest(peer,op.message_id,[poll.poll.answers[i].option for i in op.option_indices]),guard)
                    return {'changed':True,'text':'Проголосовала в опросе.'}
                return await mutate(str(op.message_id),vote)
            if not message.out: raise ValueError('Остановить можно только свой опрос')
            closed=copy.copy(poll.poll); closed.closed=True
            async def close():
                await self.telegram.client.edit_message(entity,op.message_id,file=types.InputMediaPoll(closed))
                return {'changed':True,'text':'Опрос остановлен.'}
            return await mutate(str(op.message_id),close)
        raise ValueError('Действие пока недоступно')

    @staticmethod
    def story_rows(stories):
        return [{'story_id':s.id,'caption':redact_locations(getattr(s,'caption','') or '')[:1000],
            'expires':getattr(s,'expire_date',None).isoformat() if getattr(s,'expire_date',None) else None,
            'has_media':bool(getattr(s,'media',None))} for s in stories if isinstance(s,types.StoryItem)]
