import * as drive from './drive.mjs';
import {defaults,formatDate} from './preferences.mjs';
let preferences=defaults();
function applyPreferences() {
  const html=document.documentElement;
  html.lang=preferences.language;
  html.dataset.theme=preferences.theme==='system'?(matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light'):preferences.theme;
  html.dataset.density=preferences.density;html.style.fontSize=preferences.fontSize+'px';html.style.setProperty('--accent',preferences.accent);
}
matchMedia('(prefers-color-scheme: dark)').addEventListener('change',applyPreferences);
import {coverageView,monitorHeadline} from './coverage.mjs';
import {checkPublication} from './publication-check.mjs';
import {filterItems,summarize,captureLabel} from './catalog.mjs';
const $=id=>document.getElementById(id), monitor=location.pathname.replace(/\/$/,'').endsWith('/monitor');
const node=(tag,text,cls='')=>{const n=document.createElement(tag);n.textContent=text;n.className=cls;return n;};
if(monitor) {
  const panel=node('section','','card'),label=node('label','Контрольний URL публікації'),input=node('input'),button=node('button','Перевірити в архіві'),result=node('p');
  input.type='url';input.id='control-publication-url';input.placeholder='https://…';label.htmlFor=input.id;result.id='control-publication-result';result.setAttribute('role','status');
  panel.append(label,input,button,result);$('content').before(panel);
  button.onclick=async()=>{
    const current=session;
    button.disabled=true;result.textContent='Звіряю з поточним архівом…';
    try {const [snapshot,catalog,report]=await Promise.all([drive.readArchive(),drive.readSources(),drive.readMonitor()]);
      if(current===session)result.textContent=checkPublication(snapshot.items,input.value,catalog.sources||catalog,report?.coverage);
    } catch(error){if(current===session)result.textContent='Перевірку не завершено: '+error.message;}
    finally {button.disabled=false;}
  };
}
const stamp=value=>value ? formatDate(value,preferences) : '—';
const bytes=n=>n>=1048576?(n/1048576).toFixed(1)+' МіБ':n>=1024?(n/1024).toFixed(1)+' КіБ':n+' Б';
let archive={items:[],packages:[]}, offset=0, timer, session=0, view=0, loading=false;
$('archive-link').href='../archive/'; $('monitor-link').href='../monitor/';
$('title').textContent=monitor?'Моніторинг колекторів':'Матеріали';
$('subtitle').textContent=monitor?'ARGUS / МОНІТОРИНГ':'ARGUS / АРХІВ';
for(const id of ['filters','analytics','pagination']) $(id).hidden=monitor;
drive.prepareSignIn().then(()=>{$('signin').disabled=false;}).catch(e=>{$('login-error').textContent=e.message;});
function login(message='') {
  preferences=defaults();applyPreferences();
  document.body.classList.add('login-view');
  drive.clearSession(); session++; view++; loading=false; clearInterval(timer);
  archive={items:[],packages:[]}; $('content').replaceChildren(); $('stats').replaceChildren(); $('charts').replaceChildren();
  if($('control-publication-result')){$('control-publication-result').textContent='';$('control-publication-url').value='';}
  $('search').value=''; $('platform').replaceChildren(node('option','Усі платформи')); $('platform').firstChild.value='';
  $('sample').value='all'; $('workspace').hidden=true; $('login').hidden=false; $('logout').hidden=true;
  $('signin').disabled=!drive.secure || !drive.ready; $('login-error').textContent=message;
}
function stat(label,value) {const el=node('div','','stat');el.append(node('span',label),node('strong',String(value)));return el;}
function sourceLink(url,label) {
  try {const u=new URL(url);if(!['https:','http:'].includes(u.protocol)) return null;
    const a=node('a',label);a.href=u.href;a.target='_blank';a.rel='noopener noreferrer';return a;
  } catch{return null;}
}
function card(item) {
  const el=node('article','','card'), title=node('h2',item.tool || item.platform);
  if(item.sample) title.append(node('span','Тестовий матеріал','badge'));
  el.append(title,node('p',(item.published_at?'Опубліковано: ':'Виявлено: ')+stamp(item.published_at || item.discovered_at || item.observed_at),'meta'));
  if(captureLabel(item)) el.append(node('p',captureLabel(item),'badge'));
  if(item.source_match==='search_query_only') el.append(node('p','Авторство не підтверджено: знайдено за пошуковим запитом.','meta'));
  const text=node('p',item.text.slice(0,2500),'text');text.hidden=!preferences.previews;el.append(text);
  if(Array.isArray(item.search_evidence) && item.search_evidence.length) {
    const evidence=node('details','');evidence.append(node('summary','Як знайдено'));
    for(const entry of item.search_evidence.slice(0,10)) {
      if(entry && typeof entry==='object') evidence.append(node('p',`${entry.provider || ''} · ${entry.query || ''} · ${stamp(entry.discovered_at)}`,'meta'));
    }
    el.append(evidence);
  }
  if(item.text.length>2500||!preferences.previews) {const more=node('button','Читати повністю');more.onclick=()=>{text.textContent=item.text;text.hidden=false;more.remove();};el.append(more);}
  const original=sourceLink(item.url,'Відкрити оригінал ↗');if(original) el.append(original);
  if(item.tool==='sherlock') el.append(node('p','Збіги username — лише кандидати. Особу не підтверджено.','meta'));
  const list=node('ul','','files'), error=node('p','','card-error'), current=session, currentView=view;
  for(const file of item.files) {
    const row=node('li',''), label=String(file.name).replaceAll('\\','/').split('/').pop();
    const download=node('button','Завантажити');row.append(download,node('span',label+' · '+bytes(file.size)));
    async function load(preview,button) {
      button.disabled=true;error.textContent='';
      try {
        const result=await drive.attachment(file,preview);
        if(current!==session || currentView!==view || !el.isConnected) {drive.release(result.url);return;}
        if(preview && ['image/png','image/jpeg','video/mp4'].includes(result.type)) {
          const media=document.createElement(result.type.startsWith('image/')?'img':'video');
          media.src=result.url;
          if(media.tagName==='VIDEO') {
            media.controls=true;
            const caption=item.files.find(f=>/\.vtt$/i.test(f.name));
            if(caption) {
              const sub=await drive.attachment(caption,true);
              if(current!==session || currentView!==view || !el.isConnected) {drive.release(sub.url);drive.release(result.url);return;}
              if(sub.type==='text/vtt') {const track=document.createElement('track');track.src=sub.url;track.kind='subtitles';track.label='Субтитри';track.srclang='en';media.append(track);}
              else drive.release(sub.url);
            }
          } else media.alt='Вкладення матеріалу';
          el.querySelector('img,video')?.remove();el.append(media);
        } else {
          const a=node('a','');a.href=result.url;a.download=label;a.click();setTimeout(()=>drive.release(result.url),30000);
        }
      } catch(e) {if(current===session) {if([401,403,404].includes(e.status)) login(e.message);else error.textContent=e.message;}}
      finally {button.disabled=false;}
    }
    download.onclick=()=>load(false,download);
    if(file.size>100*1024*1024) {const link=sourceLink('https://drive.google.com/file/d/'+file.drive_id+'/view','Відкрити у Drive ↗');row.append(link);}
    if((preferences.images&&/\.(png|jpe?g)$/i.test(label))||(preferences.videos&&/\.mp4$/i.test(label))) {const preview=node('button','Переглянути');preview.onclick=()=>load(true,preview);row.prepend(preview);}
    list.append(row);
  }
  if(item.files.length) {const details=node('details','');details.append(node('summary',`Вкладення · ${item.files.length}`),list);el.append(details);}
  el.append(error);return el;
}
function charts(summary) {
  $('charts').replaceChildren();
  for(const [label,values] of [['За платформою',Object.entries(summary.platforms)],['За датою',Object.entries(summary.days).sort().slice(-14)]]) {
    const group=node('section','');group.append(node('h3',label));const max=Math.max(1,...values.map(([,v])=>v));
    for(const [name,value] of values) {const row=node('div','','bar'),meter=document.createElement('meter');meter.max=max;meter.value=value;meter.setAttribute('aria-label',name);row.append(node('span',name),meter,node('span',value));group.append(row);}
    $('charts').append(group);
  }
}
function render() {
  view++;
  // Release old previews when the page/filter changes, without keeping media in storage.
  $('content').querySelectorAll('[src^="blob:"]').forEach(n=>drive.release(n.src));
  const items=filterItems(archive.items,$('search').value,$('platform').value,$('sample').value);
  if(offset>=items.length) offset=0;
  const summary=summarize(items);
  $('stats').replaceChildren(stat('Матеріалів',summary.posts),stat('Платформ',summary.sources),stat('Медіафайлів',summary.media),stat('Обсяг медіа',bytes(summary.bytes)));
  charts(summary);$('content').replaceChildren(...items.slice(offset,offset+preferences.pageSize).map(card));
  if(!items.length) $('content').append(node('div',archive.items.length?'За цими фільтрами матеріалів немає.':'Архів поки порожній. Матеріали з’являться після успішного запису з GitHub.','empty'));
  $('previous').disabled=offset===0; $('next').disabled=offset+preferences.pageSize>=items.length;
  $('page-label').textContent=items.length?`${offset+1}–${Math.min(offset+preferences.pageSize,items.length)} із ${items.length}`:'0 матеріалів';
}
function renderMonitor(report) {
  $('content').replaceChildren();$('stats').replaceChildren();
  if(!report) {$('content').append(node('div','Звітів хмарного збору ще немає.','empty'));return;}
  const status={passed:'Успішно',failed:'Помилка',unknown:'Невідомо',partial:'Частково виконано',collected:'Публікації отримано',no_posts:'Публікацій не отримано',no_sources:'Джерела не налаштовані',completed:'Пошук завершено',not_configured:'Потрібен ключ пошукового API',disabled:'Вимкнено'};
  const headline=monitorHeadline(report);
  if(headline)$('stats').append(...headline.stats.map(([label,value])=>stat(label,value)));
  else $('stats').append(stat('Останній запуск',status[report.state] || 'Невідомо'),stat('Колекторів',report.stages.length),stat('З результатами',report.stages.filter(s=>['passed','collected'].includes(s.status)).length),stat('Run',report.run_id || '—'));
  const coverage=coverageView(report);
  if(coverage) {
    const panel=node('article','','card');panel.append(node('h2',coverage.title));
    for(const text of [...coverage.lines,...coverage.platforms])panel.append(node('p',text));
    const details=node('details','');details.append(node('summary','Остання успішна перевірка кожного джерела'));
    for(const text of coverage.sources)details.append(node('p',text));
    panel.append(details);$('content').append(panel);
  }
  if(report.live) {
    const live=report.live, stale=Date.now()-Date.parse(live.updated_at)>90000;
    const platforms=Object.values(live.platforms), total=k=>platforms.reduce((s,p)=>s+(p[k]||0),0);
    const queue=live.pending, flowing=platforms.some(p=>p.new['5']>0);
    const panel=node('article','','card');
    panel.append(node('h2','Безперервний збір'),node('p',stale?'Телеметрія застаріла':queue?'Очікують доставки: '+queue:flowing?'Нові записи надходять':'За 5 хвилин нових записів немає'));
    panel.append(node('p',`Активні джерела: ${total('active')} · Перевірені за годину: ${total('checked')} · Без успішної перевірки: ${total('without_success')} · Черга доставки: ${queue} · Черга помилок: ${live.dead_letters}`));
    panel.append(node('p','Остання успішна перевірка: '+(live.last_success?stamp(new Date(live.last_success*1000).toISOString()):'—')));
    for(const [platform,m] of Object.entries(live.platforms)) {
      panel.append(node('h3',platform),node('p',`Нові за 1 / 5 / 15 / 60 хв: ${[1,5,15,60].map(n=>m.new[n]).join(' / ')} · Перевірено за годину: ${m.checked}/${m.active}`),
        node('p',`Останні перевірки джерел · отримано ${m.received}, збережено нових ${m.saved}, дублікатів ${m.duplicates}, джерел із помилкою ${m.errors} · Затримка за годину, середня / p95: ${m.mean_seconds??'—'} / ${m.p95_seconds??'—'} с`));
    }
    const details=node('details','');details.append(node('summary','Остання перевірка кожного джерела'));
    for(const [id,r] of Object.entries(live.sources)) details.append(node('p',`${id} · ${r.status||'Ще не перевірено'} · ${r.checked?stamp(new Date(r.checked*1000).toISOString()):'—'} · отримано / нових / дублікатів: ${r.received??'—'} / ${r.saved??'—'} / ${r.duplicates??'—'}`));
    panel.append(details);$('content').append(panel);
  }
  const history=headline?node('details','','card'):$('content');
  if(headline){history.append(node('summary','Історичний звіт GitHub · '+stamp(report.updated_at)));$('content').append(history);}
  for(const stage of report.stages) {
    const el=node('article','','card');el.append(node('h2',stage.tool),node('p',status[stage.status] || 'Невідомо'),node('p',stamp(stage.observed_at),'meta'));
    if(Number.isInteger(stage.posts)) el.append(node('p',`Публікацій: ${stage.posts}`));
    for(const [reason,count] of Object.entries(stage.results || {})) {
      const labels={ok:'Джерел із постами',login_required:'Потрібен вхід',http_429:'Обмеження частоти',http_403:'Доступ відхилено',http_404:'Адресу не знайдено',no_public_posts:'Немає публічних постів',challenge_required:'Перевірка платформи',collection_failed:'Помилка збору',network_error:'Помилка мережі',timeout:'Час очікування вичерпано',requests:'Пошукових запитів',candidates:'Знайдених кандидатів',hydrated:'Отримано публічних карток',previews:'Збережено пошукових прев’ю'};
      el.append(node('p',`${labels[reason] || reason}: ${count}`,'meta'));
    }
    for(const metric of stage.metrics || []) if(Number.isFinite(metric.wall_seconds)) el.append(node('p',`${metric.stage}: ${metric.wall_seconds.toFixed(1)} с${Number.isFinite(metric.max_process_rss_kib)?' · RAM процесу '+(metric.max_process_rss_kib/1024).toFixed(1)+' МіБ':''}`,'meta'));
    history.append(el);
  }
  if(/^\d+$/.test(report.run_id || '')) {const a=sourceLink('https://github.com/cutebigmonkey-team/ArgusPRO/actions/runs/'+report.run_id,'Відкрити запуск у GitHub ↗');history.append(a);}
}
async function refresh() {
  if(loading) return;loading=true;$('refresh').disabled=true;const current=session;
  try {
    const value=await (monitor?drive.readMonitor():drive.readArchive());
    if(current!==session) return;
    if(monitor) renderMonitor(value); else {
      const changed=archive.updated_at!==value.updated_at || archive.liveSequence!==value.liveSequence || !$('content').children.length;
      archive=value;const selected=$('platform').value;
      $('platform').replaceChildren(node('option','Усі платформи'));$('platform').firstChild.value='';
      for(const name of Object.keys(summarize(archive.items).platforms).sort()) {const option=node('option',name);option.value=name;$('platform').append(option);}
      $('platform').value=selected;if(changed)render();
    }
    $('status').textContent='Оновлено: '+stamp(monitor?(monitorHeadline(value)?.at||value?.updated_at):value?.updated_at);
  } catch(e) {if(current===session) {if([401,403,404].includes(e.status)) login(e.message);else $('status').textContent=e.message;}}
  finally {if(current===session) {loading=false;$('refresh').disabled=false;}}
}
$('signin').onclick=async()=>{
  $('signin').disabled=true;$('login-error').textContent='';const current=session;
  try {
    await drive.signIn(); if(current!==session)return;
    // Verify actual folder access before displaying the workspace.
    archive=await drive.readArchive();if(current!==session)return;
    preferences=(await drive.readPreferences()).value;if(current!==session)return;applyPreferences();
    document.body.classList.remove('login-view');
    $('login').hidden=true;$('workspace').hidden=false;$('logout').hidden=false;
    await refresh();clearInterval(timer);timer=setInterval(()=>{if(!document.hidden)refresh();},monitor?10000:7000);
  } catch(e) {if(current===session) login(e.message);}
  finally {$('signin').disabled=false;}
};
$('logout').onclick=()=>login();$('refresh').onclick=refresh;
for(const id of ['search','platform','sample']) $(id).addEventListener(id==='search'?'input':'change',()=>{offset=0;render();});
$('previous').onclick=()=>{offset=Math.max(0,offset-preferences.pageSize);render();};$('next').onclick=()=>{offset+=preferences.pageSize;render();};
window.addEventListener('pagehide',()=>login());
