let publicationFeedEditing=null;
let publicationData=null;
function pubLines(id){return $(id).value.split('\n').map(v=>v.trim()).filter(Boolean);}
function pubConfig(){
 const sources=[];
 for(const [id,kind] of [['pub-channels','telegram_chat'],['pub-searches','search'],['pub-web','web'],['pub-rss','rss']])for(const value of pubLines(id))sources.push({kind:publicationFeedEditing?.config.sources.find(s=>s.value===value)?.kind||kind,value});
 if(!sources.length)throw new Error('Добавь хотя бы один канал, сайт, RSS или поисковый запрос');
 return {name:$('pub-name').value.trim(),topic:$('pub-topic').value.trim(),destination:$('pub-destination').value.trim(),sources,
  enabled:$('pub-feed-enabled').checked,mode:$('pub-mode').value,interval_minutes:Number($('pub-interval').value),
  daily_limit:Number($('pub-daily').value),topic_id:Number($('pub-topic-id').value||0),instructions:$('pub-instructions').value.trim(),max_chars:publicationFeedEditing?.config.max_chars||2500,
  routes:Number($('pub-topic-id').value||0)?[]:publicationFeedEditing?.config.routes||[],include_media:publicationFeedEditing?.config.include_media||false,
  include_attribution:publicationFeedEditing?.config.include_attribution??false,new_only:publicationFeedEditing?.config.new_only||false};
}
function pubEdit(feed){
 publicationFeedEditing=feed;$('pub-form-title').textContent='Правки задания';$('pub-cancel-edit').hidden=false;
 const c=feed.config;
 for(const [id,key] of [['pub-name','name'],['pub-topic','topic'],['pub-destination','destination'],['pub-instructions','instructions'],['pub-mode','mode'],['pub-interval','interval_minutes'],['pub-daily','daily_limit'],['pub-topic-id','topic_id']])$(id).value=c[key];
 for(const [id,kind] of [['pub-channels','telegram'],['pub-searches','search'],['pub-web','web'],['pub-rss','rss']])$(id).value=c.sources.filter(s=>s.kind===kind||(kind==='telegram'&&s.kind==='telegram_chat')).map(s=>s.value).join('\n');
 $('pub-feed-enabled').checked=!!feed.enabled;$('pub-feed-form').scrollIntoView({behavior:'smooth'});
}
function pubReset(){publicationFeedEditing=null;$('pub-feed-form').reset();$('pub-form-title').textContent='Новое задание Даше';$('pub-cancel-edit').hidden=true;}
function pubButton(text,operation){const b=node('button',text,'small-button');b.type='button';b.addEventListener('click',()=>busy(b,operation));return b;}
async function loadPublications(initial=false){
 const d=await api('/publications');publicationData=d;
 if(initial)$('pub-enabled').checked=!!d.enabled;
 $('pub-status').textContent=(d.enabled?'Мониторинг включён':'Мониторинг выключен')+` · заданий: ${d.feeds.filter(f=>f.enabled).length} · черновиков: ${d.posts.filter(p=>p.status==='draft').length}`;
 if(d.destination)$('pub-status').textContent+=' · группа по умолчанию: '+d.destination.name;
 const feeds=$('pub-feeds');feeds.replaceChildren();
 if(!d.feeds.length)feeds.append(node('p','Добавь тему, источники и канал или группу — Даша начнёт следить за новыми материалами.','panel form-body hint'));
 for(const f of d.feeds){
  const card=node('div',undefined,'panel form-body pub-feed-card');
  card.append(node('h3',f.name),node('p',f.config.topic),node('span',`${f.config.destination} · ${f.config.mode==='auto'?'публикует сама':'готовит черновики'} · ${f.config.sources.length} источников`,'hint'),node('span',f.working?'Проверяет источники…':f.enabled?'Следующая проверка: '+date(f.next_check):'Задание остановлено','hint'));
  if(f.config.routes?.length)card.append(node('p','Темы: '+f.config.routes.map(r=>r.name).join(', '),'hint'));
  if(f.error)card.append(node('p',f.error,'pub-error'));
  const buttons=node('div',undefined,'pub-actions');
  buttons.append(pubButton('Изменить',async()=>pubEdit(f)),pubButton('Проверить сейчас',async()=>{const r=await api('/publications/check','POST',{feed_id:f.id});toast(r.accepted?'Даша проверяет источники':r.message);await loadPublications();}));
  if(f.enabled)buttons.append(pubButton('Остановить',async()=>{await api('/publications/feeds/'+f.id,'DELETE');await loadPublications();toast('Задание остановлено');}));
  card.append(buttons);feeds.append(card);
 }
 const posts=$('pub-posts');posts.replaceChildren();
 if(!d.posts.length)posts.append(node('p','Здесь появятся подготовленные и опубликованные посты.','panel form-body hint'));
 const states={draft:'Черновик',dispatch:'Публикуется',sent:'Опубликовано',uncertain:'Проверь доставку в Telegram',failed:'Не опубликовано',discarded:'Убран'};
 for(const p of d.posts){
  const card=node('article',undefined,'panel form-body pub-post');
  card.append(node('h3',states[p.status]||p.status),node('span',date(p.created),'hint'));
  if(p.topic_id)card.append(node('span','Тема: '+(d.feeds.find(f=>f.id===p.feed_id)?.config.routes?.find(r=>r.topic_id===p.topic_id)?.name||p.topic_id),'hint'));
  if(p.error)card.append(node('p',p.error,'pub-error'));
  const text=node('div',p.text,'pub-post-text');card.append(text);
  const sources=node('div',undefined,'pub-sources');
  for(const s of p.sources){const a=node('a',s.title||s.url);a.href=s.url;a.target='_blank';a.rel='noopener noreferrer';sources.append(a);}
  card.append(sources);
  const buttons=node('div',undefined,'pub-actions');
  if(p.status==='draft'){
   buttons.append(pubButton('Опубликовать',async()=>{await api('/publications/publish','POST',{post_id:p.id});toast('Пост отправляется в Telegram');await loadPublications();}));
   buttons.append(pubButton('Править',async()=>{
    text.hidden=true;const edit=document.createElement('textarea');edit.value=p.text;edit.rows=8;edit.maxLength=4096;card.insertBefore(edit,sources);
    buttons.replaceChildren(pubButton('Сохранить правки',async()=>{await api('/publications/posts/'+p.id,'PATCH',{text:edit.value});await loadPublications();toast('Черновик сохранён');}),pubButton('Отмена',()=>loadPublications()));
   }));
  }
  if(['draft','uncertain'].includes(p.status))buttons.append(pubButton(p.status==='uncertain'?'Убрать без повторной отправки':'Убрать',async()=>{await api('/publications/discard','POST',{post_id:p.id});await loadPublications();}));
  card.append(buttons);posts.append(card);
 }
}
$('pub-feed-form').addEventListener('submit',event=>{event.preventDefault();busy(event.submitter,async()=>{
 const c=pubConfig();await api('/publications/feeds'+(publicationFeedEditing?'/'+publicationFeedEditing.id:''),publicationFeedEditing?'PATCH':'POST',c);
 pubReset();await loadPublications();toast('Задание сохранено');
});});
$('pub-settings').addEventListener('submit',event=>{event.preventDefault();busy(event.submitter,async()=>{const values={publishing_enabled:$('pub-enabled').checked};await api('/settings','PATCH',values);Object.assign(state.settings,values);await loadPublications();toast('Публикации настроены');});});
$('pub-cancel-edit').addEventListener('click',pubReset);
$('pub-refresh').addEventListener('click',()=>busy($('pub-refresh'),()=>loadPublications(true)));
