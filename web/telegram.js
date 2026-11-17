const telegramPermissionLabels={
  telegram_autonomy_profile:'Менять свой профиль и приватность',
  telegram_autonomy_messages:'Писать и отправлять медиа',
  telegram_autonomy_calls:'Звонить знакомым',
  telegram_auto_answer_calls:'Принимать входящие звонки',
  telegram_autonomy_groups:'Действовать в группах и каналах',
  telegram_autonomy_stories:'Публиковать истории и реагировать',
  telegram_autonomy_organize:'Организовывать чаты и Избранное',
  telegram_autonomy_discovery:'Искать людей, стикеры и GIF',
  telegram_autonomy_new_contacts:'Начинать общение с найденными людьми',
  telegram_autonomy_destructive:'Удалять сообщения и исключать участников'
};
for(const [key,title] of Object.entries(telegramPermissionLabels)){
  const label=node('label',undefined,'rule-toggle');
  const input=document.createElement('input');input.type='checkbox';input.id='tg-permission-'+key;input.setAttribute('role','switch');
  label.append(node('span',title),input);$('tg-autonomy-permissions').append(label);
}
const telegramRunStates={planning:'Обдумывает',running:'Выполняет',complete:'Готово',skipped:'Решила отложить',failed:'Не удалось выполнить',uncertain:'Проверь результат в Telegram',interrupted:'Прервано',clarification:'Нужно уточнить'};
const telegramActionNames={profile:'Посмотрела профиль',search:'Поиск в Telegram',collect_media:'Собрала медиа',find_stickers:'Поиск стикеров',find_gifs:'Поиск GIF',send_sticker:'Отправила стикер',send_gif:'Отправила GIF',send_text:'Отправила сообщение',send_voice:'Отправила голосовое',call:'Исходящий звонок',accept_call:'Приём звонка',publish_story:'Публикация истории',profile_details:'Обновление профиля',profile_photo:'Новый аватар',save_note:'Запись в Избранное',folder:'Папка чатов',poll:'Опрос'};
let telegramPageLoading=false;
function telegramSettingsToForm(){
  const s=state.settings;
  $('tg-private-enabled').checked=!!s.private_initiative_enabled;
  for(const [field,key] of [['min','min_minutes'],['max','max_minutes'],['unanswered','max_unanswered'],['daily','daily_limit']])$('tg-private-'+field).value=s['private_initiative_'+key];
  $('tg-autonomy-enabled').checked=!!s.telegram_autonomy_enabled;
  $('tg-autonomy-minutes').value=s.telegram_autonomy_minutes;
  $('tg-autonomy-daily-limit').value=s.telegram_autonomy_daily_limit;
  $('tg-autonomy-max-actions').value=s.telegram_autonomy_max_actions;
  $('tg-call-cooldown').value=s.telegram_call_cooldown_hours;
  $('tg-autonomy-instructions').value=s.telegram_autonomy_instructions||'';
  for(const key of Object.keys(telegramPermissionLabels))$('tg-permission-'+key).checked=!!s[key];
  $('tg-stickers-enabled').checked=!!s.stickers_enabled;
  $('tg-gifs-enabled').checked=!!s.gifs_enabled;
  $('tg-collect-media').checked=!!s.collect_telegram_media;
}
async function loadTelegramPage(initial=false){
  if(telegramPageLoading)return;
  telegramPageLoading=true;
  try{
    const data=await api('/telegram/autonomy');
    $('tg-autonomy-state').textContent=(data.enabled?(data.working?'Выбирает занятие':'Самостоятельность включена'):'Самостоятельность выключена')+` · ${data.daily_used} из ${data.daily_limit} действий сегодня`;
    const privateState=data.private_initiative;
    $('tg-private-state').textContent=privateState?.enabled?`Личная инициатива включена · контактов: ${privateState.contacts.length} · ждёт ответа: ${privateState.contacts.filter(c=>!c.allowed).length}`:'Личная инициатива выключена';
    const journal=$('tg-action-journal');journal.replaceChildren();
    if(!data.runs.length)journal.append(node('p','Здесь появятся её первые решения','hint'));
    for(const run of data.runs){
      const row=node('div',undefined,'tg-journal-row');
      row.append(node('strong',telegramRunStates[run.status]||run.status),node('span',run.reason||'Занятие в Telegram','hint'),node('small',date(run.created),'hint'));
      const actions=data.actions.filter(a=>a.run_id===run.id);
      for(const action of actions){
        const status=({sent:'выполнено',read:'посмотрела',dispatch:'выполняется',planned:'в плане',failed:'ошибка',blocked:'отложено',uncertain:'результат не подтверждён'})[action.status]||action.status;
        row.append(node('span',(telegramActionNames[action.action]||'Действие в Telegram')+' · '+status,'hint'));
      }
      journal.append(row);
    }
    if(initial){
      telegramSettingsToForm();
      const list=await api('/contacts');
      $('tg-known-contacts').replaceChildren();
      for(const c of list){const option=document.createElement('option');option.value=c.username?'@'+c.username:String(c.id);option.label=c.name;$('tg-known-contacts').append(option);}
      const files=await api('/media');
      $('tg-action-file').replaceChildren(new Option('Выбрать из медиатеки',''));
      for(const f of files)if(/\.(jpe?g|png|webp|mp4)$/i.test(f.filename))$('tg-action-file').append(new Option(f.filename,f.filename));
      await telegramLoadCollection();
    }
  }finally{telegramPageLoading=false;}
}
$('tg-autonomy-form').addEventListener('submit',event=>{
  event.preventDefault();busy(event.submitter,async()=>{
    const values={telegram_autonomy_enabled:$('tg-autonomy-enabled').checked,telegram_autonomy_minutes:Number($('tg-autonomy-minutes').value),telegram_autonomy_daily_limit:Number($('tg-autonomy-daily-limit').value),telegram_autonomy_max_actions:Number($('tg-autonomy-max-actions').value),telegram_call_cooldown_hours:Number($('tg-call-cooldown').value),telegram_autonomy_instructions:$('tg-autonomy-instructions').value.trim()};
    values.private_initiative_enabled=$('tg-private-enabled').checked;
    for(const [field,key] of [['min','min_minutes'],['max','max_minutes'],['unanswered','max_unanswered'],['daily','daily_limit']])values['private_initiative_'+key]=Number($('tg-private-'+field).value);
    for(const key of Object.keys(telegramPermissionLabels))values[key]=$('tg-permission-'+key).checked;
    await api('/settings','PATCH',values);Object.assign(state.settings,values);await loadTelegramPage();toast('Самостоятельность сохранена');
  });
});
$('tg-media-settings').addEventListener('submit',event=>{
  event.preventDefault();busy(event.submitter,async()=>{
    const values={stickers_enabled:$('tg-stickers-enabled').checked,gifs_enabled:$('tg-gifs-enabled').checked,collect_telegram_media:$('tg-collect-media').checked};
    await api('/settings','PATCH',values);Object.assign(state.settings,values);toast('Поведение со стикерами и GIF сохранено');
  });
});
$('tg-refresh').addEventListener('click',()=>busy($('tg-refresh'),()=>loadTelegramPage(true)));
$('tg-autonomy-now').addEventListener('click',()=>busy($('tg-autonomy-now'),async()=>{
  const result=await api('/telegram/autonomy/check','POST',{});toast(result.accepted?'Дарья выбирает занятие':result.message);await loadTelegramPage();
}));
function telegramResultText(result){
  if(result.text)return result.text;
  if(result.first_name!==undefined)return [result.first_name,result.last_name,result.username?'@'+result.username:'',result.bio,result.status].filter(Boolean).join('\n');
  if(result.messages)return result.messages.map(m=>m.text||'[Медиа]').join('\n\n')||'Сообщений не найдено';
  if(result.users||result.chats)return [...(result.users||[]),...(result.chats||[])].map(c=>[c.name,c.username?'@'+c.username:''].filter(Boolean).join(' · ')).join('\n')||'Ничего не найдено';
  if(result.stories)return result.stories.map(s=>s.caption||'История без подписи').join('\n')||'Доступных историй нет';
  if(result.items?.some(i=>i.url))return result.items.map(i=>[i.title,i.snippet||i.text,i.url].filter(Boolean).join('\n')).join('\n\n');
  if(result.items)return `Найдено ${result.items.length} медиа`;
  if(result.connected!==undefined)return result.connected?'В звонке: '+result.name:'Сейчас вне звонка';
  if(result.sent)return 'Отправлено';
  if(result.saved)return 'Сохранено';
  if(result.changed||result.published||result.created)return 'Готово';
  return 'Данные получены';
}
async function telegramWaitRun(runId,target){
  target.textContent='Дарья выполняет действие…';
  for(let i=0;i<160;i++){
    const data=await api('/telegram/actions/'+runId);
    if(!['planning','running'].includes(data.run.status)){
      if(data.run.status==='complete'){
        target.textContent=data.actions.map(a=>{try{return telegramResultText(JSON.parse(a.result||'{}'));}catch{return 'Результат доступен в журнале';}}).join('\n\n')||'Готово';
      }else target.textContent=(telegramRunStates[data.run.status]||data.run.status)+': '+data.run.reason;
      await loadTelegramPage();return;
    }
    await new Promise(resolve=>setTimeout(resolve,1500));
  }
  target.textContent='Действие ещё выполняется. Результат появится в журнале.';
}
$('tg-command-form').addEventListener('submit',event=>{
  event.preventDefault();busy(event.submitter,async()=>{
    const run=await api('/telegram/command','POST',{text:$('tg-command').value.trim()});await telegramWaitRun(run.run_id,$('tg-command-result'));
  });
});
function telegramActionFields(){
  const action=$('tg-action-kind').value;
  $('tg-recipient-field').hidden=['hang_up','search','web_search','read_web','publish_story','save_note','profile_photo','profile_details','create_group','create_channel'].includes(action);
  $('tg-action-text-field').hidden=['profile','call','accept_call','hang_up','history','collect_media','stories','read_channel','profile_photo'].includes(action);
  $('tg-poll-field').hidden=action!=='poll';
  $('tg-file-field').hidden=!['publish_story','profile_photo'].includes(action);
}
$('tg-action-kind').addEventListener('change',telegramActionFields);telegramActionFields();
$('tg-action-form').addEventListener('submit',event=>{
  event.preventDefault();busy(event.submitter,async()=>{
    const action=$('tg-action-kind').value,data={action};
    const recipient=$('tg-recipient').value.trim(),text=$('tg-action-text').value.trim();
    if(!$('tg-recipient-field').hidden&&recipient)data.recipient=recipient;
    if(['search','web_search'].includes(action))data.query=text;
    else if(action==='read_web')data.url=text;
    else if(['create_group','create_channel'].includes(action))data.title=text;
    else if(!$('tg-action-text-field').hidden)data.text=text;
    if(action==='poll')data.options=$('tg-poll-options').value.split('\n').map(v=>v.trim()).filter(Boolean);
    if(['publish_story','profile_photo'].includes(action))data.filename=$('tg-action-file').value;
    const run=await api('/telegram/actions','POST',data);await telegramWaitRun(run.run_id,$('tg-action-result'));
  });
});
function telegramRenderMedia(items){
  const gallery=$('tg-media-gallery');gallery.replaceChildren();
  $('tg-media-status').textContent=items.length?`Найдено ${items.length}`:'Пока пусто. Найди медиа в Telegram или дождись новых стикеров и GIF в переписке.';
  for(const item of items){
    const card=node('div',undefined,'tg-media-card');
    const visual=node('div',item.kind==='gif'?'GIF':'✦','tg-media-visual');
    if(!item.asset_id.startsWith('gif-inline:')){
      const img=document.createElement('img');img.src='/api/telegram/media/'+encodeURIComponent(item.asset_id)+'/preview';img.alt=item.label;img.loading='lazy';
      img.addEventListener('error',()=>img.remove());visual.replaceChildren(img);visual.append(node('small',item.kind==='gif'?'GIF':'Стикер'));
    }
    card.append(visual,node('span',item.label,'tg-media-label'));
    const buttons=node('div',undefined,'tg-media-buttons');
    const send=node('button','Отправить','small-button');send.type='button';send.addEventListener('click',()=>busy(send,async()=>{
      const recipient=$('tg-media-recipient').value.trim();if(!recipient)throw new Error('Выбери, кому отправить');
      const run=await api('/telegram/actions','POST',{action:item.kind==='gif'?'send_gif':'send_sticker',recipient,asset_id:item.asset_id});await telegramWaitRun(run.run_id,$('tg-media-status'));
    }));
    buttons.append(send);
    if(!item.asset_id.startsWith('gif-inline:')){
      const save=node('button','В избранное','text-button');save.type='button';save.addEventListener('click',()=>busy(save,async()=>{
        const run=await api('/telegram/actions','POST',{action:'save_media',asset_id:item.asset_id});await telegramWaitRun(run.run_id,$('tg-media-status'));
      }));buttons.append(save);
    }
    card.append(buttons);gallery.append(card);
  }
}
async function telegramLoadCollection(){
  const data=await api('/telegram/media?kind='+$('tg-media-kind').value+'&q='+encodeURIComponent($('tg-media-query').value.trim()));telegramRenderMedia(data.items);
}
$('tg-media-collection').addEventListener('click',()=>busy($('tg-media-collection'),telegramLoadCollection));
$('tg-media-kind').addEventListener('change',()=>busy($('tg-media-collection'),telegramLoadCollection));
$('tg-media-search').addEventListener('submit',event=>{
  event.preventDefault();busy(event.submitter,async()=>{
    $('tg-media-status').textContent='Ищу в Telegram…';
    const data=await api('/telegram/media/search','POST',{kind:$('tg-media-kind').value,query:$('tg-media-query').value.trim()});telegramRenderMedia(data.items);
  });
});
