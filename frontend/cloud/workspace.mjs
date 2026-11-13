import * as drive from './drive.mjs';
import {createRagChat} from './rag-chat.mjs';
const ragChat=createRagChat((path,options)=>drive.aiRequest(path,options));
import {createControlCenter} from './control-center.mjs';
import {assessment} from './analysis.mjs';
const control=createControlCenter(drive,{onChange:()=>{if(activeSnapshot)update(activeSnapshot,currentAnalysis,analysisWarning);},getSnapshot:()=>activeSnapshot,logout:()=>ArgusCloud.logout()});
import {createWorkspaceData} from './workspace-data.mjs';
import {createReportsController} from './reports.mjs';
import {createKeywords} from './keywords.mjs';
import {createComments} from './comments.mjs';
import {createTelevision} from './television.mjs';
const television=createTelevision(drive);
const comments=createComments(drive);
const reports = createReportsController(async()=>{
  try {
    const snapshot=await drive.readArchive();
    try {return {snapshot,analysis:await drive.readAnalysis(snapshot)};}
    catch(e) {
      if(e.status===401) throw e;
      return {snapshot,analysis:null,warning:'Оцінки TypeSafe не вдалося прочитати. Довідку сформовано за архівом.'};
    }
  } catch(e) {if(e.status===401) ArgusCloud.logout(e.message);throw e;}
});
const root = document.getElementById('app-root'), login = document.getElementById('cloud-login');
const button = document.getElementById('signin'), error = document.getElementById('login-error');
const status = document.getElementById('cloud-status'), toolbar = document.getElementById('cloud-toolbar');
if (location.hash === '#/archive') location.replace('./archive/');
let generation = 0, booted = false, refreshTimer, response = null, snapshotDate, analysisDate, busy = false;
let sources=[], sourceRevision=0, currentAnalysis=null;
let publishedAnalytics=null;
let pendingCritical=new Map();
const badge=document.getElementById('cloud-new');
let pendingCount=0, lastDetails=0, analysisPending=false;
const readingOlder=()=> (document.querySelector('.dashboard-shell__wrapper')?.scrollTop || window.scrollY)>80;
function redraw() {
  if(location.hash==='#/argus-ai') return;
  if(location.hash==='#/tv') return;
  if(['#/settings','#/personalization','#/profile','#/help','#/notifications'].includes(location.hash))return;
  if(location.hash==='#/comments') {void comments.refresh().catch(e=>{status.textContent=e.message;});return;}
  if(location.hash === '#/keywords' || reports.isMounted()) return;
  const analytics=root.querySelector('[data-argus-on-argus-live-analytics]');
  if(analytics) {analytics.dispatchEvent(new Event('argus-live-analytics'));return;}
  const feed=root.querySelector('[data-argus-on-argus-live-feed]');
  if(feed) feed.dispatchEvent(new Event('argus-live-feed'));
  else if(!document.querySelector('.modal-overlay')) window.dispatchEvent(new PopStateEvent('popstate'));
}
badge.onclick=()=>{pendingCount=0;badge.hidden=true;redraw();};
window.ArgusCloud = {
  control,
  authenticated:false,
  mountAi(main) {void ragChat.mount(main);},
  unmountAi() {ragChat.unmount();},
  keywords:createKeywords(),
  async addSource(name,url,region,origin,submit,message,overlay) {
    if(!this.authenticated || submit.disabled) return;
    const current=generation;
    submit.disabled=true; message.style.display='block'; message.textContent='Збереження у Google Drive…';
    try {
      const row=await drive.submitSource(name,url,region,origin,sources);
      if(current!==generation) return;
      sourceRevision++; sources=[row,...sources];
      update(activeSnapshot,currentAnalysis,analysisWarning);
      overlay.remove();
      window.dispatchEvent(new PopStateEvent('popstate'));
      status.textContent='Джерело «'+row.name+'» збережено. Воно в черзі на автоматичний збір.';
    } catch(e) {
      message.textContent=e.status===403 || e.status===404 ? 'Запис недоступний. Використайте Google-акаунт із правом редактора папки ARGUS.' : e.message;
    } finally {submit.disabled=false;}
  },
  mountReports(main) {reports.mount(main);},
  mountComments(main) {void comments.mount(main);},
  mountTelevision(main) {void television.mount(main);},
  unmountTelevision() {television.clear();},
  response(path) {return response && this.authenticated ? response(path) : JSON.stringify({status:401,body:{error:'Увійдіть до Argus.'}});},
  logout(message = '') {
    ragChat.clear();
    television.clear();
    pendingCritical.clear();
    control.clear();
    comments.clear();
    generation++; pendingCount=0; badge.hidden=true; lastDetails=0; clearInterval(refreshTimer); drive.clearSession(); reports.clear(); response = null;
    this.keywords = createKeywords();
    activeSnapshot=undefined; analysisWarning=''; sources=[]; sourceRevision++; currentAnalysis=null;
    publishedAnalytics=null;
    document.querySelectorAll('.modal-overlay').forEach(el=>el.remove());
    this.authenticated = false;
    document.body.classList.remove('cloud-authenticated', 'cloud-menu-open');
    if (booted) window.dispatchEvent(new PopStateEvent('popstate'));
    root.replaceChildren(); root.hidden = true; toolbar.hidden = true; login.hidden = false;
    error.textContent = message; status.textContent = ''; document.title = 'Вхід · ARGUS';
    button.disabled = !drive.secure || !drive.ready;
  },
};
function update(snapshot,analysis,warning='') {
  ragChat.update(snapshot);
  currentAnalysis=analysis;
  response = createWorkspaceData(snapshot,analysis,sources,publishedAnalytics,control.preferences()); snapshotDate = snapshot.updated_at; analysisDate=analysis?.updated_at;
  reports.update(snapshot,analysis);
  status.textContent = 'Оновлено: ' + (snapshotDate ? new Date(snapshotDate).toLocaleString('uk-UA') : 'архів порожній') +
    (warning ? ' · '+warning : analysis ? ' · Оцінки TypeSafe доступні' : ' · Оцінки TypeSafe очікуються');
}
let analysisWarning='', activeSnapshot;
async function refreshAnalysis(snapshot,current) {
  let analysis=null, warning='',published=null;
  try { analysis=await drive.readAnalysis(snapshot); }
  catch(e) { if(e.status===401) throw e; warning='Аналітика тимчасово недоступна'; }
  try {published=await drive.readPublishedAnalytics(snapshot);}
  catch(e) {if(e.status===401)throw e;warning='Snapshot статистики тимчасово недоступний; показано перевірений архів';}
  if(current!==generation || snapshot!==activeSnapshot) return;
  for(const [id,post] of pendingCritical) {
    const a=assessment(analysis,id);
    if(a){if(a.critical?.value===true)control.observePosts([{...post,critical:true}]);pendingCritical.delete(id);}
  }
  if(analysis!==currentAnalysis || warning!==analysisWarning || published!==publishedAnalytics) {
    publishedAnalytics=published;
    analysisWarning=warning;
    update(snapshot,analysis,warning);
    if(booted && (location.hash==='#/analytics' || !readingOlder())) redraw();
  }
}
async function refresh() {
  if (busy || !ArgusCloud.authenticated) return;
  busy = true; const current = generation;
  void ArgusCloud.keywords.refresh();
  if(location.hash==='#/comments') void comments.refresh().catch(e=>{status.textContent=e.message;});
  try {
    const revision=sourceRevision;
    const details=Date.now()-lastDetails>=60000;
    const snapshot=await drive.readArchive();
    if (current !== generation) return;
    if(snapshot!==activeSnapshot) {
      const ids=new Set(activeSnapshot.items.map(p=>p.id));
      const fresh=snapshot.items.filter(p=>!ids.has(p.id));
      if(control.preferences().notifications.priority==='critical') {
        for(const p of fresh)pendingCritical.set(p.id,p);
        while(pendingCritical.size>1000)pendingCritical.delete(pendingCritical.keys().next().value);
      } else control.observePosts(fresh);
      pendingCount+=snapshot.items.filter(p=>!p.sample && !ids.has(p.id)).length;
      badge.hidden=pendingCount===0; badge.textContent='Нові публікації · '+pendingCount;
      activeSnapshot=snapshot;
      publishedAnalytics=null;
      update(snapshot,null,'Оцінки звіряються з новими публікаціями');
      if(location.hash==='#/analytics' || !readingOlder()) redraw();
      lastDetails=0;
    }
    if((details || lastDetails===0) && !analysisPending) {
      lastDetails=Date.now(); analysisPending=true;
      void (async()=>{
        if(control.preferences().notifications.errors) {try{control.observeErrors(await drive.readMonitor());}catch{}}
        const latest=await drive.readSources();
        if(current!==generation) return;
        if(revision===sourceRevision && JSON.stringify(latest)!==JSON.stringify(sources)) {
          sources=latest; update(activeSnapshot,currentAnalysis,analysisWarning);
        }
        await refreshAnalysis(activeSnapshot,current);
      })().catch(e=>{if(current===generation) status.textContent=e.message;}).finally(()=>{analysisPending=false;});
    }
  } catch(e) {
    if (current === generation) {
      if ([401,403,404].includes(e.status)) ArgusCloud.logout(e.message);
      else status.textContent = e.message;
    }
  } finally {busy = false;}
}
button.onclick = async () => {
  button.disabled = true; error.textContent = ''; const current = generation;
  try {
    await drive.signIn();
    const [snapshot,rows] = await Promise.all([drive.readArchive(),drive.readSources()]);
    if (current !== generation) return;
    sources=rows; activeSnapshot=snapshot; analysisWarning='';
    update(snapshot,null); ArgusCloud.authenticated = true;
    await control.load();
    update(snapshot,null);
    if(current!==generation)return;
    void ArgusCloud.keywords.refresh();
    document.body.classList.add('cloud-authenticated');
    login.hidden = true; root.hidden = false; toolbar.hidden = false; document.title = 'ARGUS';
    if (!location.hash || location.hash === '#/login' || location.hash === '#/') history.replaceState(null, '', '#'+control.preferences().startPage);
    if (!booted) { await window.ArgusModule(); booted = true; }
    else window.dispatchEvent(new PopStateEvent('popstate'));
    control.apply();
    if (current !== generation) {root.replaceChildren(); return;}
    let lastRefresh=0;
    refreshTimer = setInterval(() => {if (!document.hidden&&Date.now()-lastRefresh>=control.preferences().refreshSeconds*1000){lastRefresh=Date.now();void refresh();}}, 1000);
    await refreshAnalysis(snapshot,current);
  } catch(e) {if (current === generation) ArgusCloud.logout(e.message);}
  finally {button.disabled = !drive.secure || !drive.ready;}
};
document.getElementById('cloud-alerts').onclick=()=>{location.hash='#/notifications';document.getElementById('cloud-alerts').hidden=true;};
document.getElementById('cloud-logout').onclick = () => ArgusCloud.logout();
document.getElementById('cloud-refresh').onclick = refresh;
document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
const menu = document.getElementById('cloud-menu');
menu.onclick = () => menu.setAttribute('aria-expanded', String(document.body.classList.toggle('cloud-menu-open')));
document.addEventListener('click', event => {
  if (event.target.closest('.sidebar a')) {
    document.body.classList.remove('cloud-menu-open'); menu.setAttribute('aria-expanded', 'false');
  }
});
window.addEventListener('hashchange', () => {
  if (location.hash === '#/archive' && ArgusCloud.authenticated) location.assign('./archive/');
  else if (booted) window.dispatchEvent(new PopStateEvent('popstate'));
});
window.addEventListener('pagehide', () => ArgusCloud.logout());
drive.prepareSignIn().then(() => {button.disabled = false;}).catch(e => {error.textContent = e.message;});
