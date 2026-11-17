const $ = (id) => document.getElementById(id);
let currentPage = 'character', state, contacts = [], selectedContact = null, previewHistory = [], telegramEditing = false, showArchive = false;
let toastTimer, refreshRunning = false;
let websiteJobId = null;
const titles = {character:'Характер и голос',behavior:'Поведение и фото',websites:'Сайты и объявления',contacts:'Диалоги',memory:'Память',tasks:'Её планы',connections:'Подключения',events:'Журнал действий',access:'Доступ',gifts:'Подарки',hermes:'Hermes и задачи',media:'Медиатека'};
const behaviorChecks=['natural_enabled','split_replies','reactions_enabled','presence_enabled','vision_enabled','group_enabled'];
const behaviorNumbers=['typing_cps','read_delay_min','read_delay_max','reply_delay_min','reply_delay_max','burst_window','presence_interval_min','presence_interval_max','presence_duration_min','presence_duration_max'];
titles.telegram='Telegram и инициатива';
titles.publications='Источники и посты';
function behaviorInput(key){return $(key.replaceAll('_','-'));}
async function saveBehavior(){
  const values={};
  for(const key of behaviorChecks)values[key]=behaviorInput(key).checked;
  for(const key of behaviorNumbers)values[key]=Number(behaviorInput(key).value);
  values.vision_model=$('vision-model').value.trim();values.group_aliases=$('group-aliases').value.trim();
  await api('/settings','PATCH',values);Object.assign(state.settings,values);
}

