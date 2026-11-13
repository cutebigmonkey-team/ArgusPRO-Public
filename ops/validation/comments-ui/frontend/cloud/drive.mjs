import {createJournal} from './journal.mjs';
import {validatePreferences,defaults} from './preferences.mjs';
import {fileId,validateCatalog,readSnapshot} from './catalog.mjs';
import {validateAnalysis,analysisForArchive} from './analysis.mjs';
const MAX_INDEX=20*1024*1024, MAX_MEDIA=100*1024*1024;
const scope='https://www.googleapis.com/auth/drive.readonly';
const writeScope='https://www.googleapis.com/auth/drive.file';
const submissionName='argus-site-source-v1.json';
const api='https://www.googleapis.com/drive/v3/';
let token='', expires=0, generation=0, oauth, cached=null;
let journal=createJournal();
let registryCache=null, writeToken='', writeExpires=0;
let analysisCache=null;
let descriptiveCache=null;
const controllers=new Set(), urls=new Set();
const config=window.ARGUS_CLOUD_CONFIG || {};
export async function aiRequest(path,options={}) {
  if(!token || Date.now()>=expires)throw new Error('Увійдіть у ARGUS повторно.');
  let base;
  try {base=new URL(config.aiBaseUrl);if(base.protocol!=='https:'||base.username||base.password||base.search||base.hash||base.pathname!=='/')throw new Error();}
  catch {throw new Error('AI-сервер ще не підключено. Надсилання повідомлень стане доступним після його запуску.');}
  if(!/^\/api\/ai\/(?:status|conversations(?:\?id=[a-f0-9]{32})?|chat)$/.test(path))throw new Error('Неприпустимий AI-запит.');
  const response=await fetch(new URL(path,base),{...options,credentials:'omit',redirect:'error',cache:'no-store',
    headers:{Authorization:'Bearer '+token,'Content-Type':'application/json'}});
  if(!response.ok)throw new Error(response.status===403?'Немає доступу до архіву чи діалогу. Увійдіть повторно.':response.status===429?'Ліміт запитів вичерпано. Спробуйте пізніше.':'AI-сервер тимчасово недоступний.');
  return response;
}
export const ready = /^[\w-]+\.apps\.googleusercontent\.com$/.test(config.clientId || '') && fileId(config.folderId);
export const secure = window.isSecureContext && (location.protocol==='https:' || ['localhost','127.0.0.1'].includes(location.hostname));
export function prepareSignIn() {
  if(!secure) return Promise.reject(new Error('Для входу потрібне HTTPS-з’єднання. Власник завершує налаштування сертифіката сайту.'));
  if(!ready) return Promise.reject(new Error('Власник ще не підключив сховище Argus.'));
  if(window.google?.accounts?.oauth2) return Promise.resolve();
  return new Promise((resolve,reject)=>{
    const script=document.createElement('script');script.src='https://accounts.google.com/gsi/client';script.async=true;
    const timeout=setTimeout(()=>reject(new Error('Google-вхід не завантажився. Оновіть сторінку.')),15000);
    script.onload=()=>{clearTimeout(timeout);resolve();};
    script.onerror=()=>{clearTimeout(timeout);reject(new Error('Не вдалося завантажити Google-вхід. Оновіть сторінку.'));};
    document.head.append(script);
  });
}
export function clearSession() {
  analysisCache=null;
  descriptiveCache=null;
  token=''; expires=0; generation++; journal=createJournal(); cached=null; registryCache=null; writeToken=''; writeExpires=0;
  for (const c of controllers) c.abort(); controllers.clear();
  for (const url of urls) URL.revokeObjectURL(url); urls.clear();
}
export function signIn() {
  if (!secure) return Promise.reject(new Error('Вхід дозволений лише через HTTPS.'));
  if (!ready) return Promise.reject(new Error('Власник ще не підключив сховище Argus.'));
  if (!window.google?.accounts?.oauth2) return Promise.reject(new Error('Вхід Google ще не завантажився. Спробуйте знову.'));
  const current=generation;
  return new Promise((resolve,reject)=>{
    oauth=google.accounts.oauth2.initTokenClient({client_id:config.clientId,scope,
      include_granted_scopes:false,
      callback:result=>{
        if (current!==generation) return reject(new Error('Вхід скасовано.'));
        if (result.error || !result.access_token || !google.accounts.oauth2.hasGrantedAllScopes(result,scope)) return reject(new Error('Не надано доступ для читання Google Drive.'));
        token=result.access_token;
        expires=Date.now()+Number(result.expires_in || 3600)*1000-30000;
        resolve();
      },error_callback:()=>reject(new Error('Вхід Google скасовано або вікно заблоковано.'))});
    oauth.requestAccessToken({prompt:'select_account'});
  });
}
async function fetchBytes(path,limit,auth=token,metadata=null,method=metadata?'POST':'GET') {
  if (!token || Date.now()>=expires) { clearSession(); throw Object.assign(new Error('Сеанс завершено. Увійдіть знову.'),{status:401}); }
  const controller=new AbortController(), current=generation;
  controllers.add(controller);
  const timeout=setTimeout(()=>controller.abort(),90000);
  try {
    const response=await fetch(api+path,{method,body:metadata?JSON.stringify(metadata):undefined,
      headers:{Authorization:'Bearer '+auth,...(metadata?{'Content-Type':'application/json'}:{})},cache:'no-store',
      credentials:'omit',redirect:'error',referrerPolicy:'no-referrer',signal:controller.signal});
    if (!response.ok) {
      if (response.status===401) clearSession();
      throw Object.assign(new Error([403,404].includes(response.status)
        ? 'Цей Google-акаунт не має доступу до архіву або файла.' : response.status===429
        ? 'Ліміт Google Drive. Спробуйте пізніше.' : 'Не вдалося прочитати Google Drive.'),{status:response.status});
    }
    if (Number(response.headers.get('Content-Length') || 0)>limit) throw new Error('Файл перевищує ліміт перегляду.');
    const reader=response.body.getReader(), chunks=[]; let total=0;
    while (true) {
      const {done,value}=await reader.read(); if(done) break;
      total+=value.length;
      if(total>limit) { await reader.cancel(); throw new Error('Файл перевищує ліміт перегляду.'); }
      chunks.push(value);
    }
    if (current!==generation) throw new Error('Сеанс завершено.');
    const bytes=new Uint8Array(total); let offset=0;
    for(const chunk of chunks) { bytes.set(chunk,offset); offset+=chunk.length; }
    return bytes;
  } finally { clearTimeout(timeout); controllers.delete(controller); }
}
const json=async path=>JSON.parse(new TextDecoder().decode(await fetchBytes(path,MAX_INDEX)));
async function named(name) {
  // Folder and fixed object names only. Do not enumerate the user's other files.
  const params=new URLSearchParams({q:`'${config.folderId}' in parents and trashed=false and name='${name}'`,
    pageSize:'2',fields:'files(id,size,version),nextPageToken'});
  const result=await json('files?'+params);
  if (result.files?.length>1 || result.nextPageToken) throw new Error('У сховищі дубльований індекс. Зверніться до власника.');
  return result.files?.[0] || null;
}
async function checkFolder() {
  const root=await json('files/'+config.folderId+'?fields=id,mimeType,trashed');
  if(root.trashed || root.mimeType!=='application/vnd.google-apps.folder') throw new Error('Папка архіву недоступна.');
}
async function readBaseArchive() {
  const current=generation;
  await checkFolder();
  const catalogFile=await named('catalog.json');
  if(!catalogFile) {cached=null;return {items:[],packages:[],updated_at:null};}
  if(current===generation && catalogFile.version && cached?.id===catalogFile.id && cached.version===catalogFile.version)
    return cached.value;
  const catalog=validateCatalog(await json('files/'+catalogFile.id+'?alt=media'));
  if(current!==generation) throw new Error('Сеанс завершено.');
  if(cached?.sha===catalog.snapshot_sha256 && cached.id===catalogFile.id) return cached.value;
  const value=await readSnapshot(catalog,(id,limit)=>{
    if(current!==generation) throw new Error('Сеанс завершено.');
    return fetchBytes('files/'+id+'?alt=media',limit);
  },checksum);
  if(current!==generation) throw new Error('Сеанс завершено.');
  value.snapshot_sha256=catalog.snapshot_sha256;
  cached={sha:catalog.snapshot_sha256,id:catalogFile.id,version:catalogFile.version,value};
  return value;
}
export async function readArchive() {
  const read=journal;
  const [base,file]=await Promise.all([readBaseArchive(),named('live-feed.json')]);
  const head=file?await json('files/'+file.id+'?alt=media'):null;
  return read(base,head,async cursor=>{
    const bytes=await fetchBytes('files/'+cursor.id+'?alt=media',MAX_INDEX);
    if(await checksum(bytes)!==cursor.sha) throw new Error('Помилка контрольної суми LIVE.');
    return JSON.parse(new TextDecoder().decode(bytes));
  });
}
export async function readCommentsCatalog() {
  const file=await named('comments-catalog.json');
  if(!file)return null;
  const value=await json('files/'+file.id+'?alt=media');
  if(value.format!=='argus-comments-catalog-v1' || !Array.isArray(value.pages) || value.pages.length>100000)
    throw new Error('Непідтримуваний індекс коментарів.');
  for(const p of value.pages) if(!fileId(p.id) || !/^[a-f0-9]{64}$/.test(p.sha256) ||
    !Number.isInteger(p.size) || p.size<1 || p.size>4*1024*1024 || !Array.isArray(p.platforms) ||
    !Array.isArray(p.sources) || !Array.isArray(p.posts)) throw new Error('Пошкоджений індекс коментарів.');
  return value;
}
export async function readCommentsPage(page) {
  if(!fileId(page.id) || !/^[a-f0-9]{64}$/.test(page.sha256) || !Number.isInteger(page.size) || page.size>4*1024*1024)
    throw new Error('Пошкоджене посилання на коментарі.');
  const bytes=await fetchBytes('files/'+page.id+'?alt=media',page.size);
  if(bytes.length!==page.size || await checksum(bytes)!==page.sha256)throw new Error('Контрольна сума коментарів не збігається.');
  const value=JSON.parse(new TextDecoder().decode(bytes));
  if(value.format!=='argus-comments-page-v1' || !Array.isArray(value.items) || value.items.length!==page.count)
    throw new Error('Пошкоджена сторінка коментарів.');
  return value.items;
}
export function sourceAddress(value) {
  let u;
  try {u=new URL(value);} catch {throw new Error('Вкажіть повну публічну адресу джерела.');}
  if(!['https:','http:'].includes(u.protocol) || u.username || u.password || u.port ||
      value.length>2048 || /\s/.test(value) || !/^(?:[a-z0-9-]+\.)+[a-z]{2,63}$/i.test(u.hostname) ||
      /\.(?:local|localhost|internal|test|invalid)$/.test(u.hostname)) throw new Error('Потрібна публічна адреса сайту або каналу.');
  const h=u.hostname.replace(/^www\./,''), p=u.pathname;
  if([...u.searchParams.keys()].some(k=>/token|secret|password|auth|session|email|api.?key/i.test(k))) throw new Error('Введіть публічне посилання без ключів або даних входу.');
  const hosts={'t.me':'telegram','telegram.me':'telegram','web.telegram.org':'telegram','facebook.com':'facebook',
    'm.facebook.com':'facebook','mbasic.facebook.com':'facebook','youtube.com':'youtube','m.youtube.com':'youtube',
    'youtu.be':'youtube','instagram.com':'instagram','x.com':'x','twitter.com':'x','threads.net':'threads',
    'threads.com':'threads','vk.com':'vk','vk.ru':'vk','ok.ru':'ok','tiktok.com':'tiktok','vm.tiktok.com':'tiktok',
    'vt.tiktok.com':'tiktok','reddit.com':'reddit','rutube.ru':'rutube'};
  if(/(^|\.)(?:whatsapp\.(?:net|com)|max\.ru)$/.test(h)) throw new Error('Для цієї платформи збір через форму поки недоступний.');
  const platform=hosts[h] || (/(^|\.)mail\.ru$/.test(h)?'mailru':'rss');
  if(platform==='telegram') {
    const handle=h==='web.telegram.org'?u.hash.match(/^#@(\w{3,64})$/):p.match(/^\/(?:s\/)?(\w{3,64})\/?$/);
    if(!handle) throw new Error('Вкажіть публічний канал у форматі https://t.me/назва_каналу.');
    u=new URL('https://t.me/'+handle[1].toLowerCase());
  }
  if(platform==='youtube' && p==='/feeds/videos.xml' && !/^UC[\w-]{22}$/.test(u.searchParams.get('channel_id')||'')) throw new Error('Вкажіть посилання на канал YouTube.');
  u.hash='';
  for(const k of [...u.searchParams.keys()]) if(/^(utm_|fbclid$|gclid$|yclid$)/i.test(k)) u.searchParams.delete(k);
  const host=u.hostname.replace(/^www\./,'').replace(/^m\.facebook\.com$/,'facebook.com').replace(/^twitter\.com$/,'x.com').replace(/^threads\.net$/,'threads.com');
  const key=host+u.pathname.replace(/\/$/,'')+u.search;
  return {platform,url:u.href,key};
}
function uniqueSources(rows) {
  const seen=new Set();
  return rows.filter(row=>{
    let key;
    try {key=sourceAddress(row.url).key;} catch {key=row.url;}
    if(seen.has(key)) return false;
    seen.add(key); return true;
  });
}
export async function readSources() {
  const current=generation;
  const file=await named('sources.json');
  let registered=[];
  if(file) {
    if(registryCache?.id===file.id && registryCache.version===file.version) registered=registryCache.rows;
    else {
      registered=(await json('files/'+file.id+'?alt=media')).sources;
      if(!Array.isArray(registered)) throw new Error('Не вдалося прочитати реєстр джерел.');
      if(current===generation) registryCache={id:file.id,version:file.version,rows:registered};
    }
  }
  const pending=[];
  const params=new URLSearchParams({q:`'${config.folderId}' in parents and trashed=false and name='${submissionName}' and mimeType='application/json'`,
    orderBy:'createdTime',pageSize:'1000',fields:'nextPageToken,files(id,description,createdTime)'});
  do {
    const page=await json('files?'+params);
    for(const f of page.files||[]) {
      try {
        const r=JSON.parse(f.description), address=sourceAddress(r.url);
        if(typeof r.name==='string' && r.name.trim() && r.name.length<=200 && typeof r.region==='string' && r.region.length<=120 && ['dsns','general'].includes(r.origin))
          pending.push({...address,name:r.name,region:r.region,origin:r.origin,id:'web-'+f.id,enabled:true,createdTime:f.createdTime});
      } catch { /* Ignore malformed submissions. */ }
    }
    if(!page.nextPageToken) break;
    params.set('pageToken',page.nextPageToken);
  } while(true);
  if(current!==generation) throw new Error('Сеанс завершено.');
  return uniqueSources([...registered,...pending.sort((a,b)=>a.createdTime.localeCompare(b.createdTime)||a.id.localeCompare(b.id))]);
}
function authorizeWrite() {
  if(writeToken && Date.now()<writeExpires) return Promise.resolve(writeToken);
  const current=generation;
  return new Promise((resolve,reject)=>{
    google.accounts.oauth2.initTokenClient({client_id:config.clientId,scope:scope+' '+writeScope,include_granted_scopes:false,
      callback:async result=>{
        try {
          if(current!==generation || !result.access_token || !google.accounts.oauth2.hasGrantedAllScopes(result,scope,writeScope))
            throw new Error('Для збереження змін потрібен дозвіл Google на запис файлів ARGUS.');
          const path='about?fields=user(permissionId)';
          const [reader,writer]=await Promise.all([json(path),fetchBytes(path,4096,result.access_token).then(b=>JSON.parse(new TextDecoder().decode(b)))]);
          if(!reader.user?.permissionId || reader.user.permissionId!==writer.user?.permissionId || current!==generation) throw new Error('Виберіть той Google-акаунт, з яким увійшли до ARGUS.');
          writeToken=result.access_token; writeExpires=Date.now()+(Number(result.expires_in||3600)-30)*1000;
          resolve(writeToken);
        } catch(e) {reject(e);}
      },error_callback:()=>reject(new Error('Вікно дозволу Google закрито або заблоковано.'))}).requestAccessToken({prompt:''});
  });
}
export async function submitSource(name,url,region,origin,rows) {
  const address=sourceAddress(url.trim());
  name=name.trim();
  if(!name || name.length>200 || region.length>120) throw new Error('Вкажіть назву до 200 символів і регіон до 120 символів.');
  if(uniqueSources([...rows,address]).length===uniqueSources(rows).length) throw new Error('Ця адреса вже є у ваших джерелах.');
  const auth=await authorizeWrite();
  const data={name,url:address.url,region,origin};
  const bytes=await fetchBytes('files?fields=id,description,parents',8192,auth,
    {name:submissionName,mimeType:'application/json',parents:[config.folderId],description:JSON.stringify(data)});
  const f=JSON.parse(new TextDecoder().decode(bytes));
  if(!fileId(f.id) || !f.parents?.includes(config.folderId) || f.description!==JSON.stringify(data)) throw new Error('Не вдалося підтвердити запис. Оновіть список джерел.');
  return {...data,platform:address.platform,id:'web-'+f.id,enabled:true};
}
const keywordFile = 'argus-keyword-v1.json';
export async function readKeywords() {
  const params = new URLSearchParams({q:`'${config.folderId}' in parents and trashed=false and name='${keywordFile}' and mimeType='application/json'`,
    fields:'nextPageToken,files(id,description)', pageSize:'1000'});
  const rows = [];
  do {
    const page = await json('files?'+params);
    for (const {id,description} of page.files) {
      if (fileId(id) && typeof description === 'string' && description.trim() && description.length <= 120)
        rows.push({id,word:description});
    }
    if (!page.nextPageToken) break;
    params.set('pageToken',page.nextPageToken);
  } while (true);
  return rows;
}
// A separate Drive file per word lets unrelated edits proceed independently.
export async function writeKeyword(id, word) {
  if (id && !fileId(id)) throw new Error('Невідоме ключове слово.');
  if ((!id || word !== null) && (!word?.trim() || word.length > 120))
    throw new Error('Введіть слово або фразу до 120 символів.');
  const auth = await authorizeWrite();
  const metadata = word === null ? {trashed:true} : {description:word};
  if (!id) Object.assign(metadata,{name:keywordFile,mimeType:'application/json',parents:[config.folderId]});
  const path = (id ? 'files/'+id : 'files')+'?fields=id,description,trashed';
  const bytes = await fetchBytes(path,8192,auth,metadata,id?'PATCH':'POST');
  const saved = JSON.parse(new TextDecoder().decode(bytes));
  if (!fileId(saved.id) || (id && saved.id !== id) ||
      (word === null ? !saved.trashed : saved.description !== word || saved.trashed))
    throw new Error('Не вдалося підтвердити зміну. Оновіть список слів.');
  return {id:saved.id,word};
}
export async function readMonitor() {
  await checkFolder();
  const [file,liveFile]=await Promise.all([named('monitor.json'),named('live-health.json')]);
  const live=liveFile?await json('files/'+liveFile.id+'?alt=media'):null;
  if(!file) return live?{stages:[],live,updated_at:live.updated_at}:null;
  const report=await json('files/'+file.id+'?alt=media');
  if(report.format!=='argus-monitor-v1' || !Array.isArray(report.stages)) throw new Error('Невірний звіт моніторингу.');
  return {...report,live};
}
// TV metadata stays in the existing private archive. Media never passes through Drive.
export async function readTvHealth() {
  const file=await named('tv-health.json');
  return file?JSON.parse(new TextDecoder().decode(await fetchBytes('files/'+file.id+'?alt=media',1024*1024))):null;
}
export async function readTvIntelligence() {
  const file=await named('tv-intelligence.json');if(!file)return [];
  const data=JSON.parse(new TextDecoder().decode(await fetchBytes('files/'+file.id+'?alt=media',2*1024*1024)));
  if(data.format!=='argus-tv-intelligence-v1'||!Array.isArray(data.items))throw new Error('Невірний індекс TV Intelligence');
  return data.items.slice(0,500).filter(r=>typeof r.text==='string'&&r.text.length<=4000&&typeof r.channelId==='string'&&Number.isFinite(Date.parse(r.timestamp)));
}
async function tvPreferenceFile(auth) {
  // Per-user root file: never put personal walls in the shared archive folder.
  const params=new URLSearchParams({q:"trashed=false and 'me' in owners and appProperties has { key='argusTV' and value='v1' }",
    pageSize:'2',fields:'files(id,description),nextPageToken'});
  const page=JSON.parse(new TextDecoder().decode(await fetchBytes('files?'+params,32768,auth)));
  if(page.nextPageToken||page.files?.length>1)throw new Error('У Drive є дублікати налаштувань TV; узгодьте їх перед збереженням.');
  return page.files?.[0]||null;
}
export async function loadTvPreferences() {
  const auth=await authorizeWrite(),file=await tvPreferenceFile(auth);
  return file?.description?JSON.parse(file.description):null;
}
export async function saveTvPreferences(preferences) {
  const description=JSON.stringify(preferences);if(description.length>12000)throw new Error('Забагато збережених конфігурацій TV.');
  const auth=await authorizeWrite(),file=await tvPreferenceFile(auth);
  const metadata=file?{description}:{name:'ARGUS TV settings.json',mimeType:'application/json',description,appProperties:{argusTV:'v1'}};
  const path=(file?'files/'+file.id:'files')+'?fields=id,description';
  const result=JSON.parse(new TextDecoder().decode(await fetchBytes(path,32768,auth,metadata,file?'PATCH':'POST')));
  if(!fileId(result.id)||result.description!==description)throw new Error('Не вдалося підтвердити збереження TV.');
  return preferences;
}

// Personal documents live in the signed-in user's My Drive, never the shared archive.
const personalName='argus-user-settings-v1.json';
export async function readIdentity() {
  const [about,folder]=await Promise.all([json('about?fields=user(permissionId,displayName,emailAddress,photoLink)'),
    json('files/'+config.folderId+'?fields=owners(permissionId),capabilities(canAddChildren),trashed')]);
  if(!about.user?.permissionId||folder.trashed)throw new Error('Не вдалося підтвердити обліковий запис.');
  return {...about.user,role:folder.owners?.some(o=>o.permissionId===about.user.permissionId)?'owner':folder.capabilities?.canAddChildren?'editor':'reader'};
}
async function personalFile() {
  const q=new URLSearchParams({q:`'me' in owners and trashed=false and name='${personalName}'`,
    fields:'files(id,description,ownedByMe,shared,version),nextPageToken',pageSize:'2'});
  const r=await json('files?'+q);
  if(r.nextPageToken||r.files?.length>1)throw new Error('Знайдено дублікати особистих налаштувань у Drive.');
  const f=r.files?.[0];
  if(f&&(!f.ownedByMe||f.shared))throw new Error('Особистий файл має бути приватним і належати вам.');
  return f;
}
export async function readPreferences() {
  const f=await personalFile();
  return {value:f?validatePreferences(JSON.parse(f.description)):defaults(),version:f?.version||null};
}
export async function savePreferences(value,version) {
  value=validatePreferences(value);
  const auth=await authorizeWrite();
  const f=await personalFile();
  if((f?.version||null)!==version)throw new Error('Налаштування змінені в іншій вкладці. Оновіть сторінку.');
  const description=JSON.stringify(value);
  const result=JSON.parse(new TextDecoder().decode(await fetchBytes('files'+(f?'/'+f.id:'')+'?fields=id,version',4096,auth,
    f?{description}:{name:personalName,mimeType:'application/json',description},f?'PATCH':'POST')));
  const saved=await personalFile();
  if(saved?.id!==result.id||saved.description!==description)throw new Error('Запис не підтверджено. Оновіть налаштування.');
  return {value,version:saved.version};
}
export async function readControlState() {
  const identity=await readIdentity();
  if(identity.role!=='owner')throw Object.assign(new Error('Потрібні права власника архіву.'),{status:403});
  const file=await named('control-locator.json');
  if(!file)return null;
  const locator=await json('files/'+file.id+'?alt=media');
  if(locator.format!=='argus-control-locator-v1'||!fileId(locator.id))throw new Error('Непідтримуваний покажчик керування.');
  const metadata=await json('files/'+locator.id+'?fields=ownedByMe,shared,trashed');
  if(!metadata.ownedByMe||metadata.shared||metadata.trashed)throw new Error('Стан керування має бути приватним файлом власника.');
  const value=await json('files/'+locator.id+'?alt=media');
  if(value.format!=='argus-control-state-v1')throw new Error('Непідтримуваний стан керування.');
  return value;
}
export async function submitControl(command) {
  const identity=await readIdentity();
  if(identity.role!=='owner')throw Object.assign(new Error('Лише власник архіву може змінювати системні параметри.'),{status:403});
  const auth=await authorizeWrite();
  const description=JSON.stringify({format:'argus-control-command-v1',...command});
  if(description.length>16000)throw new Error('Команда завелика.');
  const bytes=await fetchBytes('files?fields=id,description',20000,auth,{name:'argus-control-command-v1.json',
    mimeType:'application/json',parents:[config.folderId],description});
  const result=JSON.parse(new TextDecoder().decode(bytes));
  if(!fileId(result.id)||result.description!==description)throw new Error('Передавання команди не підтверджено.');
  // Receipt is NOT application: the UI must wait for the worker's durable result.
  return result.id;
}
export async function readAnalysis(snapshot=cached?.value) {
  const current=generation;
  if(!snapshot) return null;
  const pointerFile = await named('typesafe-catalog.json');
  if (!pointerFile) return null;
  const pointer = await json('files/'+pointerFile.id+'?alt=media');
  if (pointer.format !== 'argus-typesafe-catalog-v1' || !fileId(pointer.id) || !/^[a-f0-9]{64}$/.test(pointer.sha256))
    throw new Error('Пошкоджений індекс аналітики.');
  let result=analysisCache?.digest===pointer.sha256?analysisCache.raw:null;
  if(!result) {
    const bytes = await fetchBytes('files/'+pointer.id+'?alt=media',MAX_INDEX);
    if (await checksum(bytes) !== pointer.sha256) throw new Error('Не збігається контрольна сума аналітики.');
    result = validateAnalysis(JSON.parse(new TextDecoder().decode(bytes)));
  }
  if(result.snapshot_sha256!==pointer.snapshot_sha256) throw new Error('Індекс аналітики посилається на іншу версію архіву.');
  if(analysisCache?.digest===pointer.sha256 && analysisCache.snapshot===snapshot) return analysisCache.value;
  const analysis=await analysisForArchive(result,snapshot,checksum);
  if(current!==generation) throw new Error('Сеанс завершено.');
  analysisCache={digest:pointer.sha256,raw:result,snapshot,value:analysis};
  return analysis;
}
export async function readPublishedAnalytics(snapshot) {
  const current=generation;
  const file=await named('analytics-catalog.json');
  if(!file)return null;
  const pointer=await json('files/'+file.id+'?alt=media');
  if(pointer.format!=='argus-analytics-catalog-v1' || !fileId(pointer.id) || !/^[a-f0-9]{64}$/.test(pointer.sha256))
    throw new Error('Пошкоджений snapshot статистики.');
  if(pointer.base_sha256!==snapshot.snapshot_sha256 || pointer.live_sequence!==(snapshot.liveSequence||snapshot.live_checkpoint?.last||0) ||
     pointer.live_sha256!==(snapshot.live_sha256||snapshot.live_checkpoint?.sha||null))return null;
  if(descriptiveCache?.digest===pointer.sha256)return descriptiveCache.value;
  const bytes=await fetchBytes('files/'+pointer.id+'?alt=media',MAX_INDEX);
  if(await checksum(bytes)!==pointer.sha256)throw new Error('Не збігається контрольна сума статистики.');
  const value=JSON.parse(new TextDecoder().decode(bytes));
  if(value.format!=='argus-analytics-v1' || !value.summaries || value.base_sha256!==pointer.base_sha256 || value.live_sequence!==pointer.live_sequence || value.live_sha256!==pointer.live_sha256)
    throw new Error('Неповний snapshot статистики.');
  if(current!==generation)throw new Error('Сеанс завершено.');
  descriptiveCache={digest:pointer.sha256,value};return value;
}
export async function checksum(bytes) {
  return Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',bytes)),b=>b.toString(16).padStart(2,'0')).join('');
}
export async function attachment(file,preview=false) {
  if(!fileId(file.drive_id) || file.size>MAX_MEDIA) throw new Error('Для файла понад 100 МіБ скористайтеся Google Drive.');
  const current=generation;
  const bytes=await fetchBytes('files/'+file.drive_id+'?alt=media',Math.min(MAX_MEDIA,file.size));
  if(bytes.length!==file.size || await checksum(bytes)!==file.sha256) throw new Error('Не збігається контрольна сума вкладення.');
  if(current!==generation) throw new Error('Сеанс завершено.');
  let type='application/octet-stream';
  if(preview) {
    if(bytes[0]===137 && bytes[1]===80 && bytes[2]===78 && bytes[3]===71) type='image/png';
    else if(bytes[0]===255 && bytes[1]===216 && bytes[2]===255) type='image/jpeg';
    else if(new TextDecoder().decode(bytes.slice(4,8))==='ftyp') type='video/mp4';
    else if(new TextDecoder().decode(bytes.slice(0,6))==='WEBVTT') type='text/vtt';
  }
  const url=URL.createObjectURL(new Blob([bytes],{type})); urls.add(url);
  return {url,type};
}
export function release(url) {if(urls.delete(url)) URL.revokeObjectURL(url);}
