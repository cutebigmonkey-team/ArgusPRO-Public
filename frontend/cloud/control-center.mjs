import {defaults,validatePreferences,formatDate,matchingPosts,tabs} from './preferences.mjs';
import {helpArticles} from './help.mjs';
import {interfaceEnglish} from './interface-language.mjs';
const el=(tag,text='',cls='')=>{const n=document.createElement(tag);n.textContent=text;n.className=cls;return n;};
const labels=['Всі пости','ДСНС ресурси','Джерела','Ключові слова','Аналітика','Коментарі','Карти','Телебачення','ARGUS_AI','Аналітична довідка','Налаштування'];
const english=['All posts','DSNS resources','Sources','Keywords','Analytics','Comments','Maps','Television','ARGUS_AI','Analytical reports','Settings'];
const dictionary=Object.fromEntries([...labels.map((v,i)=>[v,english[i]]),
  ['Персоналізація','Personalization'],['Профіль','Profile'],['Довідка','Help'],['Вийти','Sign out'],['Пости','Posts'],
  ['Оновити','Refresh'],['Завантаження…','Loading…'],['Згенерувати довідку','Generate report'],['Фільтри','Filters'],
  ['Усі платформи','All platforms'],['Усі регіони','All regions'],['Усі джерела','All sources'],['Застосувати','Apply'],
  ['Скасувати','Cancel'],['Зберегти','Save'],['Скинути','Reset'],['Пошук','Search'],['Попередня','Previous'],['Наступна','Next'],
  ['Пошук за текстом публікації...','Search publication text...'],['Оберіть період','Select period'],['Додати джерело','Add source'],
  ['Назва','Name'],['Платформа','Platform'],['Регіон','Region'],['Статус','Status'],['Активні','Active'],['Неактивні','Inactive']]);