function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = text;
  if (className) element.className = className;
  return element;
}
function toast(text, error = false) {
  clearTimeout(toastTimer);
  $('toast').textContent = text;
  $('toast').classList.toggle('error', error);
  $('toast').hidden = false;
  toastTimer = setTimeout(() => $('toast').hidden = true, error ? 8000 : 4000);
}
async function api(path, method = 'GET', body) {
  const response = await fetch('/api' + path, {
    method, credentials:'same-origin', headers: {'Content-Type':'application/json'},
    ...(method !== 'GET' ? {body:JSON.stringify(body ?? {})} : {})
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    if (response.status === 401 && path !== '/login') showLogin();
    const detail = typeof data.detail === 'string' ? data.detail : 'Проверь заполненные поля';
    throw new Error(detail || 'Не удалось выполнить действие');
  }
  return data;
}
async function busy(button, work) {
  if (button?.disabled) return;
  const controls = button?.form ? [...button.form.querySelectorAll('button')] : [button].filter(Boolean);
  controls.forEach(control => control.disabled = true);
  if (button) button.setAttribute('aria-busy', 'true');
  try { return await work(); }
  catch (error) { toast(error.message || 'Сервер не ответил', true); }
  finally { controls.forEach(control => control.disabled = false); button?.removeAttribute('aria-busy'); if(button?.form === $('telegram-code-form'))updateResendButton(); }
}
function showLogin() { $('login-screen').hidden=false; $('app-shell').hidden=true; }
function showApp() { $('login-screen').hidden=true; $('app-shell').hidden=false; }
function date(value) { return new Date(value * 1000).toLocaleString('ru-RU', {day:'numeric',month:'short',hour:'2-digit',minute:'2-digit'}); }
function bubble(text, role, created) {
  const item = node('div', undefined, 'bubble ' + role);
  item.append(node('span', text));
  if (created) item.append(node('small', date(created), 'bubble-time'));
  return item;
}
async function page(name) {
  currentPage = name;
  document.querySelectorAll('.page').forEach(el => el.classList.toggle('active', el.id === 'page-' + name));
  document.querySelectorAll('[data-page]').forEach(el => {el.classList.toggle('active', el.dataset.page === name);el.setAttribute('aria-current', el.dataset.page === name ? 'page' : 'false');});
  $('breadcrumb').textContent = titles[name];
  $('sidebar').classList.remove('open');
  try {
    if (name === 'contacts' || name === 'memory' || name === 'tasks') await loadContacts();
    if (name === 'tasks') await loadTasks();
    if (name === 'events') await loadEvents();
    if (name === 'memory') await loadMemory();
    if (name === 'gifts') {await loadContacts();await loadGifts();}
    if (name === 'hermes') await loadHermes();
    if (name === 'websites') await loadWebsiteJob(true);
    if (name === 'media') {await loadContacts();await loadMedia();}
    if (name === 'telegram') await loadTelegramPage(true);
    if (name === 'publications') await loadPublications(true);
  } catch(error) { toast(error.message, true); }
}
document.querySelectorAll('[data-page]').forEach(button => button.addEventListener('click', () => page(button.dataset.page)));
$('menu').addEventListener('click', () => $('sidebar').classList.toggle('open'));
$('login-form').addEventListener('submit', async event => {
  event.preventDefault();
  $('login-error').textContent = '';
  const button = event.submitter;
  button.disabled = true;
  try {
    await api('/login','POST',{password:$('login-password').value});
    $('login-password').value = '';
    await initialize();
  } catch(error) { $('login-error').textContent = error.message; }
  finally { button.disabled = false; }
});
$('logout').addEventListener('click', () => busy($('logout'), async () => {await api('/logout','POST');showLogin();}));

function applyStatus(initial = false) {
  const running = state.settings.enabled;
  $('enabled').checked = running;
  $('run-caption').textContent = running ? 'Дарья включена' : 'Дарья на паузе';
  $('run-label').textContent = running ? 'Готова к разговору' : 'На паузе';
  $('status-dot').classList.toggle('running', running && state.telegram.connected);
  $('side-state').textContent = state.telegram.connected ? state.telegram.name : 'Пока не подключена';
  $('nav-contact-count').textContent = state.counts.contacts;
  $('nav-task-count').textContent = state.counts.pending;
  $('footer-counts').textContent = `${state.counts.messages} сообщений · ${state.counts.memories} воспоминаний`;
  $('ai-state').textContent = state.ai.configured ? 'Ключ добавлен' : 'Не подключён';
  $('ai-key-hint').textContent = state.ai.configured ? 'Ключ уже сохранён. Оставь поле пустым, чтобы сохранить его' : 'Ключ хранится на сервере и не возвращается в браузер';
  const tg = telegramEditing ? 'disconnected' : state.telegram.state;
  $('tg-state').textContent = ({connected:'Подключён',code:'Ожидает код',password:'Проверка пароля',qr:'Вход по QR-коду'})[tg] || 'Не подключён';
  $('telegram-connected').hidden = tg !== 'connected';
  $('telegram-form').hidden = ['connected','code','password','qr'].includes(tg);
  $('telegram-code-form').hidden = tg !== 'code';
  $('telegram-password-form').hidden = tg !== 'password';
  $('telegram-qr').hidden = tg !== 'qr';
  $('telegram-name').textContent = state.telegram.name;
  const guard = state.telegram.session_guard || {};
  const guardLabel = $('telegram-session-guard');
  guardLabel.hidden = !guard.state || guard.state === 'unconfigured';
  guardLabel.textContent = guard.state === 'protected'
    ? `Защита входов включена. Доверенных сессий: ${guard.trusted_count}. Новые посторонние сессии завершаются автоматически.`
    : guard.error || 'Защита входов ожидает подключения Telegram.';
  $('telegram-api-hash-hint').textContent = state.telegram.has_credentials ? 'API Hash уже сохранён. Оставь поле пустым, если API ID тот же.' : 'Получается вместе с API ID на my.telegram.org/apps';
  const login = state.telegram.login || {};
  const deliveryHints = {
    telegram:'Telegram отправил код в служебный чат «Telegram» на подключённых устройствах этого аккаунта.',
    sms:'Telegram выбрал SMS. Проверь сообщения на телефоне с этим номером.',
    call:'Telegram выбрал телефонный звонок. Код продиктуют при звонке.',
    flash_call:'Telegram выбрал короткий звонок. Код — номер входящего звонка.',
    missed_call:'Telegram выбрал пропущенный звонок. Введи последние цифры номера звонка.',
    email:`Telegram выбрал email${login.email_hint ? ': '+login.email_hint : ''}. Проверь почту и папку «Спам».`,
    sms_word:'Telegram выбрал SMS со словом. Введи слово из сообщения.',
    sms_phrase:'Telegram выбрал SMS с фразой. Введи фразу из сообщения.',
    fragment:'Telegram выбрал доставку через Fragment. Проверь сообщения для своего номера на Fragment.',
  };
  $('telegram-code-hint').textContent = (deliveryHints[login.channel] || 'Telegram принял запрос кода. Проверь служебный чат Telegram и SMS.') + (login.phone_hint ? ` Номер: ${login.phone_hint}.` : '') + ' Код для my.telegram.org здесь не подходит.';
  $('telegram-code').inputMode = ['sms_word','sms_phrase'].includes(login.channel) ? 'text' : 'numeric';
  updateResendButton();
  updateQrView();
  if (initial) {
    const s = state.settings;
    $('persona').value = s.persona;
    $('voice-persona').value = s.voice_persona;
    $('proactive-enabled').checked = s.proactive_enabled;
    ['proactive-hours','quiet-start','quiet-end','timezone','hourly-limit','daily-limit','min-interval'].forEach((id, i) => $(id).value = s[['proactive_hours','quiet_start','quiet_end','timezone','hourly_limit','daily_contact_limit','min_interval'][i]]);
    $('ai-base-url').value = s.ai_base_url;
    $('ai-model').value = s.ai_model;
    $('image-enabled').checked = s.image_generation_enabled !== false;
    $('image-base-url').value = s.image_generation_base_url || '';
    $('image-model').value = s.image_generation_model || '';
    $('image-api-mode').value = s.image_generation_api_mode || 'images';
    $('ai-provider').value = s.ai_base_url.includes('deepseek.com') ? 'deepseek' : s.ai_base_url.includes('openrouter.ai') ? 'openrouter' : 'custom';
    $('telegram-api-id').value = state.telegram.api_id || '';
    updateCharacterCount();
    updateVoiceCharacterCount();
    populateModels(s.ai_models||[]);
    $('gifts-enabled').checked=s.gifts_enabled;
    $('gift-autonomous-enabled').checked=!!s.gift_autonomous_enabled;
    $('gift-max-price').value=s.gift_max_price;
    $('gift-monthly-budget').value=s.gift_monthly_budget;
    $('gift-interval').value=s.gift_interval_hours;
    $('gift-check').value=s.gift_check_hours;
    $('hermes-enabled').checked=s.hermes_enabled;
    $('brightdata-enabled').checked=!!s.brightdata_enabled;
    $('owner-id').value=s.owner_id;
    $('agent-rounds').value=s.agent_max_rounds;
    for(const key of behaviorChecks)behaviorInput(key).checked=!!s[key];
    for(const key of behaviorNumbers)behaviorInput(key).value=s[key];
    $('vision-model').value=s.vision_model||'';$('group-aliases').value=s.group_aliases||'';
  }
  updateGiftPolicyStatus();
  $('image-state').textContent = !state.image_generation?.enabled ? 'Выключена' : state.image_generation?.configured ? 'Подключена' : 'Впиши модель и сохрани подключение';
}
async function refresh() {
  if (refreshRunning || $('app-shell').hidden || document.hidden) return;
  refreshRunning = true;
  try {
    state = await api('/status'); applyStatus();
    if (currentPage === 'contacts') {await loadContacts();if (selectedContact) await loadConversation(true);}
    if (currentPage === 'tasks') await loadTasks();
    if (currentPage === 'hermes') await loadHermes();
    if (currentPage === 'websites') await loadWebsiteJob();
    if (currentPage === 'telegram') await loadTelegramPage(false);
    if (currentPage === 'publications') await loadPublications(false);
  } catch(error) { /* Explicit actions show errors; polling stays quiet during reconnects. */ }
  finally {refreshRunning = false;}
}
async function initialize() { state=await api('/status');applyStatus(true);await loadContacts();showApp(); }
$('enabled').addEventListener('change', async () => {
  const enabled = $('enabled').checked;
  $('enabled').disabled = true;
  try {await api('/settings','PATCH',{enabled});state.settings.enabled=enabled;applyStatus();toast(enabled?'Дарья включена':'Дарья на паузе');}
  catch(error){$('enabled').checked=!enabled;toast(error.message,true);}
  finally{$('enabled').disabled=false;}
});
function updateCharacterCount() {$('persona-length').textContent=`${$('persona').value.length.toLocaleString('ru-RU')} символов`;}
$('persona').addEventListener('input', () => {updateCharacterCount();$('character-state').textContent='Есть изменения';});
function updateVoiceCharacterCount() {$('voice-persona-length').textContent=`${$('voice-persona').value.length.toLocaleString('ru-RU')} символов`;}
$('voice-persona').addEventListener('input', () => {updateVoiceCharacterCount();$('voice-character-state').textContent='Есть изменения';});
$('voice-character-form').addEventListener('submit', event => {
  event.preventDefault();busy(event.submitter,async()=>{
    const voice_persona=$('voice-persona').value;
    await api('/settings','PATCH',{voice_persona});
    state.settings.voice_persona=voice_persona;$('voice-character-state').textContent='Сохранено';toast('Голосовой характер сохранён');
  });
});
$('behavior-form').addEventListener('submit',event=>{event.preventDefault();busy(event.submitter,async()=>{await saveBehavior();toast('Поведение сохранено');});});
$('test-vision').addEventListener('click',()=>busy($('test-vision'),async()=>{
  await saveBehavior();$('vision-test-state').textContent='Рассматривает тестовое изображение…';
  try{const result=await api('/ai/vision-test','POST');$('vision-test-state').textContent='Фото распознано: '+result.answer;toast('Дарья видит изображения');}
  catch(error){$('vision-test-state').textContent=error.message;throw error;}
}));
function websiteValues(){return {url:$('website-url').value.trim(),max_items:Number($('website-max').value),pages:Number($('website-pages').value)};}
function renderWebsite(data){
  $('website-state').textContent=data.blocked?data.error:`Прочитано ${data.pages} стр. · найдено ${data.items.length} записей`;
  const results=$('website-results');results.replaceChildren();results.hidden=false;
  results.append(node('h2',data.title||'Результат'));
  if(!data.items.length)results.append(node('p',data.blocked?data.error:data.text||'На странице не найдено объявлений'));
  for(const item of data.items){const row=node('div',undefined,'website-item');const link=node('a',item.title);link.href=item.url;link.target='_blank';link.rel='noopener noreferrer';row.append(link,node('span',[item.price,item.location].filter(Boolean).join(' · '),'hint'));if(item.description)row.append(node('p',item.description));results.append(row);}
  $('website-download').hidden=!data.filename;
  if(data.filename)$('website-download').href=`/api/media/${encodeURIComponent(data.filename)}/download`;
  if(data.warning)results.append(node('p',data.warning,'hint'));
}
async function loadWebsiteJob(restore=false){
  if(!websiteJobId&&!restore)return;
  const jobs=await api('/websites/jobs');
  const job=websiteJobId?jobs.find(item=>item.id===websiteJobId):jobs[0];
  if(!job)return;
  websiteJobId=job.id;
  if(job.status==='completed'&&job.data)renderWebsite(job.data);
  else if(['failed','interrupted'].includes(job.status)){
    $('website-state').textContent=job.result||'Сбор прерван';$('website-download').hidden=true;
  }else{
    $('website-state').textContent=`Собирает объявления · задача ${job.id}. Обычно это занимает 1–3 минуты. Результат появится здесь автоматически.`;
    $('website-download').hidden=true;
  }
}
$('brightdata-enabled').addEventListener('change',async()=>{
  const input=$('brightdata-enabled'),value=input.checked;input.disabled=true;
  try{await api('/settings','PATCH',{brightdata_enabled:value});state.settings.brightdata_enabled=value;toast(value?'Bright Data включён':'Bright Data выключен');}
  catch(error){input.checked=!value;toast(error.message,true);}
  finally{input.disabled=false;}
});
$('website-form').addEventListener('submit',event=>{event.preventDefault();busy(event.submitter,async()=>{
  $('website-state').textContent='Читает страницу…';$('website-download').hidden=true;
  try{
    const data=await api('/websites/parse','POST',websiteValues());
    if(data.job_id){websiteJobId=data.job_id;$('website-results').hidden=true;await loadWebsiteJob();}
    else{websiteJobId=null;renderWebsite(data);}
  }catch(error){$('website-state').textContent=error.message;throw error;}
});});
$('website-hermes').addEventListener('click',()=>busy($('website-hermes'),async()=>{
  if(!$('website-form').reportValidity())return;
  const result=await api('/websites/hermes','POST',websiteValues());$('website-state').textContent=`Hermes выполняет задание ${result.job_id}. Результат появится в разделе Hermes и задачи.`;toast('Сайт передан Hermes');
}));
$('save-character').addEventListener('click', () => busy($('save-character'), async () => {
  await api('/settings','PATCH',{persona:$('persona').value});
  state.settings.persona=$('persona').value;$('character-state').textContent='Сохранено';toast('Характер сохранён');
}));
const tones = {
  warm:'\nТон: особенно тёплый и заботливый. Замечай настроение собеседника, слушай и поддерживай без навязчивости.',
  playful:'\nТон: лёгкий и игривый. Иногда мягко подшучивай, предлагай темы для разговора и делись интересом к аниме.',
  calm:'\nТон: спокойный и мягкий. Короткие сообщения без суеты, давления и обилия смайлов.'
};
document.querySelectorAll('[data-tone]').forEach(button => button.addEventListener('click', () => {
  $('persona').value=$('persona').value.replace(/\nТон:.*$/s,'')+tones[button.dataset.tone];
  $('persona').dispatchEvent(new Event('input'));
}));
$('autonomy-form').addEventListener('submit', event => {
  event.preventDefault();busy(event.submitter,async()=>{
    const settings={proactive_enabled:$('proactive-enabled').checked,proactive_hours:+$('proactive-hours').value,
      quiet_start:+$('quiet-start').value,quiet_end:+$('quiet-end').value,timezone:$('timezone').value.trim(),
      hourly_limit:+$('hourly-limit').value,daily_contact_limit:+$('daily-limit').value,min_interval:+$('min-interval').value};
    await api('/settings','PATCH',settings);Object.assign(state.settings,settings);toast('Правила сохранены');
  });
});
function clearPreview(){previewHistory=[];$('preview-chat').replaceChildren(node('div','Начни новый пробный диалог','empty-state'));}
$('clear-preview').addEventListener('click',clearPreview);
$('preview-mode').addEventListener('change',clearPreview);
$('preview-form').addEventListener('submit',event=>{
  event.preventDefault();busy(event.submitter,async()=>{
    if(!state.ai.configured)throw new Error('Сначала добавь API-ключ в подключениях');
    const mode=$('preview-mode').value;
    if(mode==='text' ? $('persona').value!==state.settings.persona : $('voice-persona').value!==state.settings.voice_persona)throw new Error('Сначала сохрани изменения выбранного характера');
    const text=$('preview-input').value.trim();if(!text)return;
    const chat=$('preview-chat');chat.querySelector('.preview-empty')?.remove();chat.querySelector('.empty-state')?.remove();
    chat.append(bubble(text,'user'));$('preview-input').value='';chat.scrollTop=chat.scrollHeight;
    const pending=node('div','Дарья думает…','hint');chat.append(pending);
    $('preview-mode').disabled=true;$('clear-preview').disabled=true;
    try{
      const result=await api('/preview','POST',{text,history:previewHistory.slice(-16),mode});
      previewHistory.push({role:'user',content:text},{role:'assistant',content:result.text});
      chat.append(bubble(result.text,'assistant'));
    }catch(error){chat.append(node('div',error.message,'inline-error'));throw error;}
    finally{pending.remove();chat.scrollTop=chat.scrollHeight;$('preview-mode').disabled=false;$('clear-preview').disabled=false;}
  });
});

async function loadContacts(){
  contacts=await api('/contacts');
  const list=$('contact-list');list.replaceChildren();
  if(!contacts.length)list.append(node('div','Подключи Telegram и добавь первого собеседника','empty-state'));
  for(const contact of contacts){
    const button=node('button',undefined,'contact-row');button.classList.toggle('selected',contact.id===selectedContact);
    button.append(node('span',(contact.name||'д')[0].toLowerCase(),'avatar'));
    const copy=node('div');copy.append(node('span',contact.name,'contact-name'),node('small',contact.kind==='group'?'Группа · по обращению':contact.proactive?'Может писать первой':'Только отвечает'));
    button.append(copy);button.addEventListener('click',()=>busy(button,async()=>{selectedContact=contact.id;await loadConversation();await loadContacts();}));list.append(button);
  }
  for(const id of ['memory-contact','task-contact','media-contact']){
    const select=$(id), selected=select.value;select.replaceChildren(new Option('Выбрать собеседника',''));
    for(const contact of contacts)select.add(new Option(contact.name,String(contact.id)));
    select.value=selected;
  }
}
async function loadConversation(polling=false){
  if(!selectedContact)return;
  const data=await api(`/contacts/${selectedContact}/history?archive=${showArchive}`);
  $('conversation-heading').replaceChildren(node('h2',data.contact.name),node('span',data.contact.kind==='group'?'Групповой чат':data.contact.username?'@'+data.contact.username:'Личный диалог','hint'));
  const archiveButton=node('button',showArchive?'Вернуться к текущему диалогу':'Архив','text-button');
  archiveButton.addEventListener('click',()=>busy(archiveButton,async()=>{showArchive=!showArchive;await loadConversation();}));
  $('conversation-heading').append(archiveButton);
  const chat=$('conversation');const wasBottom=chat.scrollHeight-chat.scrollTop-chat.clientHeight<60;
  chat.replaceChildren();
  if(!data.messages.length)chat.append(node('div','Разговор ещё не начался','empty-state'));
  data.messages.forEach(message=>{
    const item=bubble(message.text,message.role,message.created);
    if(data.contact.kind==='group'&&message.role==='user'&&message.sender_name)item.prepend(node('small',message.sender_name,'bubble-author'));
    if(message.has_image){const image=node('img');image.src=`/api/contacts/${selectedContact}/photos/${message.id}`;image.alt='Фото из сообщения';image.loading='lazy';image.className='message-photo';item.prepend(image);}
    if(message.archived)item.prepend(node('small','Архив · не используется в новых ответах','hint'));
    chat.append(item);
  });
  if(wasBottom||!data.messages.length)chat.scrollTop=chat.scrollHeight;
  for(const saved of data.archived_profiles||[]){
    const profile=JSON.parse(saved.profile);const details=Object.entries(profile).filter(([,value])=>value).map(([key,value])=>`${key}: ${value}`).join('\n');
    if(details)chat.prepend(bubble('Архив профиля\n'+details,'assistant',saved.created));
  }
  $('contact-controls').hidden=showArchive;
  $('contact-enabled').checked=!!data.contact.enabled;$('contact-proactive').checked=!!data.contact.proactive;
  if(!polling){$('contact-birthday').value=data.contact.birthday||'';$('contact-notes').value=data.contact.notes||'';
  $('contact-persona').value=data.contact.persona||'';$('contact-hermes').checked=!!data.contact.hermes_allowed;$('contact-calls').checked=data.contact.calls_allowed!==0;}
}
for(const id of ['contact-enabled','contact-proactive'])$(id).addEventListener('change',async()=>{
  const enabled=$('contact-enabled').checked,proactive=$('contact-proactive').checked;
  try{await api(`/contacts/${selectedContact}`,'PATCH',{enabled,proactive});await loadContacts();toast('Настройки собеседника сохранены');}
  catch(error){toast(error.message,true);await loadConversation();}
});
$('message-form').addEventListener('submit',event=>{event.preventDefault();busy(event.submitter,async()=>{
  const text=$('message-input').value.trim();if(!text)return;
  await api(`/contacts/${selectedContact}/send`,'POST',{text});$('message-input').value='';await loadConversation();toast('Сообщение отправлено');
});});
$('initiative').addEventListener('click',()=>busy($('initiative'),async()=>{await api(`/contacts/${selectedContact}/initiative`,'POST');toast('Дарья подумает, как начать разговор');}));
$('add-contact').addEventListener('click',()=>{$('contact-dialog').showModal();$('contact-handle').focus();});
document.querySelectorAll('.close-dialog').forEach(button=>button.addEventListener('click',()=>button.closest('dialog').close()));
$('contact-form').addEventListener('submit',event=>{event.preventDefault();busy(event.submitter,async()=>{
  const contact=await api('/contacts','POST',{handle:$('contact-handle').value.trim(),proactive:$('new-contact-proactive').checked});
  $('contact-dialog').close();$('contact-form').reset();selectedContact=contact.id;await loadContacts();await loadConversation();toast('Собеседник добавлен');
});});
$('import-dialogs').addEventListener('click',()=>busy($('import-dialogs'),async()=>{
  const dialogs=await api('/telegram/dialogs');const list=$('import-list');list.replaceChildren();
  if(!dialogs.length)list.append(node('div','Личных диалогов пока нет','empty-state'));
  for(const contact of dialogs){
    const row=node('div',undefined,'import-row');row.append(node('span',contact.name));
    const add=node('button',contacts.some(c=>c.id===contact.id)?'Добавлен':'Добавить','small-button');
    add.disabled=contacts.some(c=>c.id===contact.id);
    add.addEventListener('click',()=>busy(add,async()=>{await api('/contacts','POST',{handle:String(contact.id),proactive:false});await loadContacts();add.textContent='Добавлен';setTimeout(()=>add.disabled=true,0);}));
    row.append(add);list.append(row);
  }
  $('import-dialog').showModal();
}));
$('memory-contact').addEventListener('change',()=>loadMemory().catch(error=>toast(error.message,true)));
async function loadMemory(){
  const list=$('memory-list');list.replaceChildren();const id=$('memory-contact').value;
  if(!id){list.append(node('div','Выбери собеседника, чтобы посмотреть её воспоминания','empty-state'));return;}
  const [current,archive,notebooks]=await Promise.all([api(`/contacts/${id}/history`),api(`/contacts/${id}/history?archive=true`),api(`/contacts/${id}/notebooks`)]);
  const data={memories:[...current.memories,...archive.memories]};
  if(!data.memories.length&&!notebooks.length)list.append(node('div','Она пока не сохранила заметок об этом разговоре','empty-state'));
  const sections={summary:'Разговор',people:'О собеседниках',threads:'Темы',self_notes:'Что Дарья уже сказала'};
  for(const notebook of notebooks){
    const row=node('div',undefined,'memory-row');row.append(node('strong',notebook.topic_id?`Блокнот · тема ${notebook.topic_id}`:'Блокнот разговора'));
    for(const [section,entries] of Object.entries(notebook.notes)){
      row.append(node('small',sections[section]||section,'hint'));
      for(const entry of entries){
        row.append(node('p',(entry.status==='closed'?'Завершено · ':entry.status==='open'?'Можно продолжить · ':'')+entry.text));
        const sources=node('details');sources.append(node('summary','По каким сообщениям'));
        for(const source of entry.evidence)sources.append(node('p',`№${source.id}: ${source.quote}`));row.append(sources);
      }
    }
    const download=node('button','Скачать для Obsidian','text-button');download.addEventListener('click',()=>{
      const url=URL.createObjectURL(new Blob([notebook.markdown],{type:'text/markdown;charset=utf-8'}));
      const link=document.createElement('a');link.href=url;link.download=`darya-${id}-topic-${notebook.topic_id}.md`;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
    });row.append(download);
    const remove=node('button','Забыть блокнот','text-button');remove.addEventListener('click',()=>busy(remove,async()=>{await api(`/contacts/${id}/notebooks/${notebook.topic_id}`,'DELETE');await loadMemory();toast('Блокнот очищен');}));row.append(remove);list.append(row);
  }
  for(const memory of data.memories){
    const row=node('div',undefined,'memory-row');row.append(node('strong',memory.key),node('p',memory.value));
    row.append(node('small',(memory.archived?'Архив · не участвует в ответах':'Текущая память')+(memory.sender_id?' · Участник '+memory.sender_id:''),'hint'));
    const remove=node('button','Забыть','text-button');remove.addEventListener('click',()=>busy(remove,async()=>{await api(`/contacts/${id}/memories/${encodeURIComponent(memory.key)}?sender_id=${memory.sender_id}&generation=${memory.generation}`,'DELETE');await loadMemory();toast('Воспоминание удалено');}));row.append(remove);list.append(row);
  }
}
async function loadTasks(){
  const tasks=await api('/tasks');$('task-list').replaceChildren();$('tasks-empty').hidden=!!tasks.length;
  const labels={pending:'Запланировано',sending:'Отправляется',sent:'Отправлено',failed:'Ошибка',cancelled:'Отменено',archived:'В архиве',uncertain:'Проверь доставку'};
  for(const task of tasks){
    const row=node('tr');const when=node('td',date(task.due));const content=node('td');content.append(node('div',task.name),node('small',task.text));
    const status=node('td');status.append(node('span',labels[task.status]||task.status,'task-status '+task.status));
    if(task.error)status.append(node('small',task.error));
    const action=node('td');if(task.status==='pending'){
      const cancel=node('button','Отменить','text-button');cancel.addEventListener('click',()=>busy(cancel,async()=>{await api('/tasks/'+task.id,'DELETE');await loadTasks();}));action.append(cancel);
    }
    row.append(when,content,status,action);$('task-list').append(row);
  }
}
$('add-task').addEventListener('click',()=>{const d=new Date(Date.now()+3600000);$('task-when').value=new Date(d.getTime()-d.getTimezoneOffset()*60000).toISOString().slice(0,16);$('task-dialog').showModal();});
$('task-form').addEventListener('submit',event=>{event.preventDefault();busy(event.submitter,async()=>{
  await api('/tasks','POST',{contact_id:+$('task-contact').value,text:$('task-text').value,when:new Date($('task-when').value).toISOString(),repeat_hours:+$('task-repeat').value});
  $('task-dialog').close();$('task-text').value='';await loadTasks();toast('Сообщение запланировано');
});});
async function loadEvents(){
  const events=await api('/events');const list=$('events-list');list.replaceChildren();
  if(!events.length)list.append(node('div','Действий пока нет','empty-state'));
  for(const event of events){const row=node('div',undefined,'event-row '+event.level);row.append(node('span',date(event.created),'event-time'),node('p',event.text));list.append(row);}
}
$('refresh-events').addEventListener('click',()=>busy($('refresh-events'),loadEvents));
$('ai-provider').addEventListener('change',()=>{
  const values={deepseek:['https://api.deepseek.com/v1','deepseek-chat'],openrouter:['https://openrouter.ai/api/v1','']};
  if(values[$('ai-provider').value])[$('ai-base-url').value,$('ai-model').value]=values[$('ai-provider').value];
  queueAutoProbe();
});
function aiFormValues(){
  return {base_url:$('ai-base-url').value.trim(),model:$('ai-model').value.trim(),key:$('ai-key').value.trim()};
}
function imageFormValues(){
  return {base_url:$('image-base-url').value.trim(),model:$('image-model').value.trim(),
    api_mode:$('image-api-mode').value,key:$('image-key').value.trim()};
}
let imageDiscoveryTimer, imageDiscoveryRunning=false;
async function discoverImageModels(){
  const fields=imageFormValues();
  const found=await api('/ai/images/models','POST',fields);
  if(JSON.stringify(fields)!==JSON.stringify(imageFormValues()))return;
  $('image-models').replaceChildren();
  for(const model of [...new Set([...found.image_models,...found.models])])$('image-models').append(new Option(model,model));
  $('image-result').textContent=found.image_models.length ? `Найдено моделей для изображений: ${found.image_models.length}. Выбери модель или впиши её имя` : 'Список получен. Впиши модель, которая умеет создавать изображения';
}
function queueImageDiscovery(){
  clearTimeout(imageDiscoveryTimer);
  imageDiscoveryTimer=setTimeout(async()=>{
    if(imageDiscoveryRunning){queueImageDiscovery();return;}
    if(!$('image-key').value.trim()&&!state?.ai.configured&&!state?.image_generation?.configured)return;
    imageDiscoveryRunning=true;
    try{await discoverImageModels();}catch(error){$('image-result').textContent=error.message;}
    finally{imageDiscoveryRunning=false;}
  },1800);
}
for(const id of ['image-base-url','image-key'])$(id).addEventListener('input',queueImageDiscovery);
$('image-refresh-models').addEventListener('click',()=>busy($('image-refresh-models'),discoverImageModels));
$('image-form').addEventListener('submit',event=>{event.preventDefault();busy(event.submitter,async()=>{
  clearTimeout(imageDiscoveryTimer);
  await api('/ai/images/config','POST',{...imageFormValues(),enabled:$('image-enabled').checked,clear_key:$('image-clear-key').checked});
  $('image-key').value='';$('image-clear-key').checked=false;
  state=await api('/status');applyStatus();
  $('image-result').textContent=state.image_generation.configured?'Сохранено. Теперь можно попросить Дарью нарисовать в личке или группе':'Настройки сохранены. Для этого адреса нужен API-ключ';
  toast('Модель генерации сохранена');
});});
$('image-test').addEventListener('click',()=>busy($('image-test'),async()=>{
  clearTimeout(imageDiscoveryTimer);$('image-preview').hidden=true;
  $('image-result').textContent='Создаю пробное изображение…';
  try{
    const started=await api('/ai/images/test','POST',imageFormValues());
    let result;
    for(let attempt=0;attempt<150;attempt++){
      result=await api('/ai/images/test/'+encodeURIComponent(started.run_id));
      if(result.status==='failed')throw new Error(result.error);
      if(result.status==='complete')break;
      await new Promise(resolve=>setTimeout(resolve,2000));
    }
    if(result?.status!=='complete')throw new Error('Генерация ещё не завершилась. Запрос уже отправлен, не запускай повторно');
    $('image-preview').src=result.image;$('image-preview').hidden=false;
    $('image-result').textContent='Изображение создано. Сохрани настройки, чтобы использовать эту модель в Telegram';
  }catch(error){$('image-result').textContent=error.message;throw error;}
}));
let autoProbeTimer, autoProbeRunning=false;
function populateModels(models){
  $('ai-models').replaceChildren();$('ai-model-select').replaceChildren(new Option('Выбрать модель из API',''));
  for(const model of models){$('ai-models').append(new Option(model,model));$('ai-model-select').add(new Option(model,model));}
  $('ai-model-select').hidden=!models.length;
  if(models.length)$('model-discovery-state').textContent=`Доступно моделей: ${models.length}`;
}
async function discoverAndCheck(check=true){
  const fields=aiFormValues();
  const found=await api('/ai/models','POST',{...fields,model:fields.model||null});
  if(JSON.stringify(fields)!==JSON.stringify(aiFormValues()))return;
  populateModels(found.models);
  if(!fields.model||!found.models.includes(fields.model))$('ai-model').value=found.selected;
  if(check){await api('/ai/test','POST',aiFormValues());$('model-discovery-state').textContent=`Модель отвечает · доступно ${found.models.length}`;}
}
function queueAutoProbe(){
  clearTimeout(autoProbeTimer);
  autoProbeTimer=setTimeout(async()=>{
    if(autoProbeRunning){queueAutoProbe();return;}
    if(!$('ai-base-url').value||(!$('ai-key').value.trim()&&!state?.ai.configured))return;
    autoProbeRunning=true;$('model-discovery-state').textContent='Получаю модели и проверяю подключение…';
    try{await discoverAndCheck();}catch(error){$('model-discovery-state').textContent=error.message;}
    finally{autoProbeRunning=false;}
  },1800);
}
for(const id of ['ai-base-url','ai-model','ai-key'])$(id).addEventListener('input',queueAutoProbe);
$('ai-model-select').addEventListener('change',()=>{if($('ai-model-select').value){$('ai-model').value=$('ai-model-select').value;queueAutoProbe();}});
$('refresh-models').addEventListener('click',()=>busy($('refresh-models'),()=>discoverAndCheck(false)));
$('ai-form').addEventListener('submit',event=>{event.preventDefault();busy(event.submitter,async()=>{
  clearTimeout(autoProbeTimer);
  await api('/ai/config','POST',aiFormValues());
  $('ai-key').value='';state=await api('/status');applyStatus();toast('Модель сохранена');
});});
$('test-ai').addEventListener('click',()=>{
  clearTimeout(autoProbeTimer);
  if(!$('ai-form').reportValidity())return;
  busy($('test-ai'),async()=>{
    await api('/ai/test','POST',aiFormValues());
    const pending=$('ai-key').value.trim() || $('ai-base-url').value.trim().replace(/\/$/,'')!==state.settings.ai_base_url.replace(/\/$/,'') || $('ai-model').value.trim()!==state.settings.ai_model;
    toast(pending?'Модель отвечает — нажми «Сохранить», чтобы подключить её':'Модель отвечает — можно поговорить с Дарьей');
  });
});
$('telegram-form').addEventListener('submit',event=>{event.preventDefault();busy(event.submitter,async()=>{
  await api('/telegram/code','POST',{api_id:+$('telegram-api-id').value,api_hash:$('telegram-api-hash').value.trim(),phone:$('telegram-phone').value.replace(/[\s()-]/g,'')});
  telegramEditing=false;
  state=await api('/status');applyStatus();if(state.telegram.connected)toast('Аккаунт подключён');else{$('telegram-code').focus();toast('Telegram принял запрос — способ доставки указан под формой');}
});});
$('telegram-code-form').addEventListener('submit',event=>{event.preventDefault();busy(event.submitter,async()=>{
  await api('/telegram/sign-in','POST',{code:$('telegram-code').value.trim()});$('telegram-code').value='';
  state=await api('/status');applyStatus();if(state.telegram.connected)toast('Аккаунт подключён');else $('telegram-password').focus();
});});
$('telegram-password-form').addEventListener('submit',event=>{event.preventDefault();busy(event.submitter,async()=>{
  await api('/telegram/password','POST',{password:$('telegram-password').value});$('telegram-password').value='';
  state=await api('/status');applyStatus();toast('Аккаунт подключён');
});});
function updateResendButton(){
  const button=$('new-code');
  const remaining=Math.max(0,Math.ceil((state?.telegram?.login?.resend_at || 0)-Date.now()/1000));
  button.textContent=remaining ? `Новый код через ${remaining} с` : 'Новый код';
  if(!button.dataset.resending)button.disabled=remaining>0;
}
$('new-code').addEventListener('click',()=>busy($('new-code'),async()=>{
  $('new-code').dataset.resending='true';
  try{
    await api('/telegram/resend','POST',{});
    state=await api('/status');applyStatus();$('telegram-code').value='';$('telegram-code').focus();toast('Повторный запрос принят — проверь способ доставки');
  }finally{delete $('new-code').dataset.resending;updateResendButton();}
}));
$('change-telegram-phone').addEventListener('click',()=>{telegramEditing=true;applyStatus();});
function updateQrView(){
  const qr=state?.telegram?.qr || {}, image=$('telegram-qr-image'), link=$('telegram-qr-link');
  const remaining=Math.max(0,Math.ceil((qr.expires_at || 0)-Date.now()/1000));
  const active=state?.telegram?.state==='qr' && remaining>0 && !qr.error;
  image.hidden=!active;
  $('telegram-qr-status').textContent=qr.error || (active ? `QR-код действителен ещё ${remaining} с` : 'QR-код истёк. Нажми «Обновить QR-код».');
  const stamp=`${qr.expires_at}:${qr.version || 0}`;
  if(active && image.dataset.expires!==stamp){
    image.dataset.expires=stamp;image.src=`/api/telegram/qr-image?t=${qr.expires_at}&v=${qr.version || 0}`;
    link.hidden=true;link.removeAttribute('href');
    const expires=qr.expires_at,version=qr.version;
    api('/telegram/qr-link').then(data=>{
      if(state?.telegram?.state==='qr' && state.telegram.qr?.expires_at===expires && state.telegram.qr?.version===version && expires>Date.now()/1000 && /^tg:\/\/login\?token=[A-Za-z0-9_-]+$/.test(data.url)){
        link.href=data.url;link.hidden=false;
      }
    }).catch(()=>{});
  }
  if(!active){image.removeAttribute('src');delete image.dataset.expires;link.hidden=true;link.removeAttribute('href');}
}
function startQr(button){return busy(button,async()=>{
  await api('/telegram/qr','POST',{api_id:+$('telegram-api-id').value,api_hash:$('telegram-api-hash').value.trim()});
  telegramEditing=false;state=await api('/status');applyStatus();
  toast(state.telegram.connected ? 'Аккаунт подключён' : 'Открой Telegram и подтверди подключение устройства');
});}
for(const id of ['telegram-qr-start','telegram-qr-from-code','telegram-qr-refresh'])$(id).addEventListener('click',()=>startQr($(id)));
$('telegram-qr-back').addEventListener('click',()=>busy($('telegram-qr-back'),async()=>{
  await api('/telegram/cancel-login','POST',{});telegramEditing=false;state=await api('/status');applyStatus();
}));
$('disconnect-telegram').addEventListener('click',()=>busy($('disconnect-telegram'),async()=>{await api('/telegram/logout','POST');state=await api('/status');applyStatus();toast('Аккаунт отключён, Дарья на паузе');}));
$('password-form').addEventListener('submit',event=>{event.preventDefault();busy(event.submitter,async()=>{
  await api('/password','POST',{current:$('current-password').value,password:$('new-password').value});$('password-form').reset();toast('Пароль обновлён');
});});
setInterval(refresh,15000);
setInterval(updateResendButton,1000);
setInterval(updateQrView,1000);
setInterval(()=>{if(state?.telegram?.state==='qr')refresh();},2000);
api('/auth').then(async data=>{if(data.authenticated)await initialize();else showLogin();}).catch(error=>{$('login-error').textContent='Сервер пока не отвечает. Повтори через минуту';});

$('contact-profile-form').addEventListener('submit',event=>{event.preventDefault();busy(event.submitter,async()=>{
 await api(`/contacts/${selectedContact}`,'PATCH',{enabled:$('contact-enabled').checked,proactive:$('contact-proactive').checked,birthday:$('contact-birthday').value,notes:$('contact-notes').value,persona:$('contact-persona').value,hermes_allowed:$('contact-hermes').checked,calls_allowed:$('contact-calls').checked});toast('Профиль собеседника сохранён');
});});
$('gift-policy-form').addEventListener('submit',event=>{event.preventDefault();busy(event.submitter,async()=>{
 const rules={gifts_enabled:$('gifts-enabled').checked,gift_autonomous_enabled:$('gift-autonomous-enabled').checked,gift_max_price:+$('gift-max-price').value,gift_monthly_budget:+$('gift-monthly-budget').value,gift_interval_hours:+$('gift-interval').value,gift_check_hours:+$('gift-check').value};
 await api('/settings','PATCH',rules);Object.assign(state.settings,rules);toast('Правила подарков сохранены');await loadGifts();
});});
function updateGiftPolicyStatus(){
 const saved=state?.settings;if(!saved)return;
 const status=$('gift-policy-status');status.replaceChildren();
 const enabled=!!saved.gifts_enabled;
 status.append(node('strong',enabled?'Отправка подарков разрешена':'Отправка подарков выключена'));
 if(enabled)status.append(node('p',saved.gift_autonomous_enabled?'Подарки по просьбе и по инициативе Дарьи.':'Только по просьбе. Самостоятельные подарки выключены.'));
 status.append(node('p',enabled
  ? `Сохранено: до ${saved.gift_max_price} Stars за подарок, ${saved.gift_monthly_budget} Stars в месяц. У получателя тоже должно быть разрешение.`
  : 'Разрешение у получателя и заданный бюджет сами по себе не включают отправку. Включи общий переключатель ниже и сохрани правила.'));
 const changed=$('gifts-enabled').checked!==enabled || $('gift-autonomous-enabled').checked!==!!saved.gift_autonomous_enabled || +$('gift-max-price').value!==saved.gift_max_price || +$('gift-monthly-budget').value!==saved.gift_monthly_budget;
 if(changed)status.append(node('small','Изменения формы ещё не сохранены. Сейчас действуют настройки, указанные выше.','hint'));
}
['gifts-enabled','gift-autonomous-enabled','gift-max-price','gift-monthly-budget'].forEach(id=>$(id).addEventListener('input',updateGiftPolicyStatus));
async function loadGifts(){
 updateGiftPolicyStatus();
 const data=await api('/gifts');$('gift-balance').textContent=data.balance===null?'Telegram не подключён':`${data.balance} Stars`;$('gift-spent').textContent=`${data.spent} Stars`;
 const recipients=$('gift-recipients');recipients.replaceChildren();
 if(!contacts.length)recipients.append(node('div','Добавь собеседников в разделе «Диалоги»','empty-state'));
 for(const contact of contacts){
  const row=node('div',undefined,'permission-row');row.append(node('span',contact.name));const label=node('label',undefined,'rule-toggle');label.append(node('span','Может получать подарки'));const input=document.createElement('input');input.type='checkbox';input.checked=!!contact.gift_allowed;
  input.addEventListener('change',async()=>{input.disabled=true;try{await api(`/contacts/${contact.id}`,'PATCH',{gift_allowed:input.checked});contact.gift_allowed=input.checked;toast(input.checked&&!state.settings.gifts_enabled?'Получатель разрешён. Общая отправка подарков пока выключена.':'Получатель обновлён');}catch(error){input.checked=!input.checked;toast(error.message,true);}finally{input.disabled=false;}});label.append(input);row.append(label);recipients.append(row);
 }
 const catalog=$('gift-catalog');catalog.replaceChildren();
 if(!data.catalog.length)catalog.append(node('div',data.error||'Каталог появится после подключения Telegram','empty-state'));
 for(const gift of data.catalog){const row=node('div',undefined,'permission-row');row.append(node('span',gift.title),node('span',`${gift.stars} Stars · ${gift.available?'Доступен':'Недоступен'}`,'hint'));catalog.append(row);}
 const orders=$('gift-orders');orders.replaceChildren();if(!data.orders.length)orders.append(node('div','Дарья пока не дарила подарков','empty-state'));
 const labels={sent:'Отправлен',reserved:'Зарезервирован',sending:'Отправляется',uncertain:'Проверь доставку',failed:'Не оплачен'};
 for(const order of data.orders){const row=node('div',undefined,'memory-row');row.append(node('strong',order.name),node('p',`${order.message}\n${order.stars} Stars · ${date(order.created)}`),node('span',labels[order.status]||order.status,'hint'));orders.append(row);}
}
$('refresh-gifts').addEventListener('click',()=>busy($('refresh-gifts'),loadGifts));
$('hermes-settings-form').addEventListener('submit',event=>{event.preventDefault();busy(event.submitter,async()=>{
 const rules={hermes_enabled:$('hermes-enabled').checked,owner_id:+$('owner-id').value,agent_max_rounds:+$('agent-rounds').value};await api('/settings','PATCH',rules);Object.assign(state.settings,rules);toast('Настройки Hermes сохранены');
});});
$('hermes-job-form').addEventListener('submit',event=>{event.preventDefault();busy(event.submitter,async()=>{
 await api('/hermes/jobs','POST',{task:$('hermes-task').value});$('hermes-task').value='';await loadHermes();toast('Задание передано Hermes');
});});
async function loadHermes(){
 const data=await api('/hermes');$('hermes-state').textContent=data.status.connected?'Hermes подключён':'Hermes пока не отвечает';const list=$('hermes-jobs');list.replaceChildren();
 if(!data.jobs.length)list.append(node('div','Можно поручить первое задание','empty-state'));
 const labels={pending:'В очереди',running:'Выполняется',completed:'Завершено',failed:'Ошибка',interrupted:'Прервано'};
 for(const job of data.jobs){const row=node('div',undefined,'job-row');const head=node('div',undefined,'job-heading');head.append(node('strong',job.task),node('span',labels[job.status]||job.status,'hint'));row.append(head);if(job.result)row.append(node('pre',job.result,'job-result'));if(job.artifact){const download=node('a','Скачать CSV','text-button');download.href=`/api/media/${encodeURIComponent(job.artifact)}/download`;download.download=job.artifact;row.append(download);}list.append(row);}
}
$('refresh-hermes').addEventListener('click',()=>busy($('refresh-hermes'),loadHermes));
async function loadMedia(){
 const files=await api('/media');$('media-list').replaceChildren();$('media-choice').replaceChildren(new Option('Выбрать файл',''));
 if(!files.length)$('media-list').append(node('div','Добавь первый файл в медиатеку','empty-state'));
 for(const file of files){const row=node('div',undefined,'permission-row');row.append(node('span',file.filename),node('span',`${Math.round(file.bytes/1024)} КБ`,'hint'));const remove=node('button','Удалить','text-button');remove.addEventListener('click',()=>busy(remove,async()=>{await api(`/media/${encodeURIComponent(file.filename)}`,'DELETE',{});await loadMedia();toast('Файл удалён из медиатеки');}));row.append(remove);$('media-list').append(row);$('media-choice').add(new Option(file.filename,file.filename));}
}
$('media-upload-form').addEventListener('submit',event=>{event.preventDefault();busy(event.submitter,async()=>{
 const file=$('media-file').files[0];if(!file)return;if(file.size>3*1024*1024)throw new Error('Выбери файл до 3 МБ');
 const data=await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(reader.result.split(',')[1]);reader.onerror=reject;reader.readAsDataURL(file);});
 await api('/media','POST',{filename:file.name,base64:data});$('media-upload-form').reset();await loadMedia();toast('Файл добавлен');
});});
$('media-send-form').addEventListener('submit',event=>{event.preventDefault();busy(event.submitter,async()=>{
 await api('/media/send','POST',{contact_id:+$('media-contact').value,filename:$('media-choice').value,caption:$('media-caption').value});toast('Файл отправлен');
});});
