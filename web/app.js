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
let data = null, activeTab = 'queue', selectedTicket = null;
const preview = new URLSearchParams(location.search).has('preview');
const hashParams = new URLSearchParams(location.hash.slice(1));
const launch = window.WebApp?.initData || hashParams.get('WebAppData') || '';
const loginCode = hashParams.get('login') || '';
if(loginCode) history.replaceState(null,'',location.pathname+location.search);
let siteSession = sessionStorage.getItem('dompulse-miniapp-session') || '';
function showLogin(message='') {
  $('.main-content').classList.add('login-locked');
  $('#login-error').textContent=message;
  $('#login-error').hidden=!message;
}
function showApp() { $('.main-content').classList.remove('login-locked'); }
window.WebApp?.ready?.();

let noticeTimer;
function notify(message, success=false) { const el=$('#notice'); clearTimeout(noticeTimer); el.textContent=message; el.className='notice'+(success?' success':''); el.hidden=false; noticeTimer=setTimeout(()=>{el.hidden=true},6000); }
async function api(path, options={}) {
  if (!launch && !siteSession) throw new Error('Откройте приложение через новую кнопку в боте MAX.');
  const authHeader=launch ? {'X-Max-Init-Data':launch} : {'X-Miniapp-Session':siteSession};
  const response=await fetch(path,{...options,headers:{...authHeader,'Content-Type':'application/json',...(options.headers||{})},cache:'no-store'});
  if(response.status===401 && !launch){siteSession='';sessionStorage.removeItem('dompulse-miniapp-session');showLogin('Срок входа истёк. Откройте мини-приложение заново через бота MAX.');}
  const body=await response.json().catch(()=>({}));
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
  return {operator:{name:'Оператор УК'},houses:[{id:'demo-house',address:'Лиственничная аллея, 16'}],house_id:'demo-house',tickets,signals:[{ticket_ids:['demo-water-1','demo-water-2'],category:'water',location:'Подвал',count:2,description:'Течь трубы в подвале'}],announcements:[{title:'ДЕМО: плановые работы в доме',created_at:date(60)}],metrics:{active:4,emergency:1,overdue:1,average_first_response_minutes:60,responded_with_sla_data:4,first_response_on_time_percent:75,top_categories:[['water',2],['heating',1],['elevator',1]],top_locations:[['Подвал',2],['Подъезд 2',1],['Лифт',1]]}};
}
async function load(announce=false) {
  if(!preview && !launch && !siteSession){showLogin();return;}
  const refresh=$('#refresh-btn');
  if(announce && refresh.disabled) return;
  if(announce) {
    refresh.disabled=true;
    refresh.classList.add('is-loading');
    refresh.setAttribute('aria-busy','true');
  }
  try {
    const selected=$('#house-select').value;
    data=preview ? demoData() : await api('/api/miniapp/overview'+(selected?'?house_id='+encodeURIComponent(selected):''));
    showApp();
    $('#operator-name').textContent=data.operator.name+(preview?' · просмотр макета':'');
    $('#house-select').innerHTML=data.houses.map(h=>`<option value="${esc(h.id)}" ${h.id===data.house_id?'selected':''}>${esc(h.address)}</option>`).join('');
    syncMobileSelect(document.getElementById("house-select"));
    render();
    if(announce) notify(preview?'Демонстрационные данные обновлены':'Данные обновлены',true);
  } catch(err) { notify(err.message); $('#operator-name').textContent='Данные недоступны'; }
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
    renderDrawer();$('#drawer-backdrop').hidden=false;$('#ticket-drawer').classList.add('open');$('#ticket-drawer').setAttribute('aria-hidden','false');
  } catch(err) { notify(err.message); }
}
function closeDrawer() { $('#drawer-backdrop').hidden=true;$('#ticket-drawer').classList.remove('open');$('#ticket-drawer').setAttribute('aria-hidden','true');selectedTicket=null; }
function renderDrawer() {
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
  const tab=event.target.closest('[data-tab]');if(tab){switchTab(tab.dataset.tab);return;}
  const ticket=event.target.closest('[data-ticket]');if(ticket){openTicket(ticket.dataset.ticket);return;}
  const signal=event.target.closest('.confirm-signal');if(signal){const index=Number(signal.dataset.index),s=data.signals[index],priority=document.querySelector(`.signal-priority[data-index="${index}"]`).value;if(!confirm(`Подтвердить общий инцидент по ${s.count} обращениям? Жители получат уведомление.`))return;if(preview){notify('В режиме просмотра действия недоступны');return;}try{await api('/api/miniapp/incidents?house_id='+encodeURIComponent(data.house_id),{method:'POST',body:JSON.stringify({ticket_ids:s.ticket_ids,priority})});notify('Общий инцидент подтверждён',true);await load()}catch(err){notify(err.message)}return;}
  if(event.target.id==='send-reply'){const text=$('#reply-text').value.trim();if(text.length<3)return notify('Ответ должен содержать не менее 3 символов');mutate('/api/tickets/'+encodeURIComponent(selectedTicket.id)+'/comments',{text});}
  if(event.target.id==='change-status'){const comment=$('#status-comment').value.trim();if(comment.length<3)return notify('Комментарий должен содержать не менее 3 символов');mutate('/api/tickets/'+encodeURIComponent(selectedTicket.id)+'/status',{status:NEXT[selectedTicket.status][0],comment,expected_version:selectedTicket.version});}
  if(event.target.id==='save-priority'){mutate('/api/miniapp/tickets/'+encodeURIComponent(selectedTicket.id)+'/priority',{priority:$('#priority-select').value,expected_version:selectedTicket.version});}
});
document.addEventListener('keydown',event=>{if(event.key==='Escape')closeDrawer();if((event.key==='Enter'||event.key===' ')&&event.target.matches('[data-ticket]')){event.preventDefault();openTicket(event.target.dataset.ticket)}});
$('#drawer-close').addEventListener('click',closeDrawer);$('#drawer-backdrop').addEventListener('click',closeDrawer);
$('#house-select').addEventListener('change',()=>load());$('#refresh-btn').addEventListener('click',()=>load(true));
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
}
bootstrap();
let mobileSelect = null;
const selectSheet = document.createElement('div');
selectSheet.className = 'select-sheet';
selectSheet.hidden = true;
selectSheet.innerHTML = '<div class="select-sheet-backdrop"></div><div class="select-sheet-panel" role="dialog" aria-modal="true" aria-labelledby="select-sheet-title"><div class="select-sheet-header"><h2 id="select-sheet-title"></h2><button class="select-sheet-close" type="button" aria-label="Закрыть список">×</button></div><div class="select-sheet-options"></div></div>';
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
  selectSheet.hidden = false;
  document.body.classList.add('select-sheet-open');
  trigger.setAttribute('aria-expanded', 'true');
  (options.querySelector('[aria-current="true"]') || options.querySelector('button'))?.focus({preventScroll: true});
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
selectSheet.addEventListener('keydown', event => {
  if (event.key === 'Escape') { event.stopPropagation(); closeMobileSelect(); }
  if (event.key === 'Tab') {
    const focusable = Array.from(selectSheet.querySelectorAll('button:not(:disabled)'));
    const first = focusable[0], last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  }
});
enhanceMobileSelects();
