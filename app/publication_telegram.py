"""Explicit Telegram sources, forum destinations and original publication attachments."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from urllib.parse import urlsplit

from telethon import errors, functions, types, utils

from .groups import target
from .telegram_account import safe_message
from .publication_materials import digest_parts


MONITOR_ACTIONS = {'monitor_source', 'stop_monitoring', 'monitoring_status'}
REFERENCE = r'(?:https://t\.me/\S+|@[A-Za-z][A-Za-z0-9_]{3,31}|-?[0-9]{1,20})'


def parse_topic_publication_command(text):
    text=re.sub(r'^(?:(?:дарья|даша|дашка|даш|дура|пожалуйста)[,\s]+)+','',text.strip(),flags=re.I).casefold().rstrip('.! ')
    place=r'(?:сюда|тут|здесь|в эту тему|в этой теме|в этот раздел|в этом разделе)'
    if re.fullmatch(r'(?:'+place+r'\s+(?:ничего\s+)?не\s+(?:публикуй|пости)|не\s+(?:публикуй|пости)\s+(?:ничего\s+)?'+place+r')',text):
        return {'action':'topic_publications','enabled':False}
    if re.fullmatch(r'(?:разрешаю\s+(?:публикации|публиковать)|(?:снова\s+)?(?:публикуй|пости))\s+'+place,text):
        return {'action':'topic_publications','enabled':True}
    return None


def parse_monitor_command(text):
    text = re.sub(r'^(?:(?:дарья|даша|дашка|даш|дашуля|дашуль)[,\s]+|пожалуйста[,\s]+)+','',text.strip(),flags=re.I).rstrip('.! ')
    join_verbs = r'вступи|зайди|подпишись|присоединись'
    source_first = re.fullmatch(r'(?P<source>'+REFERENCE+r')\s+(?P<verb>'+join_verbs+r')\s+(?P<rest>.+)',text,re.I)
    if source_first:
        text = source_first['verb']+' '+source_first['source']+' '+source_first['rest']
    match = re.fullmatch(r'(?:мониторь|мониторить|отслеживай|следи за|добавь в мониторинг)\s+(?:(?:канал|группу|группой|каналом)\s+)?(?P<source>'+REFERENCE+r')(?P<rest>.*)',text,re.I)
    join = False
    if not match:
        match = re.fullmatch(r'(?:'+join_verbs+r')\s+(?:(?:в|на|к)\s+)?(?P<source>'+REFERENCE+r')\s+(?:и\s+)?(?:мониторь|отслеживай)(?:\s+(?:его|её|ее|её сообщения|ее сообщения))?(?P<rest>.*)',text,re.I)
        join = bool(match)
    if match:
        rest = match['rest'].strip()
        args = {'action':'monitor_source','target':match['source'],**({'join':True} if join else {})}
        dest = re.fullmatch(r'(?:и\s+)?(?:публикуй|публиковать|скидывай|отправляй)\s+(?:(?:материалы|всё|все|новое)\s+)?(?:в\s+)?('+REFERENCE+r')(?:\s+по темам)?',rest,re.I)
        topic = re.fullmatch(r'(?:и\s+)?(?:публикуй|скидывай|отправляй)\s+(?:в\s+)?тему\s+(.{1,100})',rest,re.I)
        if dest:args['destination']=dest[1]
        elif topic:
            name = topic[1].strip(' «»"')
            args['topic_id' if name.isdigit() else 'topic_name'] = int(name) if name.isdigit() else name
        elif ' '.join(rest.casefold().split()) not in {'','тут','здесь','публикуй','и публикуй','публикуй по темам','и публикуй тут','и публикуй здесь','и публикуй по темам','и публикуй сюда по темам','и публикуй тут по темам','по темам',
                'и публикуй в нужные темы','и публикуй в подходящие темы','и публикуй по нужным темам','и публикуй по подходящим темам'}:
            return None
        return args
    match = re.fullmatch(r'(?:не мониторь|перестань мониторить|не отслеживай|убери из мониторинга)\s+('+REFERENCE+r')',text,re.I)
    if match:return {'action':'stop_monitoring','target':match[1]}
    if text.casefold() in {'отключи мониторинг','останови мониторинг'}:return {'action':'stop_monitoring'}
    if text.casefold() in {'покажи мониторинг','какие источники ты мониторишь','что ты мониторишь','список источников'}:return {'action':'monitoring_status'}
    return None


def media_id(message):
    media = getattr(message,'media',None)
    obj = getattr(media,'photo',None) or getattr(media,'document',None)
    return getattr(obj,'id',None)


class TelegramPublications:
    def __init__(self, publications):
        self.pub, self.runtime, self.store = publications, publications.runtime, publications.store

    def owner_guard(self, actor, guard):
        guard()
        if actor != self.store.settings()['owner_id']:
            raise ValueError('Постоянный мониторинг настраивает только владелец')

    async def resolve_source(self, value, guard, *, join=False):
        parsed = target(value)
        client = self.runtime.telegram.client
        guard()
        if parsed['kind']=='invite':
            invite = await client(functions.messages.CheckChatInviteRequest(parsed['value']))
            guard()
            if isinstance(invite,types.ChatInviteAlready):entity=invite.chat
            else:
                if not join:raise ValueError('Сначала попроси вступить по этой ссылке, затем включи мониторинг')
                if getattr(invite,'subscription_pricing',None):raise ValueError('Этот источник требует платной подписки')
                try:
                    result=await client(functions.messages.ImportChatInviteRequest(parsed['value']))
                except errors.InviteRequestSentError:
                    return None
                guard()
                entity=next((c for c in result.chats if isinstance(c,(types.Chat,types.Channel))),None)
        else:
            try:
                entity=await client.get_entity(parsed['value'])
            except ValueError:
                if type(parsed['value']) is not int:raise
                peer=parsed['value']
                contacts=self.store.rows('SELECT username FROM contacts WHERE id=?',(peer,))
                names=[row['username'] for row in contacts if row['username']]
                for row in self.store.rows('SELECT url FROM publication_items WHERE source=? ORDER BY id DESC LIMIT 5',(value,)):
                    url=urlsplit(row['url']);parts=url.path.strip('/').split('/')
                    if url.hostname in {'t.me','telegram.me'} and len(parts)==2 and re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{3,31}',parts[0]):
                        names.append(parts[0])
                entity=None
                for name in dict.fromkeys(names):
                    guard()
                    try:candidate=await client.get_entity(name)
                    except ValueError:continue
                    guard()
                    if isinstance(candidate,(types.Chat,types.Channel)) and utils.get_peer_id(candidate)==peer:
                        entity=candidate;break
                if entity is None:
                    dialogs=await client.get_dialogs(limit=200)
                    guard()
                    entity=next((dialog.entity for dialog in dialogs if dialog.id==peer),None)
                if entity is None:raise ValueError('Не удалось восстановить доступ к настроенному источнику')
            guard()
            if join and isinstance(entity,types.Channel):
                try:await client(functions.channels.JoinChannelRequest(entity))
                except errors.UserAlreadyParticipantError:pass
                except errors.InviteRequestSentError:return None
                guard()
        if not isinstance(entity,(types.Chat,types.Channel)):
            raise ValueError('Источник должен быть каналом или группой, а не личным чатом')
        if getattr(entity,'noforwards',False):raise ValueError('Источник запретил копирование материалов')
        if getattr(entity,'left',False) and not join and not getattr(entity,'username',None):raise ValueError('Сначала попроси вступить в этот источник')
        self.runtime.telegram.require_unblocked(utils.get_peer_id(entity))
        return entity

    async def routes(self, entity, guard):
        if not isinstance(entity,types.Channel) or not entity.forum:return []
        result=await self.runtime.telegram.client(functions.messages.GetForumTopicsRequest(entity,None,0,0,100))
        guard()
        saved=self.destination()
        profiles={r['topic_id']:r for r in saved['routes']} if saved and saved['target_id']==utils.get_peer_id(entity) else {}
        routes=[]
        for t in result.topics:
            name=getattr(t,'title','')
            if (not name or getattr(t,'closed',False) or getattr(t,'hidden',False)
                    or t.id==1 or name.casefold().strip() in {'чат','general','общее','союзы'}):continue
            description=name
            if 'автоматизац' in name.casefold():description+=': скрипты, интеграции, боты, n8n, Make, сокращение рутины'
            if 'халяв' in name.casefold() or 'абуз' in name.casefold():description+=': бесплатные сервисы, акции, промокоды, бесплатные лимиты и тарифы'
            profile=profiles.get(t.id)
            if profile and profile['name']==name and profile.get('description'):
                description=profile['description']
            routes.append({'topic_id':t.id,'name':name,'description':description})
        return routes[:40]

    async def set_destination(self, destination, guard=lambda:None):
        entity,peer=await self.pub.target(destination,guard)
        routes=await self.routes(entity,guard)
        return self.save_destination(entity,peer,routes,guard)

    def save_destination(self, entity, peer, routes, guard):
        guard()
        self.store.execute('INSERT OR REPLACE INTO publication_destinations VALUES (?,?,?,?,?)',
            (self.store.settings()['owner_id'],peer,entity.title,json.dumps(routes,ensure_ascii=False),time.time()))
        return {'destination':str(peer),'name':entity.title,'routes':routes}

    def destination(self):
        rows=self.store.rows('SELECT * FROM publication_destinations WHERE owner_id=?',(self.store.settings()['owner_id'],))
        return rows[0] | {'routes':json.loads(rows[0]['routes'])} if rows else None

    def policy(self, peer):
        rows=self.store.rows('SELECT settings FROM publication_policies WHERE owner_id=? AND target_id=?',
                             (self.store.settings()['owner_id'],peer))
        return json.loads(rows[0]['settings']) if rows else {}

    async def command(self, args, guard, *, actor_id, chat_id=0):
        self.owner_guard(actor_id,guard)
        original_guard=guard
        guard=lambda:self.owner_guard(actor_id,original_guard)
        destination=args.get('destination') or (str(chat_id) if chat_id<0 else None)
        default=self.destination()
        destination=destination or (str(default['target_id']) if default else None)
        if args['action']=='monitoring_status':
            peer,name=None,''
            if default and destination==str(default['target_id']):
                peer,name=default['target_id'],default['name']
            elif destination:
                entity,peer=await self.pub.target(destination,guard)
                name=entity.title
            feeds=[f for f in self.pub.status()['feeds'] if peer is None or f['target_id']==peer]
            if not feeds:
                return {'text':'Для этой группы источники ещё не заданы.' if name else 'Источники мониторинга пока не заданы.'}
            return {'text':('Мониторинг для '+name+':\n' if name else 'Мониторинг:\n')+'\n'.join(
                ('Включён: ' if f['enabled'] else 'Остановлен: ')+f['name']+' · '+', '.join(s['value'] for s in f['config']['sources'])+
                ('\nОшибка: '+f['error'] if f['error'] else '') for f in feeds)}
        if not destination:raise ValueError('Укажи группу получателя: «мониторь @источник и публикуй в @группа по темам»')
        entity,peer=await self.pub.target(destination,guard)
        if args['action']=='stop_monitoring':
            source=await self.resolve_source(args['target'],guard) if args.get('target') else None
            value=str(utils.get_peer_id(source)) if source else None
            changed=0
            for feed in self.pub.status()['feeds']:
                if feed['target_id']!=peer:continue
                if value and not any(s['value']==value for s in feed['config']['sources']):continue
                self.owner_guard(actor_id,guard)
                await self.pub.execute({'action':'remove','feed_id':feed['id']},guard)
                changed+=1
            return {'text':f'Мониторинг остановлен: {changed} заданий.'}
        source=await self.resolve_source(args['target'],guard,join=args.get('join',False))
        if source is None:return {'text':'Заявка на вступление отправлена. После одобрения скажи «мониторь этот источник» с его ссылкой.'}
        source_peer=utils.get_peer_id(source)
        if source_peer==peer:raise ValueError('Источник и группа публикации должны различаться')
        routes=await self.routes(entity,guard)
        destination_routes=routes
        topic=args.get('topic_id',0)
        if args.get('topic_name'):
            matching=[r for r in routes if r['name'].casefold()==args['topic_name'].casefold().strip(' «»"')]
            if len(matching)!=1:raise ValueError('Не нашла такую тему. Укажи её точное название или ID')
            topic=matching[0]['topic_id']
        if topic:
            await self.pub.target(destination,guard,topic)
            topic_label=next((r['name'] for r in routes if r['topic_id']==topic),args.get('topic_name',''))
            routes=[]
        else:topic_label=''
        config={'name':'Материалы: '+source.title[:70],'topic':topic_label or 'Полезные новые материалы по темам форума',
            'destination':str(peer),'sources':[{'kind':'telegram_chat','value':str(source_peer)}],
            'routes':routes,'topic_id':topic,'include_media':True,'include_attribution':False,'new_only':True,'mode':'auto','enabled':False,
            'interval_minutes':5,'daily_limit':12,'max_chars':650,
            'instructions':'Короткий полезный пересказ, конкретная суть и условия. Без рекламных обещаний и лишней воды. Не публикуй ключи, токены и личные данные.'}
        config.update(self.policy(peer))
        config['enabled']=False
        existing=next((f for f in self.pub.status()['feeds'] if f['target_id']==peer and f['config']['sources']==config['sources']),None)
        saved=await self.pub.save(config,existing['id'] if existing else None,guard)
        # Establish the starting point before background work is enabled.
        cursor=self.store.rows('SELECT 1 FROM publication_source_cursors WHERE feed_id=? AND source=?',(saved['feed_id'],str(source_peer)))
        latest=await self.runtime.telegram.client.get_messages(source,limit=1) if not cursor else []
        self.owner_guard(actor_id,guard)
        self.store.execute('INSERT OR IGNORE INTO publication_source_cursors VALUES (?,?,?,?,?)',
            (saved['feed_id'],str(source_peer),source_peer,max((m.id for m in latest),default=0),time.time()))
        self.save_destination(entity,peer,destination_routes,guard)
        config['enabled']=True
        self.store.execute('UPDATE publication_feeds SET enabled=1,config=? WHERE id=?',(json.dumps(config,ensure_ascii=False),saved['feed_id']))
        self.store.update_settings({'publishing_enabled':True})
        names=', '.join(r['name'] for r in routes) if routes else args.get('topic_name') or ('тема '+str(topic) if topic else 'обычный чат')
        return {'text':'Мониторю новые сообщения «'+source.title+'». Короткие пересказы со ссылками, фото и файлами — в «'+entity.title+'», '+names+'. Проверка раз в '+str(config['interval_minutes'])+' минут, до '+str(config['daily_limit'])+' постов в день.', 'feed_id':saved['feed_id']}

    async def collect(self, feed, value, guard, *, archive_since=None, archive_query=None):
        entity=await self.resolve_source(value,guard)
        peer=utils.get_peer_id(entity)
        if peer==feed['target_id']:raise ValueError('Источник совпадает с получателем')
        rows=self.store.rows('SELECT * FROM publication_source_cursors WHERE feed_id=? AND source=?',(feed['id'],value))
        if archive_since is None and not rows and feed['config'].get('new_only'):
            latest=await self.runtime.telegram.client.get_messages(entity,limit=1)
            guard()
            self.store.execute('INSERT INTO publication_source_cursors VALUES (?,?,?,?,?)',
                (feed['id'],value,peer,max((m.id for m in latest),default=0),time.time()))
            return []
        last=rows[0]['last_id'] if rows else 0
        if archive_since is None:
            messages=await self.runtime.telegram.client.get_messages(entity,limit=100,min_id=last,reverse=bool(rows))
        else:
            messages=[];offset=0
            for _ in range(10):
                page=await self.runtime.telegram.client.get_messages(entity,limit=100,offset_id=offset,
                    **({'search':archive_query} if archive_query else {}))
                guard()
                if not page:break
                messages.extend(m for m in page if isinstance(m,types.Message) and m.date.timestamp()>=archive_since)
                oldest=min(m.id for m in page)
                if (offset and oldest>=offset) or any(m.date.timestamp()<archive_since for m in page) or len(page)<100:break
                offset=oldest
        guard()
        batches={}
        for m in messages:
            if (not isinstance(m,types.Message) or not safe_message(m) or m.noforwards or m.out
                    or not ((m.message or '').strip() or media_id(m))):continue
            batches.setdefault(m.grouped_id or -m.id,[]).append(m)
        items=[]
        for batch in batches.values():
            batch.sort(key=lambda m:m.id)
            text='\n'.join(dict.fromkeys(m.message.strip() for m in batch if m.message and m.message.strip()))
            files=[];links=[];references=[]
            for m in batch:
                for link_entity in m.entities or []:
                    if isinstance(link_entity,types.MessageEntityTextUrl):link_value=link_entity.url
                    elif isinstance(link_entity,types.MessageEntityUrl):
                        link_value=m.message.encode('utf-16-le')[link_entity.offset*2:(link_entity.offset+link_entity.length)*2].decode('utf-16-le')
                        link_value=link_value if '://' in link_value else 'https://'+link_value
                    else:continue
                    links.append(link_value)
                    if len(batch)==1:
                        encoded=m.message.encode('utf-16-le')
                        references.append({'url':link_value,
                            'start':len(encoded[:link_entity.offset*2].decode('utf-16-le')),
                            'end':len(encoded[:(link_entity.offset+link_entity.length)*2].decode('utf-16-le'))})
                if media_id(m):
                    files.append({'message_id':m.id,'media_id':media_id(m),'text_hash':hashlib.sha256((m.message or '').encode()).hexdigest()})
                    doc=getattr(m.media,'document',None)
                    if doc:
                        filename=next((a.file_name for a in doc.attributes if isinstance(a,types.DocumentAttributeFilename)),'')
                        files[-1].update({'filename':filename,'size':doc.size})
                        if filename:text+='\nФайл: '+filename
            if not text:continue
            root=batch[0].id
            url=f'https://t.me/{entity.username}/{root}' if getattr(entity,'username',None) else f'https://t.me/c/{entity.id}/{root}'
            parts=digest_parts(text,references) if len(batch)==1 and not files and feed['config'].get('routes') else []
            if parts:
                for part in parts:
                    items.append({'title':part['title'],'text':part['text'],'url':url,'published':batch[0].date.timestamp(),
                        'payload':{'peer_id':peer,'files':[],'links':part['links'],'part_id':part['part_id']},
                        'channel_id':peer,'message_id':root})
                continue
            items.append({'title':text.split('\n',1)[0][:250],'text':text[:6000],'url':url,'published':batch[0].date.timestamp(),
                'payload':{'peer_id':peer,'files':files[:10],'links':list(dict.fromkeys(links))},'channel_id':peer,'message_id':root})
        for item in items:
            if archive_since is not None:item['payload']['archive_since']=archive_since
            self.pub.remember(feed,value,item)
        guard()
        if archive_since is None:
            self.store.execute('INSERT OR REPLACE INTO publication_source_cursors VALUES (?,?,?,?,?)',
                (feed['id'],value,peer,max([last]+[m.id for m in messages]),time.time()))
        return items

    async def enrich_documents(self,feed,items,guard):
        from .publication_enrichment import needs_document
        attempted=0
        for item in items:
            payload=json.loads(item['payload'] or '{}')
            if (not needs_document(item) or payload.get('document_pages')
                    or payload.get('document_retry_after',0)>time.time()):continue
            if attempted>=3:break
            attempted+=1;pages=[]
            guard()
            try:
                if item['source'] not in {s['value'] for s in feed['config']['sources'] if s['kind']=='telegram_chat'}:
                    raise ValueError('Файл не относится к заданным источникам')
                async with asyncio.timeout(12):
                    source=await self.resolve_source(item['source'],guard)
                    if utils.get_peer_id(source)!=payload['peer_id']:raise ValueError('Источник файла изменился')
                    messages=await self.runtime.telegram.client.get_messages(source,ids=[f['message_id'] for f in payload['files']])
                    guard();known={m.id:m for m in messages if isinstance(m,types.Message)}
                    for file in payload['files']:
                        m=known.get(file['message_id'])
                        if (not m or not safe_message(m) or m.noforwards or m.out or media_id(m)!=file['media_id']
                                or hashlib.sha256((m.message or '').encode()).hexdigest()!=file['text_hash']):
                            raise ValueError('Исходный файл недоступен или изменён')
                        doc=getattr(m.media,'document',None)
                        if not doc:continue
                        filename=next((a.file_name for a in doc.attributes if isinstance(a,types.DocumentAttributeFilename)),'')
                        if not filename.casefold().endswith(('.txt','.md')) or doc.size>512*1024:continue
                        guard();raw=await self.runtime.telegram.client.download_media(m,file=bytes);guard()
                        if not isinstance(raw,bytes) or len(raw)>512*1024:continue
                        text=raw.decode('utf-16' if raw.startswith((b'\xff\xfe',b'\xfe\xff')) else 'utf-8-sig')
                        if '\x00' in text or not text.strip():continue
                        pages.append({'filename':filename[:180],'media_id':doc.id,'text':text.strip()[:6000]})
            except asyncio.CancelledError:raise
            except Exception:guard()
            guard()
            payload=payload|{'needs_document_text':True,'document_retry_after':time.time()+900}
            if pages:payload['document_pages']=pages
            encoded=json.dumps(payload,ensure_ascii=False)
            self.store.execute("UPDATE publication_items SET payload=? WHERE id=? AND status='new'",(encoded,item['id']))
            item['payload']=encoded

    async def attachments(self, post, guard):
        feed=self.pub.feed(post['feed_id'])
        if not feed['config'].get('include_media'):return []
        source_id=json.loads(post['source_ids'])[0]
        item=self.store.rows('SELECT * FROM publication_items WHERE id=? AND feed_id=?',(source_id,feed['id']))[0]
        payload=json.loads(item['payload'] or '{}')
        if not payload.get('files'):return []
        if item['source'] not in {s['value'] for s in feed['config']['sources'] if s['kind']=='telegram_chat'}:
            raise ValueError('Вложение не относится к заданным источникам')
        source=await self.resolve_source(item['source'],guard)
        if utils.get_peer_id(source)!=payload['peer_id']:raise ValueError('Источник вложения изменился')
        messages=await self.runtime.telegram.client.get_messages(source,ids=[f['message_id'] for f in payload['files']])
        guard()
        known={m.id:m for m in messages if isinstance(m,types.Message)}
        result=[]
        for file in payload['files']:
            m=known.get(file['message_id'])
            if (not m or not safe_message(m) or m.noforwards or media_id(m)!=file['media_id']
                    or hashlib.sha256((m.message or '').encode()).hexdigest()!=file['text_hash']):
                raise ValueError('Исходное вложение недоступно или изменено')
            result.append(getattr(m.media,'photo',None) or getattr(m.media,'document',None))
        return result