export function createControlCenter(drive,{onChange=()=>{},getSnapshot=()=>null,logout=()=>{}}={}) {
  let preferences=defaults(),version=null,identity=null,epoch=0,observer=null,pageEpoch=0,system=null;
  let deliveredAt=0,seenErrors=new Set(),noticeQueue=[],timer=null,audioContext=null,saving=false;
  const t=(uk,en)=>preferences.language==='en'?en:uk;
  const tr=text=>preferences.language==='en'?(dictionary[text]||interfaceEnglish[text]||text):text;
  const button=(text,action)=>{const b=el('button',text,'btn btn--secondary');b.type='button';b.onclick=action;return b;};
  const link=(text,href)=>{const a=el('a',text,'btn btn--secondary');a.href=href;return a;};
  function field(parent,title,value,options=null,type='text') {
    const label=el('label','','control-field');label.append(el('span',title));
    const input=el(options?'select':'input');
    input.setAttribute('aria-label',title);
    if(options)for(const option of options){const [v,n]=Array.isArray(option)?option:[option,option];input.add(new Option(n,String(v)));}
    else input.type=type;
    if(type==='checkbox')input.checked=value;else input.value=String(value??'');
    label.append(input);parent.append(label);return input;
  }
  function apply() {
    const p=preferences,html=document.documentElement;
    html.classList.toggle('dark',p.theme==='dark'||p.theme==='system'&&matchMedia('(prefers-color-scheme: dark)').matches);
    html.lang=p.language;html.dataset.density=p.density;html.dataset.images=p.images;html.dataset.videos=p.videos;html.dataset.previews=p.previews;
    html.style.setProperty('--blue-600',p.accent);html.style.setProperty('--blue-500',p.accent);
    html.style.setProperty('--blue-700',p.accent);html.style.setProperty('--blue-50','color-mix(in srgb, '+p.accent+' 10%, white)');
    html.style.setProperty('--argus-font-size',p.fontSize+'px');
    decorate();
  }
  function decorate() {
    if(!identity)return;
    const toggle=document.querySelector('.theme-switch');if(toggle){toggle.classList.toggle('theme-switch--on',document.documentElement.classList.contains('dark'));toggle.setAttribute('aria-label',t('Змінити тему','Change theme'));}
    const name=document.querySelector('.sidebar__user-name'),role=document.querySelector('.sidebar__user-role'),avatar=document.querySelector('.sidebar__avatar');
    const display=preferences.name||'Медіа моніторинг';
    if(name&&name.textContent!==display)name.textContent=display;
    const email=identity.emailAddress||'';
    if(role&&role.textContent!==email)role.textContent=email;
    const src=preferences.avatar||identity.photoLink;
    if(avatar&&src&&avatar.querySelector('img')?.getAttribute('src')!==src){const img=el('img');img.src=src;img.alt='';img.referrerPolicy='no-referrer';avatar.replaceChildren(img);}
    if(avatar&&!src){const initials=String(display||'ARGUS').trim().split(/\s+/).slice(0,2).map(word=>Array.from(word)[0]||'').join('').toLocaleUpperCase();if(avatar.textContent!==initials||avatar.querySelector('img'))avatar.textContent=initials;}
    for(const a of document.querySelectorAll('.sidebar__nav a')) {
      const i=preferences.tabOrder.indexOf(a.getAttribute('href')?.replace(/^#/,''));
      if(i>=0&&a.style.order!==String(i))a.style.order=i;
    }
  }
  const media=matchMedia('(prefers-color-scheme: dark)');media.addEventListener('change',()=>{if(identity)apply();});
  document.addEventListener('pointerdown',()=>{if(identity&&preferences.notifications.sound){try{audioContext||=new AudioContext();void audioContext.resume();}catch{}}});
  async function load() {
    const current=++epoch;
    const [user,saved]=await Promise.all([drive.readIdentity(),drive.readPreferences()]);
    if(current!==epoch)return;
    identity=user;preferences=saved.value;version=saved.version;
    if(version===null){const theme=localStorage.getItem('argus-theme');if(['dark','light'].includes(theme))preferences.theme=theme;}
    apply();
    observer?.disconnect();observer=new MutationObserver(decorate);
    observer.observe(document.getElementById('app-root'),{childList:true,subtree:true});
    timer=setInterval(flushNotifications,1000);
  }
  function clear() {
    epoch++;pageEpoch++;observer?.disconnect();observer=null;clearInterval(timer);timer=null;
    identity=null;preferences=defaults();version=null;system=null;seenErrors.clear();noticeQueue=[];deliveredAt=0;
    audioContext?.close();audioContext=null;apply();
  }
  async function save(value,status,submit) {
    if(saving)return;saving=true;
    const current=epoch;submit.disabled=true;status.removeAttribute('role');status.textContent=t('Збереження…','Saving…');
    try {
      const result=await drive.savePreferences(validatePreferences(value),version);
      if(current!==epoch)return;
      const languageChanged=preferences.language!==result.value.language;
      preferences=result.value;version=result.version;apply();onChange();
      status.textContent=t('Збережено й перевірено у вашому приватному Drive.','Saved and verified in your private Drive.');
      if(languageChanged){document.getElementById('cloud-status').textContent=status.textContent;window.dispatchEvent(new PopStateEvent('popstate'));}
    }catch(error){if(current===epoch){status.role='alert';status.textContent=error.message;}}
    finally{saving=false;submit.disabled=false;}
  }
  function personal(main,profile=false) {
    const p=structuredClone(preferences),form=el('form','','settings-card'),grid=el('div','','control-grid');form.append(grid);main.append(form);
    const fields={};
    if(profile) {
      grid.append(el('p',(identity.displayName||'')+' · '+(identity.emailAddress||'')+' · '+identity.role));
      fields.name=field(grid,t('Ім’я в ARGUS','ARGUS display name'),p.name);fields.name.maxLength=100;
      const image=el('img','','control-avatar');image.src=p.avatar||identity.photoLink||'';image.alt=t('Аватар','Avatar');grid.append(image);
      const upload=field(grid,t('Аватар: PNG, JPEG або WebP до 8 КБ','Avatar: PNG, JPEG or WebP up to 8 KB'),'','', 'file');
      upload.accept='image/png,image/jpeg,image/webp';upload.onchange=async()=>{
        const f=upload.files[0];if(!f)return;
        if(f.size>8000||!['image/png','image/jpeg','image/webp'].includes(f.type)){status.textContent=t('Завеликий або непідтримуваний файл.','File too large or unsupported.');upload.value='';return;}
        const reader=new FileReader();reader.onload=()=>{p.avatar=String(reader.result);image.src=p.avatar;};reader.readAsDataURL(f);
      };
      grid.append(button(t('Прибрати власний аватар','Remove custom avatar'),()=>{p.avatar='';image.src=identity.photoLink||'';}));
      grid.append(el('p',t('Вхід через Google. ARGUS не зберігає ваш пароль. Право керування колекторами має власник папки; редактор може додавати джерела.','Google sign-in. ARGUS does not store your password. The folder owner can manage collectors; editors can register sources.')));
      grid.append(link(t('Пароль та захист Google','Google password and security'),'https://myaccount.google.com/security'),
        link(t('Активні Google-сеанси','Active Google sessions'),'https://myaccount.google.com/device-activity'),
        button(t('Вийти з ARGUS','Sign out of ARGUS'),()=>{if(confirm(t('Вийти з поточного сеансу?','Sign out of this session?')))logout();}));
    } else {
      fields.theme=field(grid,t('Тема','Theme'),p.theme,[['system',t('Системна','System')],['light',t('Світла','Light')],['dark',t('Темна','Dark')]]);
      fields.accent=field(grid,t('Акцентний колір','Accent colour'),p.accent,null,'color');
      fields.language=field(grid,t('Мова','Language'),p.language,[['uk','Українська'],['en','English']]);
      fields.fontSize=field(grid,t('Розмір шрифту','Font size'),p.fontSize,[12,14,16,18]);
      fields.density=field(grid,t('Щільність стрічки','Feed density'),p.density,[['standard',t('Стандартна','Standard')],['compact',t('Компактна','Compact')]]);
      fields.pageSize=field(grid,t('Публікацій на сторінці','Posts per page'),p.pageSize,[10,20,50,100]);
      fields.refreshSeconds=field(grid,t('Оновлення стрічки, секунд','Feed refresh, seconds'),p.refreshSeconds,[7,15,30,60]);
      fields.feedKeyword=field(grid,t('Постійний фільтр стрічки (порожньо — всі)','Persistent feed keyword (empty means all)'),p.feedKeyword);fields.feedKeyword.maxLength=200;
      for(const [key,uk,en] of [['images','Зображення','Images'],['videos','Відео','Video'],['previews','Текстові прев’ю','Text previews']])fields[key]=field(grid,t(uk,en),p[key],null,'checkbox');
      fields.startPage=field(grid,t('Стартова сторінка','Start page'),p.startPage,tabs.map((v,i)=>[v,t(labels[i],english[i])]));
      fields.timezone=field(grid,t('Часова зона IANA','IANA time zone'),p.timezone);fields.timezone.setAttribute('list','argus-timezones');
      const zones=el('datalist');zones.id='argus-timezones';for(const z of ['Europe/Kyiv','Europe/London','UTC','America/New_York','Asia/Tokyo'])zones.append(new Option(z,z));grid.append(zones);
      fields.dateFormat=field(grid,t('Формат дати','Date format'),p.dateFormat,[['short',t('Короткий','Short')],['long',t('Повний','Long')],['iso','ISO']]);
      const order=el('section');order.append(el('h2',t('Порядок вкладок у групах меню','Tab order within menu groups')));form.append(order);
      const drawOrder=()=>{order.querySelectorAll('.control-order').forEach(n=>n.remove());p.tabOrder.forEach((path,i)=>{
        const row=el('div','','control-order');row.append(el('span',t(labels[tabs.indexOf(path)],english[tabs.indexOf(path)])));
        for(const [delta,mark] of [[-1,'↑'],[1,'↓']]){const b=button(mark,()=>{[p.tabOrder[i],p.tabOrder[i+delta]]=[p.tabOrder[i+delta],p.tabOrder[i]];drawOrder();});b.disabled=i+delta<0||i+delta>=p.tabOrder.length;b.setAttribute('aria-label',mark+' '+t(labels[tabs.indexOf(path)],english[tabs.indexOf(path)]));row.append(b);}order.append(row);
      });};drawOrder();
    }
    const status=el('p','','control-status');status.setAttribute('aria-live','polite');const actions=el('div','','control-actions');
    const submit=button(t('Зберегти','Save'),()=>form.requestSubmit());actions.append(submit);
    if(!profile)actions.append(button(t('Відновити стандартні параметри','Restore defaults'),async()=>{
      if(confirm(t('Відновити параметри інтерфейсу? Профіль і сповіщення збережуться.','Restore interface defaults? Profile and notifications will be preserved.'))){await save({...defaults(),name:p.name,avatar:p.avatar,notifications:p.notifications},status,submit);if(status.role!=='alert')personalReload(main);}
    }));
    form.append(actions,status);form.onsubmit=async e=>{e.preventDefault();for(const [key,input] of Object.entries(fields))p[key]=input.type==='checkbox'?input.checked:['fontSize','pageSize','refreshSeconds'].includes(key)?Number(input.value):input.value;await save(p,status,submit);};
  }
  function personalReload(main){main.replaceChildren();personal(main);}
  function notificationForm(main) {
    const form=el('form','','settings-card'),grid=el('div','','control-grid');form.append(grid);main.append(form);
    form.prepend(el('p',t('Повідомлення працюють, поки ARGUS відкритий і ви авторизовані.','Notifications run while ARGUS is open and you are signed in.')));
    const n=preferences.notifications,fields={};
    for(const [key,uk,en] of [['enabled','Нові публікації','New publications'],['errors','Помилки колекторів','Collector errors'],['sound','Звуковий сигнал','Sound'],['push','Браузерні повідомлення','Browser notifications']])fields[key]=field(grid,t(uk,en),n[key],null,'checkbox');
    fields.keywords=field(grid,t('Ключові слова через кому','Keywords separated by commas'),n.keywords.join(', '));
    fields.sources=field(grid,t('ID джерел через кому (порожньо — всі)','Source IDs separated by commas (empty means all)'),n.sources.join(', '));
    fields.frequency=field(grid,t('Мінімальний інтервал, секунд','Minimum interval, seconds'),n.frequency,[0,30,60,300,900]);
    fields.priority=field(grid,t('Пріоритет','Priority'),n.priority,[['all',t('Усі','All')],['critical',t('Лише підтверджені критичні','Verified critical only')]]);
    const status=el('p','','control-status');status.setAttribute('aria-live','polite');
    const permission=button(t('Дозволити браузерні повідомлення','Allow browser notifications'),async()=>{try{status.textContent='Notification' in window?await Notification.requestPermission():t('Не підтримується браузером','Not supported by this browser');}catch(e){status.textContent=e.message;}});
    const sound=button(t('Перевірити звук','Test sound'),()=>{try{beep();status.textContent=t('Сигнал надіслано аудіопристрою.','Signal sent to the audio device.');}catch(e){status.textContent=e.message;}});
    const submit=button(t('Зберегти','Save'),()=>form.requestSubmit());const actions=el('div','','control-actions');actions.append(permission,sound,submit);form.append(actions,status);
    form.onsubmit=async e=>{e.preventDefault();const next={};for(const [key,f] of Object.entries(fields))next[key]=f.type==='checkbox'?f.checked:['keywords','sources'].includes(key)?f.value.split(',').map(v=>v.trim()).filter(Boolean):key==='frequency'?Number(f.value):f.value;
      if(next.push&&(!('Notification' in window)||Notification.permission!=='granted')){status.role='alert';status.textContent=t('Спершу надайте браузерний дозвіл.','Grant browser permission first.');return;}
      if(next.sound)beep();await save({...preferences,notifications:next},status,submit);};
  }
  function beep(){audioContext||=new AudioContext();void audioContext.resume();const o=audioContext.createOscillator(),g=audioContext.createGain();g.gain.value=.08;o.connect(g);g.connect(audioContext.destination);o.start();o.stop(audioContext.currentTime+.15);}
  function flushNotifications() {
    if(!identity||!noticeQueue.length||Date.now()-deliveredAt<preferences.notifications.frequency*1000)return;
    const count=noticeQueue.length;noticeQueue=[];deliveredAt=Date.now();
    const text=t('Нових сповіщень: ','New notifications: ')+count;
    const badge=document.getElementById('cloud-alerts');if(badge){badge.hidden=false;badge.textContent=text;}
    if(preferences.notifications.sound){try{beep();}catch{}}
    if(preferences.notifications.push&&'Notification' in window&&Notification.permission==='granted')new Notification('ARGUS',{body:text,tag:'argus-updates'});
  }
  function observePosts(posts){noticeQueue.push(...matchingPosts(posts,preferences.notifications).map(p=>p.id));flushNotifications();}
  function observeErrors(report) {
    const rows=report?.sources||Object.entries(report?.live?.sources||{}).map(([id,r])=>({id,last_checked_at:r.checked,status:r.status,failures:r.status&&!['ok','no_new_posts','no_public_posts'].includes(r.status)?1:0}));
    for(const r of rows){const key=r.id+':'+r.last_checked_at+':'+r.status;if(r.failures>0&&!seenErrors.has(key)&&preferences.notifications.errors)noticeQueue.push(key);seenErrors.add(key);}
    if(seenErrors.size>10000)seenErrors=new Set([...seenErrors].slice(-5000));flushNotifications();
  }
  async function command(action,patch,status,submit) {
    const current=epoch,revision=system.revision;submit.disabled=true;status.role='status';
    try {
      const id=await drive.submitControl({action,revision,...(patch?{patch}:{})});
      status.textContent=t('Команда збережена. Очікує застосування worker; це ще не успішна зміна.','Command saved. Waiting for the worker; the change is not applied yet.');
      for(let i=0;i<30&&current===epoch&&status.isConnected;i++) {
        await new Promise(r=>setTimeout(r,10000));if(current!==epoch||!status.isConnected)return;
        system=await drive.readControlState();const receipt=system?.audit?.find(r=>r.id===id);
        if(receipt){status.role=receipt.status==='applied'?'status':'alert';status.textContent=receipt.status+': '+receipt.detail;return;}
      }
      if(current===epoch&&status.isConnected)status.textContent=t('Підтвердження ще немає. Оновіть діагностику; не надсилайте команду повторно.','No confirmation yet. Refresh diagnostics; do not resend the command.');
    }catch(e){if(current===epoch){status.role='alert';status.textContent=e.message;}}
    finally{submit.disabled=false;}
  }
  async function admin(main,current,section='monitoring') {
    if(identity.role!=='owner'){main.append(el('p',t('Системне керування доступне лише власнику архіву.','System management is restricted to the archive owner.')));return;}
    const message=el('p',t('Завантаження стану…','Loading state…'));main.append(message);
    try {
      const [state,monitor]=await Promise.all([drive.readControlState(),drive.readMonitor()]);if(current!==pageEpoch)return;
      system=state;message.remove();
      const runtime=monitor?.runtime;
      const diag=el('section','','settings-card');diag.append(el('h2',t('Система','System')));
      const snapshot=getSnapshot();
      const facts={frontend:t('Цей інтерфейс виконується','This interface is running'),Google_Drive:t('Читання виконано','Read succeeded'),
        posts:snapshot?.items?.filter(p=>!p.sample).length??null,last_archive_sync:snapshot?.updated_at??null,
        worker_last_seen:runtime?.at??null,last_delivery:runtime?.last_successful_delivery??null,
        active_sources:state?.sources?.filter(r=>r.enabled).length??null,database:state?.database??null,
        worker_errors:runtime?.archive_write_failures_this_session??null};
      diag.append(el('pre',JSON.stringify(facts,null,2)),button(t('Оновити діагностику','Refresh diagnostics'),()=>mount(main.parentElement,'settings')));main.append(diag);
      if(!state){main.append(el('p',t('Worker не публікує протокол керування. Потрібне розгортання відповідного backend; команди недоступні.','The worker does not publish the control protocol. Deploy the matching backend before submitting commands.')));return;}
      main.append(el('p',t('Стан на ','State as of ')+formatDate(state.updated_at,preferences)));
      if(Date.now()-Date.parse(state.updated_at)>900000)main.append(el('p',t('Стан застарів. Перевірте worker перед змінами.','State is stale. Check the worker before making changes.')));
      observeErrors(state);
      const sourceCard=el('section','','settings-card');sourceCard.append(el('h2',t('Моніторинг джерел','Source monitoring')));main.append(sourceCard);
      const select=field(sourceCard,t('Джерело','Source'),state.sources[0]?.id||'',state.sources.map(r=>[r.id,r.name+' · '+r.platform]));
      const editor=el('div');sourceCard.append(editor);
      const drawSource=()=>{
        editor.replaceChildren();const row=state.sources.find(r=>r.id===select.value);if(!row)return;
        const grid=el('div','','control-grid');editor.append(grid);
        const enabled=field(grid,t('Збирати нові публікації','Collect new publications'),row.enabled,null,'checkbox');
        const priority=field(grid,t('Пріоритет −10…10','Priority −10…10'),row.priority,null,'number');priority.min=-10;priority.max=10;
        const interval=field(grid,t('Інтервал перевірки, секунд','Polling interval, seconds'),row.interval,null,'number');interval.min=60;interval.max=86400;
        const retries=field(grid,t('Повторні спроби після помилки','Retries after failure'),row.retry_limit,null,'number');retries.min=0;retries.max=20;
        const delay=field(grid,t('Мінімальна пауза після помилки, секунд','Minimum error backoff, seconds'),row.retry_delay,null,'number');delay.min=60;delay.max=86400;
        editor.append(el('p','ID: '+row.id+' · '+(row.status||t('Не перевірено','Not checked'))+' · '+t('Остання перевірка: ','Last check: ')+(row.last_checked_at?formatDate(row.last_checked_at,preferences):'—')+' · '+t('Останній успіх: ','Last success: ')+(row.last_success_at?formatDate(row.last_success_at,preferences):'—')));
        const status=el('p','','control-status');status.setAttribute('aria-live','polite');
        const submit=button(t('Застосувати до колектора','Apply to collector'),()=>{
          if(!enabled.checked&&!confirm(t('Призупинити збір цього джерела?','Pause collection for this source?')))return;
          if([priority,interval,retries,delay].some(f=>!f.reportValidity()))return;
          void command('update',{sources:{[row.id]:{enabled:enabled.checked,priority:Number(priority.value),interval:Number(interval.value),retry_limit:Number(retries.value),retry_delay:Number(delay.value)}}},status,submit);
        });editor.append(submit,status);
        if(row.failures>0){const retry=button(t('Відновити бюджет повторних спроб','Reset retry budget'),()=>{
          if(confirm(t('Скинути лічильник помилок? Захисні паузи платформи залишаться чинними.','Reset the failure count? Platform cooldowns will remain in force.')))void command('retry_source',{sources:{[row.id]:{}}},status,retry);
        });editor.append(retry);}
      };select.onchange=drawSource;drawSource();
      const adapters=el('details');adapters.append(el('summary',t('Стан підключених платформ','Connected platform status')),
        el('pre',JSON.stringify(state.platforms,null,2)),el('p',t('Увімкнений адаптер не гарантує доступу до кожного джерела. Дивіться статус конкретного джерела.','An enabled adapter does not guarantee access to every source. Inspect individual source status.')));main.append(adapters);
      const ai=el('section','','settings-card');ai.append(el('h2','AI · TypeSafe'),el('p',(state.ai.model||t('Модель не вказана','Model not specified'))+' · '+t('Ключ налаштований: ','Key configured: ')+state.ai.configured));main.append(ai);
      const aiGrid=el('div','','control-grid');ai.append(aiGrid);
      const model=state.ai.available_models?.length?field(aiGrid,t('Модель аналітики й довідок','Analytics and report model'),state.ai.model,[...new Set([state.ai.model,...state.ai.available_models].filter(Boolean))]):null;
      if(model)ai.append(el('p',t('Поточна версія зберігається, доки ви не виберете іншу. jev-latest і jev-preview — псевдоніми, які провайдер може оновлювати.','The current version is kept until you select another. jev-latest and jev-preview are aliases the provider may update.')));
      if(!model)ai.append(el('p',t('Натисніть «Перевірити API», а потім оновіть діагностику, щоб вибрати модель із підтвердженого списку провайдера.','Test the API, then refresh diagnostics to select a model from the verified provider list.')));
      const aiEnabled=field(aiGrid,t('Автоматична аналітика','Automatic analytics'),state.ai.enabled,null,'checkbox');
      const concurrent=field(aiGrid,t('Одночасних запитів','Concurrent requests'),state.ai.concurrency,[1,2]);
      const count=field(aiGrid,t('Публікацій за цикл','Publications per cycle'),state.ai.max_posts,null,'number');count.min=1;count.max=4;
      const aiInterval=field(aiGrid,t('Інтервал AI, секунд','AI interval, seconds'),state.ai.interval,null,'number');aiInterval.min=300;aiInterval.max=86400;
      ai.append(el('p',t('TypeSafe обслуговує аналітику й оцінки для довідок. Генеративні параметри, чат, DeepSeek, локальні та резервні моделі не підключені до цього worker.','TypeSafe supplies analytics and report assessments. Generation parameters, chat, DeepSeek, local and fallback models are not connected to this worker.')));
      const aiStatus=el('p','','control-status');aiStatus.setAttribute('aria-live','polite');
      const saveAI=button(t('Застосувати AI','Apply AI settings'),()=>{if(count.reportValidity()&&aiInterval.reportValidity())void command('update',{ai:{enabled:aiEnabled.checked,concurrency:Number(concurrent.value),max_posts:Number(count.value),interval:Number(aiInterval.value),...(model&&model.value!==state.ai.model?{model:model.value}:{})}},aiStatus,saveAI);});
      const testAI=button(t('Перевірити API','Test API'),()=>command('test_ai',null,aiStatus,testAI));ai.append(saveAI,testAI,aiStatus);
      const audit=el('details');audit.append(el('summary',t('Журнал адміністративних змін','Administrative audit log')),el('pre',JSON.stringify(state.audit,null,2)));main.append(audit);
      diag.hidden=true;sourceCard.hidden=section==='ai';adapters.hidden=section==='ai';ai.hidden=section!=='ai';
      if(section==='ai') {
        const chat=el('section','','settings-card');chat.append(el('h2','ARGUS_AI · DeepSeek'));main.append(chat);
        const result=el('p');chat.append(result);
        const probe=button(t('Перевірити сервер чату','Check chat server'),async()=>{probe.disabled=true;try{const response=await drive.aiRequest('/api/ai/status');const data=await response.json();result.textContent=JSON.stringify({provider:data.providers?.primary,configured:data.providers?.configured,fallback:data.providers?.fallback,index:data.index},null,2);}catch(e){result.textContent=e.message;}finally{probe.disabled=false;}});chat.append(probe);
      }
    }catch(e){if(current===pageEpoch){message.role='alert';message.textContent=e.message;if(!message.isConnected)main.append(message);main.append(button(t('Повторити','Retry'),()=>{main.replaceChildren();void admin(main,current,section);}));}}
  }
  function help(main) {
    const search=el('input');search.type='search';search.placeholder=t('Пошук у довідці','Search help');search.setAttribute('aria-label',search.placeholder);main.append(search);
    const list=el('div');main.append(list);
    const draw=()=>{list.replaceChildren();const q=search.value.toLocaleLowerCase();for(const [uk,en,bodyUk,bodyEn,path] of helpArticles){const title=t(uk,en),body=t(bodyUk,bodyEn);if(!(title+' '+body).toLocaleLowerCase().includes(q))continue;
      const d=el('details');d.append(el('summary',title),el('p',body),link(t('Відкрити розділ','Open section'),'#'+path));list.append(d);}if(!list.children.length)list.append(el('p',t('Нічого не знайдено','No results')));};search.oninput=draw;draw();
  }
  async function overview(main,section,current) {
    const card=el('section','','settings-card');main.append(card);
    card.append(el('h2',section==='archive'?t('Архів','Archive'):section==='collectors'?t('Колектори','Collectors'):t('Система','System')));
    const status=el('p',t('Завантаження…','Loading…'));card.append(status);
    try {
      const [monitor,state]=await Promise.all([drive.readMonitor(),section==='collectors'&&identity.role==='owner'?drive.readControlState():null]);if(current!==pageEpoch)return;
      const snapshot=getSnapshot(),runtime=monitor?.runtime||monitor?.live?.runtime;
      status.textContent=t('Google Drive: читання підтверджено.','Google Drive: read verified.');
      if(section==='archive') {
        card.append(el('p',t('Записів у поточному архіві: ','Records in current archive: ')+(snapshot?.items?.length??'—')),
          el('p',t('Остання синхронізація: ','Last synchronization: ')+(snapshot?.updated_at?formatDate(snapshot.updated_at,preferences):'—')),
          el('p',t('Автоматичне видалення архівних публікацій не налаштоване. Існуючий архів зберігається; ця сторінка не видаляє дані.','Automatic deletion of archived publications is not configured. The existing archive is retained; this page does not delete data.')),
          link(t('Відкрити архів','Open archive'),'./archive/'));
      } else if(section==='collectors') {
        const platforms=['telegram','rss','facebook','instagram','x','threads','tiktok','youtube','vk','ok','max','reddit','gdelt'];
        const rows=state?.sources||monitor?.sources||Object.values(monitor?.live?.sources||{});
        const table=el('table'),head=el('tr');for(const label of [t('Платформа','Platform'),t('Перевірених джерел','Checked sources'),t('Останній стан','Latest status')])head.append(el('th',label));table.append(head);
        for(const platform of [...new Set([...platforms,...rows.map(r=>r.platform).filter(Boolean)])]) {
          const matches=rows.filter(r=>r.platform===platform),summary=monitor?.coverage?.platforms?.[platform],tr=el('tr');tr.append(el('td',platform.toUpperCase()),el('td',String(summary?.registered??matches.length)),el('td',summary?t('Успішно за 5 хв: ','Successful in 5 min: ')+summary.successful_sources_5m+' · '+t('Нових за годину: ','New in 1 hour: ')+(summary.new_posts_1h??'—'):[...new Set(matches.map(r=>r.status||r.collector_status).filter(Boolean))].join(', ')||t('Немає даних у звіті','No data in report')));table.append(tr);
        }
        const wrap=el('div','','control-table');wrap.append(table);card.append(wrap,link(t('Повний моніторинг','Full monitoring'),'./monitor/'));
      } else {
        card.append(el('pre',JSON.stringify({frontend:'running',build:window.ARGUS_CLOUD_CONFIG?.buildSha||'unavailable',archive:snapshot?.updated_at||null,worker:runtime||null},null,2)));
        const errors=(monitor?.stages||[]).filter(r=>!['ok','success','completed'].includes(r.status));
        const details=el('details');details.append(el('summary',t('Журнал станів і помилок','Status and error log')),el('pre',JSON.stringify(errors,null,2)));card.append(details);
      }
    }catch(error){if(current===pageEpoch){status.role='alert';status.textContent=error.message;}}
  }
  function settings(main,current) {
    const nav=el('nav');nav.setAttribute('aria-label',t('Секції налаштувань','Settings sections'));main.append(nav);
    const panel=el('div');main.append(panel);
    const draw=key=>{panel.replaceChildren();const revision=++pageEpoch;
      for(const b of nav.children)b.setAttribute('aria-pressed',String(b.dataset.section===key));
      if(key==='general')personal(panel);else if(key==='profile')personal(panel,true);else if(key==='notifications')notificationForm(panel);
      else if(['archive','collectors','system'].includes(key))void overview(panel,key,revision);
      else void admin(panel,revision,key);
    };
    for(const [key,uk,en] of [['general','Загальні','General'],['profile','Профіль','Profile'],['monitoring','Моніторинг','Monitoring'],['collectors','Колектори','Collectors'],['ai','AI','AI'],['notifications','Сповіщення','Notifications'],['archive','Архів','Archive'],['system','Система','System']]){const b=button(t(uk,en),()=>draw(key));b.dataset.section=key;nav.append(b);}
    draw('general');
  }
  function mount(main,section='settings') {
    const current=++pageEpoch;main.replaceChildren();main.classList.add('control-center');
    const heading={personalization:['Персоналізація','Personalization'],profile:['Профіль','Profile'],settings:['Налаштування','Settings'],help:['Довідка','Help'],notifications:['Сповіщення','Notifications']}[section];
    const title=el('h1',t(...heading));title.tabIndex=-1;main.append(title);title.focus();
    const nav=el('nav');nav.setAttribute('aria-label',t('Центр керування','Control center'));
    for(const [key,uk,en] of [['personalization','Персоналізація','Personalization'],['profile','Профіль','Profile'],['settings','Налаштування','Settings'],['help','Довідка','Help'],['notifications','Сповіщення','Notifications']])nav.append(link(t(uk,en),'#/'+key));main.append(nav);
    const content=el('div');main.append(content);
    if(!identity){content.append(el('p',t('Профіль не завантажено. Увійдіть повторно.','Profile is not loaded. Sign in again.')));return;}
    if(section==='personalization')personal(content);else if(section==='profile')personal(content,true);else if(section==='help')help(content);else if(section==='notifications')notificationForm(content);else settings(content,current);
  }
  return {load,clear,mount,apply,translate:tr,observePosts,observeErrors,preferences:()=>preferences,
    formatDate:(value,dateOnly=false)=>formatDate(value,preferences,dateOnly),
    async toggleTheme(){const s=document.getElementById('cloud-status');const b=document.querySelector('.theme-switch');if(b)await save({...preferences,theme:document.documentElement.classList.contains('dark')?'light':'dark'},s,b);}};
}
