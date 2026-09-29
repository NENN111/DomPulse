const CATEGORIES = {water:'Вода',heating:'Отопление',electricity:'Электричество',elevator:'Лифт',cleaning:'Уборка',yard:'Двор',other:'Другое'};
const STATUSES = {new:'Новое',accepted:'Принято',in_progress:'В работе',resolved:'Ожидает проверки',confirmed:'Выполнено',reopened:'Возвращено'};
const PRIORITIES = {normal:'Обычная',urgent:'Срочно',emergency:'Авария'};
const NEXT = {new:['accepted','Принять'],accepted:['in_progress','Начать работу'],in_progress:['resolved','Отметить выполненным'],reopened:['in_progress','Вернуть в работу']};
const $ = selector => document.querySelector(selector);
const themeToggle = $('#theme-toggle');
function setTheme(theme) {
  const dark = theme === 'dark';
  document.documentElement.dataset.theme = dark ? 'dark' : 'light';
  themeToggle.textContent = dark ? 'Светлая тема' : 'Тёмная тема';
  themeToggle.setAttribute('aria-pressed', String(dark));
  document.querySelector('meta[name="theme-color"]').content = dark ? '#171a23' : '#f6f7f9';
  try { localStorage.setItem('dompulse-theme', dark ? 'dark' : 'light'); } catch {}
}
themeToggle.addEventListener('click', () => setTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark'));
themeToggle.textContent = document.documentElement.dataset.theme === 'dark' ? 'Светлая тема' : 'Тёмная тема';
themeToggle.setAttribute('aria-pressed', String(document.documentElement.dataset.theme === 'dark'));
const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const fmtDate = value => value ? new Intl.DateTimeFormat('ru-RU',{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit',timeZone:'Europe/Moscow'}).format(new Date(value)) : '—';
const overdue = ticket => ticket.due_at && !ticket.first_response_at && new Date(ticket.due_at) < new Date();
let data = null, activeTab = 'queue', selectedTicket = null, drawerMode = null;
const previewMode = new URLSearchParams(location.search).get('preview');
const preview = previewMode !== null;
const hashParams = new URLSearchParams(location.hash.slice(1));
const launch = window.WebApp?.initData || hashParams.get('WebAppData') || '';
const loginCode = hashParams.get('login') || '';
if(loginCode) history.replaceState(null,'',location.pathname+location.search);
if(launch || loginCode) document.documentElement.classList.add('max-launch-loading');
let siteSession = sessionStorage.getItem('dompulse-miniapp-session') || '';
function showLogin(message='') {
  document.documentElement.classList.remove('max-launch-loading');
  $('.main-content').classList.add('login-locked');
  $('#login-error').textContent=message;
  $('#login-error').hidden=!message;
}
function showApp() { document.documentElement.classList.remove('max-launch-loading'); $('.main-content').classList.remove('login-locked'); }
window.WebApp?.ready?.();

let noticeTimer;
function notify(message, success=false) { const el=$('#ticket-drawer').classList.contains('open')?$('#drawer-notice'):$('#notice'); clearTimeout(noticeTimer); el.textContent=message; el.className='notice'+(success?' success':''); el.hidden=false; noticeTimer=setTimeout(()=>{el.hidden=true},6000); }
async function api(path, options={}) {
  if (!launch && !siteSession) throw new Error('Откройте приложение из чата с ботом MAX.');
  const authHeader=launch ? {'X-Max-Init-Data':launch} : {'X-Miniapp-Session':siteSession};
  const response=await fetch(path,{...options,headers:{...authHeader,'Content-Type':'application/json',...(options.headers||{})},cache:'no-store'});
  if(response.status===401 && !launch){siteSession='';sessionStorage.removeItem('dompulse-miniapp-session');showLogin('Срок входа истёк. Откройте мини-приложение заново через бота MAX.');}
  const body=await response.json().catch(()=>({}));
  if (!response.ok && [401,403].includes(response.status)) {
    data=null;
    closeDrawer();
    showLogin(typeof body.detail==='string' ? body.detail : 'Откройте приложение заново через чат с ботом MAX.');
  }
  if (!response.ok) throw new Error(typeof body.detail==='string' ? body.detail : 'Не удалось выполнить действие');
  return body;
}
function demoData() {
  const now=Date.now(), date=(mins)=>new Date(now-mins*60000).toISOString();
  const tickets=[
    {id:'demo-water-1',house_id:'demo-house',category:'water',location:'Подвал',description:'ДЕМО: течёт труба в подвале',status:'new',priority:'emergency',version:1,created_at:date(180),due_at:date(165),first_response_at:null},
    {id:'demo-water-2',house_id:'demo-house',category:'water',location:'Подвал',description:'ДЕМО: вода из трубы в подвале',status:'accepted',priority:'normal',version:1,created_at:date(120),due_at:date(-1320),first_response_at:date(100)},
    {id:'demo-heating',house_id:'demo-house',category:'heating',location:'Подъезд 2',description:'ДЕМО: батареи не нагреваются',status:'in_progress',priority:'urgent',version:1,created_at:date(240),due_at:date(120),first_response_at:date(90)},
    {id:'demo-elevator',house_id:'demo-house',category:'elevator',location:'Лифт',description:'ДЕМО: лифт останавливается между этажами',status:'new',priority:'normal',version:1,created_at:date(90),due_at:date(-1350),first_response_at:date(60)},
    {id:'demo-cleaning',house_id:'demo-house',category:'cleaning',location:'Лестничная клетка',description:'ДЕМО: уборка выполнена',status:'confirmed',priority:'normal',version:1,created_at:date(10080),due_at:date(8640),first_response_at:date(10040)}
  ];
  return {operator:{name:'Оператор УК',district:'ЦАО'},houses:[{id:'demo-house',address:'Лиственничная аллея, 16'}],house_id:'demo-house',tickets,signals:[{ticket_ids:['demo-water-1','demo-water-2'],category:'water',location:'Подвал',count:2,description:'Течь трубы в подвале'}],announcements:[{title:'ДЕМО: плановые работы в доме',created_at:date(60)}],metrics:{active:4,emergency:1,overdue:1,average_first_response_minutes:60,responded_with_sla_data:4,first_response_on_time_percent:75,top_categories:[['water',2],['heating',1],['elevator',1]],top_locations:[['Подвал',2],['Подъезд 2',1],['Лифт',1]]}};
}
async function load(announce=false) {
  if(!preview && !launch && !siteSession){showLogin();return;}
  const refresh=data?.role==='resident' ? $('#resident-refresh-btn') : $('#refresh-btn');
  if(announce && refresh.disabled) return;
  if(announce) {
    refresh.disabled=true;
    refresh.classList.add('is-loading');
    refresh.setAttribute('aria-busy','true');
  }
  try {
    const selected=(data?.role==='resident' ? $('#resident-house-select') : $('#house-select')).value;
    data=preview ? (previewMode==='resident' ? residentDemoData() : demoData()) : await api('/api/miniapp/overview'+(selected?'?house_id='+encodeURIComponent(selected):''));
    showApp();
    if(data.role==='resident') {
      document.querySelectorAll('.operator-view').forEach(element=>element.hidden=true);
      $('#resident-app').hidden=false;
      $('#resident-name').textContent=data.resident.name+(preview?' · просмотр макета':'');
      $('#resident-house-select').innerHTML=data.houses.map(h=>`<option value="${esc(h.id)}" ${h.id===data.house_id?'selected':''}>${esc(h.address)}</option>`).join('');
      syncMobileSelect($('#resident-house-select'));
      renderResident();
    } else {
      $('#resident-app').hidden=true;
      document.querySelectorAll('.operator-view').forEach(element=>element.hidden=false);
      $('#operator-name').textContent=data.operator.name+(data.operator.district?' · '+data.operator.district:'')+(preview?' · просмотр макета':'');
      $('#house-select').innerHTML=data.houses.map(h=>`<option value="${esc(h.id)}" ${h.id===data.house_id?'selected':''}>${esc(h.address)}</option>`).join('');
      syncMobileSelect($('#house-select'));
      render();
    }
    if(announce) notify(preview?'Демонстрационные данные обновлены':'Данные обновлены',true);
  } catch(err) { if(!data) showLogin(err.message); else notify(err.message); $('#operator-name').textContent='Данные недоступны'; }
  finally {
    if(announce) {
      refresh.disabled=false;
      refresh.classList.remove('is-loading');
      refresh.removeAttribute('aria-busy');
    }
  }
}
function render() {
  if (!data) return;
  const active=data.tickets.filter(t=>t.status!=='confirmed');
  $('#queue-count').textContent=active.length;
  $('#signals-count').textContent=data.signals.length;
  $('#queue-total').textContent=`${active.length} активных из ${data.tickets.length}`;
  renderQueue(); renderSignals(); renderMetrics();
}
function renderQueue() {
  const filter=$('#ticket-filter').value, search=$('#ticket-search').value.trim().toLowerCase();
  const tickets=data.tickets.filter(t=>{
    if (filter==='active' && t.status==='confirmed') return false;
    if (filter==='overdue' && !overdue(t)) return false;
    if (filter==='emergency' && (t.priority!=='emergency' || t.status==='confirmed')) return false;
    return !search || `${t.location} ${t.description} ${CATEGORIES[t.category]||''} ${t.id}`.toLowerCase().includes(search);
  });
  $('#ticket-list').innerHTML=tickets.length ? tickets.map(t=>`<article class="ticket-card" data-ticket="${esc(t.id)}" tabindex="0" role="button" aria-label="Открыть заявку ${esc(t.id.slice(0,8))}"><div><div class="ticket-meta"><span>№ ${esc(t.id.slice(0,8))}</span><span>${fmtDate(t.created_at)}</span><span>${esc(CATEGORIES[t.category]||t.category)}</span></div><h3>${esc(t.location)}</h3><p>${esc(t.description)}</p></div><div class="ticket-end"><span class="badge ${esc(t.status)}">${esc(STATUSES[t.status]||t.status)}</span><div>${t.priority!=='normal'?`<span class="badge ${esc(t.priority)}">${esc(PRIORITIES[t.priority])}</span>`:''}${overdue(t)?' <span class="badge overdue">Просрочено</span>':''}</div></div></article>`).join('') : '<div class="empty"><strong>Заявок не найдено</strong>Попробуйте другой фильтр или поисковый запрос.</div>';
}
function renderSignals() {
  $('#signal-list').innerHTML=data.signals.length ? data.signals.map((s,i)=>`<article class="signal-card"><div><div class="signal-count">${s.count} похожих обращения · сигнал ${i+1}</div><h3>${esc(CATEGORIES[s.category]||s.category)} · ${esc(s.location)}</h3><p>${esc(s.description)}</p></div><div><select class="signal-priority" data-index="${i}" aria-label="Срочность общего инцидента"><option value="normal">Обычная</option><option value="urgent">Срочно</option><option value="emergency">Авария</option></select> <button class="secondary-button confirm-signal" data-index="${i}">Подтвердить</button></div></article>`).join('') : '<div class="empty"><strong>Общих сигналов пока нет</strong>Когда появятся похожие обращения, они будут здесь.</div>';
  enhanceMobileSelects(document.getElementById("signal-list"));
}
function metricCard(label,value,hint,percent,kind='') { return `<div class="surface metric-card ${kind}"><span class="label">${label}</span><strong>${value}</strong><span class="hint">${hint}</span><div class="mini-bar" aria-hidden="true"><i style="width:${Math.min(100,Math.max(0,percent))}%"></i></div></div>`; }
function chartRows(items, translate=x=>x) { const max=Math.max(1,...items.map(x=>x[1])); return items.length ? items.map(([name,count])=>`<div class="chart-row"><span>${esc(translate(name))}</span><div class="bar-track" aria-hidden="true"><i style="width:${100*count/max}%"></i></div><strong>${count}</strong></div>`).join('') : '<p class="section-description">Данных пока нет.</p>'; }
function renderMetrics() {
  const m=data.metrics,total=data.tickets.length||1,active=m.active||0;
  const average=m.average_first_response_minutes==null?'—':(m.average_first_response_minutes<60?`${m.average_first_response_minutes} мин`:`${Math.round(m.average_first_response_minutes/60*10)/10} ч`);
  const sla=m.first_response_on_time_percent,shownSla=sla==null?'—':`${sla}%`;
  const announcements=data.announcements.length?data.announcements.map(a=>`<li>${esc(a.title)}<small>${fmtDate(a.created_at)}</small></li>`).join(''):'<li>Объявлений пока нет</li>';
  $('#metrics-content').innerHTML=`<div class="metric-grid">
    ${metricCard('Активные',active,'Сейчас в работе',active/total*100)}
    ${metricCard('Аварийные',m.emergency,'Среди активных',m.emergency/Math.max(1,active)*100,'danger')}
    ${metricCard('Просроченные',m.overdue,'Без первой реакции',m.overdue/Math.max(1,active)*100,'warning')}
    ${metricCard('Средняя первая реакция',average,'По заявкам с ответом',m.average_first_response_minutes==null?0:Math.min(100,m.average_first_response_minutes/1440*100))}
    ${metricCard('Ответ в пределах SLA',shownSla,`${m.responded_with_sla_data} заявок с ответом`,sla||0)}
    ${metricCard('Общие сигналы',data.signals.length,'Ожидают решения',data.signals.length/Math.max(1,active)*100)}
  </div><div class="metric-layout"><div class="surface"><h3>Частые категории</h3>${chartRows(m.top_categories,x=>CATEGORIES[x]||x)}</div><div class="surface"><h3>Проблемные места</h3>${chartRows(m.top_locations)}</div><div class="surface"><h3>Соблюдение SLA</h3><div class="donut-row"><div class="donut" style="--percent:${sla||0}%" aria-label="${shownSla} ответов в пределах SLA"><strong>${shownSla}</strong></div><div class="donut-copy"><b>${m.responded_with_sla_data}</b>заявок получили первый ответ. Показатель рассчитан по всем заявкам дома с ответом.</div></div></div><div class="surface"><h3>Последние объявления</h3><ul class="ann-list">${announcements}</ul></div></div>`;
}
function switchTab(tab) {
  activeTab=tab;
  document.querySelectorAll('.tab').forEach(el=>{const active=el.dataset.tab===tab;el.classList.toggle('active',active);if(active)el.setAttribute('aria-current','page');else el.removeAttribute('aria-current')});
  document.querySelectorAll('.panel').forEach(el=>el.classList.toggle('active',el.id===tab+'-panel'));
}
async function openTicket(id) {
  try {
    selectedTicket=preview?{...data.tickets.find(t=>t.id===id),events:[],attachments:[]}:await api('/api/tickets/'+encodeURIComponent(id));
    drawerMode='ticket';$('#drawer-notice').hidden=true;renderDrawer();$('#drawer-backdrop').hidden=false;$('#ticket-drawer').classList.add('open');$('#ticket-drawer').setAttribute('aria-hidden','false');
  } catch(err) { notify(err.message); }
}
function closeDrawer() { $('#drawer-notice').hidden=true;$('#drawer-backdrop').hidden=true;$('#ticket-drawer').classList.remove('open');$('#ticket-drawer').setAttribute('aria-hidden','true');selectedTicket=null;drawerMode=null; }
function renderDrawer() {
  if(data?.role==='resident') return renderResidentDrawer();
  const t=selectedTicket, next=NEXT[t.status];
  $('#drawer-title').textContent='Заявка №'+t.id.slice(0,8);
  $('#drawer-content').innerHTML=`<div class="detail-line"><span>Статус</span><b>${esc(STATUSES[t.status]||t.status)}</b></div><div class="detail-line"><span>Категория</span><b>${esc(CATEGORIES[t.category]||t.category)}</b></div><div class="detail-line"><span>Место</span><b>${esc(t.location)}</b></div><div class="detail-line"><span>Создана</span><b>${fmtDate(t.created_at)}</b></div><div class="detail-line"><span>Первый ответ</span><b>${fmtDate(t.first_response_at)}</b></div><div class="detail-description">${esc(t.description)}</div><div class="form-block"><h3>Срочность</h3><select id="priority-select"><option value="normal" ${t.priority==='normal'?'selected':''}>Обычная</option><option value="urgent" ${t.priority==='urgent'?'selected':''}>Срочно</option><option value="emergency" ${t.priority==='emergency'?'selected':''}>Авария</option></select><button id="save-priority" class="secondary-button">Сохранить срочность</button></div><div class="form-block"><h3>Ответ жителю</h3><textarea id="reply-text" maxlength="4000" placeholder="Напишите ответ по заявке"></textarea><button id="send-reply" class="primary-button">Отправить ответ</button></div>${next?`<div class="form-block"><h3>Следующий этап</h3><textarea id="status-comment" maxlength="4000" placeholder="Короткий комментарий для жителя"></textarea><button id="change-status" class="secondary-button">${next[1]}</button></div>`:''}<div class="form-block"><h3>История</h3><ul class="event-list">${t.events.length?t.events.map(e=>`<li><b>${esc(e.actor_name)}</b>: ${esc(e.text)}<small>${fmtDate(e.created_at)}</small></li>`).join(''):'<li>История действий пока пуста.</li>'}</ul></div>`;
  enhanceMobileSelects(document.getElementById("drawer-content"));
}
async function mutate(path,payload) {
  if(preview){notify('В режиме просмотра действия недоступны');return;}
  try {const id=selectedTicket.id;await api(path,{method:'POST',body:JSON.stringify(payload)});notify('Изменения сохранены',true);await load();await openTicket(id);}catch(err){notify(err.message);}
}
document.addEventListener('click',async event=>{
  const residentTab=event.target.closest('[data-resident-tab]');if(residentTab){switchResidentTab(residentTab.dataset.residentTab);return;}
  if(event.target.closest('#new-ticket')){openResidentCreate();return;}
  const announcement=event.target.closest('[data-announcement]');if(announcement){openResidentAnnouncement(announcement.dataset.announcement);return;}
  if(event.target.id==='create-resident-ticket'){createResidentTicket();return;}
  if(event.target.id==='resident-comment-send'){sendResidentComment();return;}
  if(event.target.id==='resident-confirm'){event.target.hidden=true;$('#resident-confirm-box').hidden=false;return;}
  if(event.target.id==='resident-confirm-submit'){changeResidentStatus('confirmed');return;}
  if(event.target.id==='resident-confirm-cancel'){$('#resident-confirm-box').hidden=true;$('#resident-confirm').hidden=false;return;}
  if(event.target.id==='resident-reopen-submit'){changeResidentStatus('reopened');return;}
  const tab=event.target.closest('[data-tab]');if(tab){switchTab(tab.dataset.tab);return;}
  const ticket=event.target.closest('[data-ticket]');if(ticket){openTicket(ticket.dataset.ticket);return;}
  const signal=event.target.closest('.confirm-signal');if(signal){const index=Number(signal.dataset.index),s=data.signals[index],priority=document.querySelector(`.signal-priority[data-index="${index}"]`).value;if(!confirm(`Подтвердить общий инцидент по ${s.count} обращениям? Жители получат уведомление.`))return;if(preview){notify('В режиме просмотра действия недоступны');return;}try{await api('/api/miniapp/incidents?house_id='+encodeURIComponent(data.house_id),{method:'POST',body:JSON.stringify({ticket_ids:s.ticket_ids,priority})});notify('Общий инцидент подтверждён',true);await load()}catch(err){notify(err.message)}return;}
  if(event.target.id==='send-reply'){const text=$('#reply-text').value.trim();if(text.length<3)return notify('Ответ должен содержать не менее 3 символов');mutate('/api/tickets/'+encodeURIComponent(selectedTicket.id)+'/comments',{text});}
  if(event.target.id==='change-status'){const comment=$('#status-comment').value.trim();if(comment.length<3)return notify('Комментарий должен содержать не менее 3 символов');mutate('/api/tickets/'+encodeURIComponent(selectedTicket.id)+'/status',{status:NEXT[selectedTicket.status][0],comment,expected_version:selectedTicket.version});}
  if(event.target.id==='save-priority'){mutate('/api/miniapp/tickets/'+encodeURIComponent(selectedTicket.id)+'/priority',{priority:$('#priority-select').value,expected_version:selectedTicket.version});}
});
document.addEventListener('keydown',event=>{if(event.key==='Escape')closeDrawer();if((event.key==='Enter'||event.key===' ')&&event.target.matches('[data-ticket], [data-announcement]')){event.preventDefault();if(event.target.dataset.ticket)openTicket(event.target.dataset.ticket);else openResidentAnnouncement(event.target.dataset.announcement)}});
document.addEventListener('submit',event=>{if(event.target.id==='resident-create-form'){event.preventDefault();createResidentTicket();}});
$('#drawer-close').addEventListener('click',closeDrawer);$('#drawer-backdrop').addEventListener('click',closeDrawer);
$('#house-select').addEventListener('change',()=>load());$('#refresh-btn').addEventListener('click',()=>load(true));
$('#resident-house-select').addEventListener('change',()=>load());$('#resident-refresh-btn').addEventListener('click',()=>load(true));
$('#ticket-filter').addEventListener('change',renderQueue);$('#ticket-search').addEventListener('input',renderQueue);
const initialTab=new URLSearchParams(location.search).get('tab');
if(['queue','signals','metrics'].includes(initialTab))switchTab(initialTab);
async function bootstrap() {
  if(loginCode && !launch) {
    try {
      const response=await fetch('/api/miniapp/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({code:loginCode}),cache:'no-store'});
      const body=await response.json().catch(()=>({}));
      if(!response.ok)throw new Error(typeof body.detail==='string'?body.detail:'Ссылка для входа недействительна. Откройте новую кнопку в боте.');
      siteSession=body.token;
      sessionStorage.setItem('dompulse-miniapp-session',siteSession);
    } catch(error) { showLogin(error.message); return; }
  }
  await load();
  if(previewMode==='resident' && new URLSearchParams(location.search).get('drawer')==='create') openResidentCreate();
}
bootstrap();
let mobileSelect = null;
const selectSheet = document.createElement('div');
selectSheet.className = 'select-sheet';
selectSheet.hidden = true;
selectSheet.innerHTML = '<div class="select-sheet-backdrop"></div><div class="select-sheet-panel" role="dialog" aria-modal="true" aria-labelledby="select-sheet-title"><div class="select-sheet-header"><h2 id="select-sheet-title"></h2><button class="select-sheet-close" type="button" aria-label="Закрыть список">×</button></div><div class="select-sheet-search" hidden><input type="search" aria-label="Поиск адреса" placeholder="Найти адрес дома" autocomplete="off"></div><div class="select-sheet-options"></div><p class="select-sheet-empty" hidden>Адрес не найден в списке.</p></div>';
document.body.append(selectSheet);
function selectTitle(select) {
  const label = select.id && document.querySelector('label[for="' + select.id + '"]');
  return select.getAttribute('aria-label') || label?.textContent || 'Выберите значение';
}
function syncMobileSelect(select) {
  const trigger = select.nextElementSibling;
  if (!trigger?.classList.contains('mobile-select-trigger')) return;
  trigger.querySelector('span').textContent = select.selectedOptions[0]?.textContent || selectTitle(select);
  trigger.disabled = select.disabled || !select.options.length;
}
function closeMobileSelect() {
  if (!mobileSelect) return;
  const trigger = mobileSelect.nextElementSibling;
  selectSheet.hidden = true;
  document.body.classList.remove('select-sheet-open');
  mobileSelect = null;
  if (trigger?.isConnected) {
    trigger.setAttribute('aria-expanded', 'false');
    trigger.focus({preventScroll: true});
  }
}
function openMobileSelect(select) {
  if (mobileSelect) closeMobileSelect();
  mobileSelect = select;
  const trigger = select.nextElementSibling;
  document.getElementById('select-sheet-title').textContent = selectTitle(select);
  const options = selectSheet.querySelector('.select-sheet-options');
  options.replaceChildren(...Array.from(select.options, (option, index) => {
    const item = document.createElement('button');
    item.type = 'button';
    item.className = 'select-sheet-option';
    item.textContent = option.textContent;
    item.dataset.index = index;
    item.disabled = option.disabled;
    item.setAttribute('aria-current', String(option.selected));
    return item;
  }));
  const search = selectSheet.querySelector('.select-sheet-search');
  search.hidden = !['house-select','resident-house-select'].includes(select.id);
  const searchInput = search.querySelector('input');
  searchInput.value = '';
  selectSheet.querySelector('.select-sheet-empty').hidden = true;
  if (matchMedia('(min-width: 701px)').matches) {
    const rect = trigger.getBoundingClientRect();
    const height = Math.min(420, innerHeight - 20);
    const top = rect.bottom + height + 8 <= innerHeight ? rect.bottom + 8 : Math.max(10, rect.top - height - 8);
    const panel = selectSheet.querySelector('.select-sheet-panel');
    panel.style.setProperty('--sheet-top', top + 'px');
    panel.style.setProperty('--sheet-left', rect.left + 'px');
    panel.style.setProperty('--sheet-width', rect.width + 'px');
  }
  selectSheet.hidden = false;
  document.body.classList.add('select-sheet-open');
  trigger.setAttribute('aria-expanded', 'true');
  if (search.hidden) (options.querySelector('[aria-current="true"]') || options.querySelector('button'))?.focus({preventScroll: true});
  else searchInput.focus({preventScroll: true});
}
function enhanceMobileSelects(root = document) {
  root.querySelectorAll('select:not([data-mobile-enhanced])').forEach(select => {
    select.dataset.mobileEnhanced = 'true';
    const trigger = document.createElement('button');
    trigger.type = 'button';
    trigger.className = 'mobile-select-trigger';
    trigger.setAttribute('aria-label', selectTitle(select));
    trigger.setAttribute('aria-haspopup', 'dialog');
    trigger.setAttribute('aria-expanded', 'false');
    trigger.innerHTML = '<span></span><i aria-hidden="true"></i>';
    select.after(trigger);
    trigger.addEventListener('click', () => openMobileSelect(select));
    select.addEventListener('change', () => syncMobileSelect(select));
    syncMobileSelect(select);
  });
}
selectSheet.addEventListener('click', event => {
  if (event.target.closest('.select-sheet-backdrop, .select-sheet-close')) return closeMobileSelect();
  const option = event.target.closest('.select-sheet-option');
  if (!option || !mobileSelect) return;
  const select = mobileSelect;
  const value = select.options[Number(option.dataset.index)]?.value;
  closeMobileSelect();
  if (value !== undefined && select.value !== value) {
    select.value = value;
    select.dispatchEvent(new Event('change', {bubbles: true}));
  }
});
selectSheet.querySelector('.select-sheet-search input').addEventListener('input', event => {
  const query = event.target.value.toLocaleLowerCase('ru').replace(/[.,]/g, ' ').replace(/\s+/g, ' ').trim();
  let visible = 0;
  selectSheet.querySelectorAll('.select-sheet-option').forEach(option => {
    const address = option.textContent.toLocaleLowerCase('ru').replace(/[.,]/g, ' ').replace(/\s+/g, ' ');
    option.hidden = !address.includes(query);
    if (!option.hidden) visible++;
  });
  selectSheet.querySelector('.select-sheet-empty').hidden = visible > 0;
});
selectSheet.addEventListener('keydown', event => {
  if (event.key === 'Escape') { event.stopPropagation(); closeMobileSelect(); }
  if (event.key === 'Tab') {
    const focusable = Array.from(selectSheet.querySelectorAll('button:not(:disabled), input')).filter(element => !element.hidden && element.getClientRects().length);
    const first = focusable[0], last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  }
});
enhanceMobileSelects();
window.addEventListener('resize', closeMobileSelect);


function residentDemoData() {
  const now=Date.now(), date=minutes=>new Date(now-minutes*60000).toISOString();
  return {role:'resident',resident:{name:'Житель дома'},houses:[{id:'demo-home',address:'Москва, Лиственничная аллея, дом 16',district:'САО',verification_method:'code'}],house_id:'demo-home',tickets:[{id:'demo-request-1',house_id:'demo-home',category:'water',location:'Подвал',description:'В подвале появилась вода рядом со стояком.',status:'in_progress',priority:'urgent',version:2,created_at:date(160),updated_at:date(110),first_response_at:date(110),events:[{actor_name:'Житель дома',text:'В подвале появилась вода рядом со стояком.',created_at:date(160)},{actor_name:'Диспетчер УК',text:'Мастер направлен на проверку.',created_at:date(110)}],attachments:[]}],announcements:[{id:'demo-announcement-1',title:'Проверка инженерных систем',body:'На этой неделе специалисты проверят оборудование в местах общего пользования. Доступ в квартиры не требуется.',created_at:date(1440)}]};
}
function switchResidentTab(tab) {
  document.querySelectorAll('[data-resident-tab]').forEach(button=>{const current=button.dataset.residentTab===tab;button.classList.toggle('active',current);if(current)button.setAttribute('aria-current','page');else button.removeAttribute('aria-current')});
  document.querySelectorAll('.resident-panel').forEach(panel=>panel.classList.toggle('active',panel.id==='resident-'+tab+'-panel'));
}
function renderResident() {
  const tickets=data.tickets||[], announcements=data.announcements||[];
  $('#resident-ticket-count').textContent=tickets.length;
  $('#resident-announcement-count').textContent=announcements.length;
  $('#resident-ticket-list').innerHTML=tickets.length?tickets.map(ticket=>`<article class="ticket-card" data-ticket="${esc(ticket.id)}" tabindex="0" role="button" aria-label="Открыть заявку ${esc(ticket.id.slice(0,8))}"><div><div class="ticket-meta"><span>№ ${esc(ticket.id.slice(0,8))}</span><span>${fmtDate(ticket.created_at)}</span><span>${esc(CATEGORIES[ticket.category]||ticket.category)}</span></div><h3>${esc(ticket.location)}</h3><p>${esc(ticket.description)}</p></div><div class="ticket-end"><span class="badge ${esc(ticket.status)}">${esc(STATUSES[ticket.status]||ticket.status)}</span></div></article>`).join(''):'<div class="empty"><strong>Заявок пока нет</strong>Сообщите о проблеме в доме — обращение появится здесь.</div>';
  $('#resident-announcement-list').innerHTML=announcements.length?announcements.map(item=>`<article class="ticket-card announcement-card" data-announcement="${esc(item.id)}" tabindex="0" role="button" aria-label="Открыть объявление ${esc(item.title)}"><div><div class="ticket-meta">${fmtDate(item.created_at)}</div><h3>${esc(item.title)}</h3><p>${esc(item.body)}</p></div></article>`).join(''):'<div class="empty"><strong>Объявлений пока нет</strong>Сообщения управляющей компании появятся здесь.</div>';
  const house=data.houses.find(item=>item.id===data.house_id);
  $('#resident-home-content').innerHTML=house?`<div class="surface home-details"><div class="detail-line"><span>Адрес</span><b>${esc(house.address)}</b></div><div class="detail-line"><span>Округ</span><b>${esc(house.district||'Не указан')}</b></div><div class="detail-line"><span>Привязка</span><b>${esc(({gosuslugi:'Подтверждена через Госуслуги',code:'Подтверждена кодом УК',address_code:'Подтверждена адресом и кодом УК',legacy:'Привязка из предыдущей версии'})[house.verification_method]||'Способ подтверждения не указан')}</b></div><p class="section-description">Для просмотра другого подтверждённого дома выберите адрес вверху страницы.</p></div>`:'<div class="empty"><strong>Дом не выбран</strong>Добавьте дом через чат с ботом MAX.</div>';
}
function openDrawer(mode,title,html) {
  drawerMode=mode;
  $('#drawer-title').textContent=title;
  $('#drawer-content').innerHTML=html;
  $('#drawer-notice').hidden=true;
  $('#drawer-backdrop').hidden=false;
  $('#ticket-drawer').classList.add('open');
  $('#ticket-drawer').setAttribute('aria-hidden','false');
  $('#drawer-close').focus({preventScroll:true});
  enhanceMobileSelects($('#drawer-content'));
}
function openResidentCreate() {
  if(!data?.house_id)return notify('Сначала подтвердите дом в боте MAX.');
  selectedTicket=null;
  const house=data.houses.find(item=>item.id===data.house_id);
  openDrawer('create','Новая заявка',`<p class="drawer-context">${esc(house?.address||'')}</p><form id="resident-create-form" class="resident-form"><label for="resident-category">Категория</label><select id="resident-category">${Object.entries(CATEGORIES).map(([value,label])=>`<option value="${value}">${label}</option>`).join('')}</select><label for="resident-location">Где возникла проблема</label><input id="resident-location" maxlength="160" minlength="2" required placeholder="Например, подъезд 2, 3 этаж"><label for="resident-description">Что произошло</label><textarea id="resident-description" maxlength="4000" minlength="10" required placeholder="Опишите проблему, чтобы диспетчер мог помочь"></textarea><p class="form-hint">Заявку увидят сотрудники УК вашего дома.</p><button id="create-resident-ticket" class="primary-button" type="button">Отправить заявку</button></form>`);
}
function openResidentAnnouncement(id) {
  const item=data.announcements.find(value=>value.id===id);
  if(!item)return;
  selectedTicket=null;
  openDrawer('announcement',item.title,`<p class="drawer-context">Объявление УК · ${fmtDate(item.created_at)}</p><div class="detail-description announcement-body">${esc(item.body)}</div>`);
}
function renderResidentDrawer() {
  const ticket=selectedTicket;
  const history=ticket.events?.length?ticket.events.map(item=>`<li><b>${esc(item.actor_name)}</b>: ${esc(item.text)}<small>${fmtDate(item.created_at)}</small></li>`).join(''):'<li>История действий пока пуста.</li>';
  const actions=ticket.status==='resolved'?`<div class="form-block"><h3>Проверьте результат</h3><p class="form-hint">Если проблема устранена, подтвердите выполнение. Если осталась — верните заявку в работу.</p><button id="resident-confirm" class="primary-button" type="button">Всё исправлено</button><div id="resident-confirm-box" class="inline-confirm" hidden><p>Подтвердить, что проблема решена?</p><button id="resident-confirm-submit" class="primary-button" type="button">Подтвердить</button><button id="resident-confirm-cancel" class="secondary-button" type="button">Отмена</button></div><label for="resident-reopen-comment">Проблема осталась</label><textarea id="resident-reopen-comment" maxlength="4000" placeholder="Что ещё не исправлено?"></textarea><button id="resident-reopen-submit" class="secondary-button" type="button">Вернуть в работу</button></div>`:'';
  const comment=ticket.status!=='confirmed'?`<div class="form-block"><h3>Сообщение диспетчеру</h3><textarea id="resident-comment-text" maxlength="4000" placeholder="Уточните детали по заявке"></textarea><button id="resident-comment-send" class="secondary-button" type="button">Отправить сообщение</button></div>`:'';
  openDrawer('ticket','Заявка №'+ticket.id.slice(0,8),`<div class="detail-line"><span>Статус</span><b>${esc(STATUSES[ticket.status]||ticket.status)}</b></div><div class="detail-line"><span>Категория</span><b>${esc(CATEGORIES[ticket.category]||ticket.category)}</b></div><div class="detail-line"><span>Место</span><b>${esc(ticket.location)}</b></div><div class="detail-line"><span>Создана</span><b>${fmtDate(ticket.created_at)}</b></div><div class="detail-description">${esc(ticket.description)}</div>${actions}${comment}<div class="form-block"><h3>История</h3><ul class="event-list">${history}</ul></div>`);
}
async function residentAction(button,work,success,keepTicket=false) {
  if(preview)return notify('В режиме просмотра действия недоступны');
  button.disabled=true;
  try {
    const id=selectedTicket?.id;
    await work();
    closeDrawer();
    await load();
    notify(success,true);
    if(keepTicket&&id)await openTicket(id);
  } catch(error) {notify(error.message);button.disabled=false;}
}
function createResidentTicket() {
  const location=$('#resident-location').value.trim(), description=$('#resident-description').value.trim();
  if(location.length<2)return notify('Укажите место проблемы: не менее 2 символов.');
  if(description.length<10)return notify('Опишите проблему: не менее 10 символов.');
  const button=$('#create-resident-ticket');
  residentAction(button,()=>api('/api/tickets',{method:'POST',body:JSON.stringify({house_id:data.house_id,category:$('#resident-category').value,location,description})}),'Заявка отправлена в УК');
}
function sendResidentComment() {
  const message=$('#resident-comment-text').value.trim();
  if(message.length<3)return notify('Сообщение должно содержать не менее 3 символов.');
  const button=$('#resident-comment-send');
  residentAction(button,()=>api('/api/tickets/'+encodeURIComponent(selectedTicket.id)+'/comments',{method:'POST',body:JSON.stringify({text:message})}),'Сообщение отправлено',true);
}
function changeResidentStatus(status) {
  const comment=status==='confirmed'?'Проблема устранена.':$('#resident-reopen-comment').value.trim();
  if(comment.length<3)return notify('Уточните, что ещё не исправлено.');
  const button=status==='confirmed'?$('#resident-confirm-submit'):$('#resident-reopen-submit');
  residentAction(button,()=>api('/api/tickets/'+encodeURIComponent(selectedTicket.id)+'/status',{method:'POST',body:JSON.stringify({status,comment,expected_version:selectedTicket.version})}),status==='confirmed'?'Выполнение подтверждено':'Заявка возвращена в работу');
}
