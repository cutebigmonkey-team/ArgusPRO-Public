import {LAYOUTS,REGIONS,normalizePreferences,matchesChannel,validateCatalog,mergeHealth} from './tv-core.mjs';
import {TVPlayer} from './tv-player.mjs';
const el=(tag,cls='',text='')=>{const n=document.createElement(tag);n.className=cls;n.textContent=text;return n;};
const read=(key,fallback)=>{try{return JSON.parse(localStorage.getItem(key))||fallback;}catch{return fallback;}};
const store=(key,value)=>{try{localStorage.setItem(key,JSON.stringify(value));return true;}catch{return false;}};
const button=(label,action,cls='')=>{const n=el('button',cls,label);n.type='button';n.onclick=action;return n;};
const link=(text,url)=>{const n=el('a','tv-link',text);n.href=url;n.target='_blank';n.rel='noopener noreferrer';return n;};
export function createTelevision(drive,{Player=TVPlayer,storageKey='argus-tv-v1'}={}) {
  const key=storageKey,goodKey=storageKey==='argus-tv-v1'?'argus-tv-last-good-v1':storageKey+'-last-good';
  let main,channels=[],prefs,epoch=0,activeSlot=0,audioSlot=-1,region='Усі країни',query='',wall,list,count,note,regionNav,layoutButtons,presetSelect;
  let refreshTimer,observer,cloudBusy=false;const players=new Map(),tiles=new Map(),states=new Map();
  let lastGood=read(goodKey,{});
  const alive=()=>main?.isConnected;
  function save(){if(!store(key,prefs)&&note)note.textContent='Браузер не дозволяє зберегти налаштування.';}
  function syncAudio(){for(const [slot,player] of players)player.setAudio(slot!==audioSlot,prefs.volume);
    for(const [slot,tile] of tiles){const b=tile.querySelector('[data-audio]');if(b){b.textContent=slot===audioSlot?'Звук увімкнено':'Увімкнути звук';b.setAttribute('aria-pressed',String(slot===audioSlot));}}}
  function selectSlot(slot){activeSlot=slot;for(const [i,tile] of tiles){tile.classList.toggle('is-selected',i===slot);tile.querySelector('[data-select]').setAttribute('aria-pressed',String(i===slot));}
    if(note)note.textContent=`Комірка ${slot+1}: виберіть канал у каталозі.`;}
  function setChannel(id){prefs.slots[activeSlot]=id;save();renderSlot(activeSlot);renderCatalog();}
  function favorite(id){prefs.favorites=prefs.favorites.includes(id)?prefs.favorites.filter(x=>x!==id):[...prefs.favorites,id];save();renderCatalog();
    for(const [slot,tile] of tiles){if(prefs.slots[slot]===id){const b=tile.querySelector('[data-favorite]');b.textContent=prefs.favorites.includes(id)?'★':'☆';b.setAttribute('aria-pressed',String(prefs.favorites.includes(id)));}}}
  function updateCounts(){if(!count)return;const live=[...states.values()].filter(s=>s.status==='LIVE').length;
    count.textContent=`${channels.length} каналів · ${live} LIVE у стіні · ${channels.filter(c=>c.sources.length).length} із вбудованим джерелом`;}
  function state(slot,value){states.set(slot,value);const tile=tiles.get(slot);if(!tile)return;
    const badge=tile.querySelector('.tv-status');badge.textContent=value.status;badge.dataset.status=value.status;
    const sourceSelect=tile.querySelector('.tv-source');if(sourceSelect&&value.sourceId)sourceSelect.value=value.sourceId;
    const channel=channels.find(c=>c.id===prefs.slots[slot]),source=channel?.sources.find(s=>s.id===value.sourceId);
    if(source){const audio=tile.querySelector('[data-audio]');if(audio)audio.hidden=source.type==='iframe';
      const q=tile.querySelector('.tv-quality');if(q)q.hidden=['youtube','iframe'].includes(source.type);
      const meta=tile.querySelector('.tv-meta');if(meta)meta.textContent=`${channel.country} · ${channel.language}`+(source.type==='iframe'?' · Звук і якість: у плеєрі мовника':source.type==='youtube'?' · Якість: ⚙ у плеєрі':'');}
    tile.querySelector('.tv-connection').textContent=value.reason||'';
    tile.querySelector('.tv-last-success').textContent=value.lastSuccess?'Останнє підключення: '+new Date(value.lastSuccess).toLocaleTimeString('uk-UA'):'У цій сесії ще не підключено';
    const quality=tile.querySelector('.tv-quality');const options=players.get(slot)?.qualities()||[];
    if(quality&&options.length&&quality.options.length===1)for(const o of options)quality.add(new Option(o.label,o.value));
    updateCounts();}
  function startPlayer(slot,host,channel){const p=new Player({host,channel,preferredSource:lastGood[channel.id]?.sourceId,volume:prefs.volume,
    lastSuccess:lastGood[channel.id]?.at,
    onAudioIntent:()=>{audioSlot=slot;syncAudio();},
    onState:value=>state(slot,value),onSuccess:sourceId=>{lastGood[channel.id]={sourceId,at:new Date().toISOString()};store(goodKey,lastGood);}});
    players.set(slot,p);syncAudio();}
  function renderSlot(slot){
    players.get(slot)?.destroy();players.delete(slot);states.delete(slot);
    const old=tiles.get(slot);if(old)observer?.unobserve(old);
    const tile=el('article','tv-tile');tile.dataset.slot=String(slot);tiles.set(slot,tile);
    if(old)old.replaceWith(tile);else wall.append(tile);
    const header=el('header','tv-tile-header');
    const select=button(String(slot+1).padStart(2,'0'),()=>selectSlot(slot),'tv-slot-number');select.dataset.select='';select.setAttribute('aria-label',`Вибрати комірку ${slot+1}`);header.append(select);
    const channel=channels.find(c=>c.id===prefs.slots[slot]);
    header.append(el('strong','',channel?.name||'Додати канал'));
    const badge=el('span','tv-status','UNKNOWN');header.append(badge);tile.append(header);
    const host=el('div','tv-screen');tile.append(host);
    // Drag only the header: native player controls remain usable.
    header.draggable=true;header.ondragstart=e=>{e.dataTransfer.setData('text/argus-tv-slot',String(slot));};
    tile.ondragover=e=>e.preventDefault();tile.ondrop=e=>{e.preventDefault();const raw=e.dataTransfer.getData('text/argus-tv-slot');if(!/^\d+$/.test(raw))return;
      const from=Number(raw);if(from>=prefs.layout||from===slot)return;
      [prefs.slots[from],prefs.slots[slot]]=[prefs.slots[slot],prefs.slots[from]];audioSlot=-1;save();renderSlot(from);renderSlot(slot);};
    const controls=el('div','tv-controls');tile.append(controls);
    if(!channel){host.append(button('+ Вибрати канал',()=>selectSlot(slot),'tv-add'));observer?.observe(tile);selectSlot(activeSlot);return;}
    const meta=el('p','tv-meta',`${channel.country} · ${channel.language}`);tile.append(meta);
    const audio=button('Увімкнути звук',()=>{audioSlot=audioSlot===slot?-1:slot;syncAudio();});audio.dataset.audio='';
    const refresh=button('Оновити',()=>players.get(slot)?.retry());
    const full=button('⛶',()=>{const promise=tile.requestFullscreen?.();promise?.catch(()=>{note.textContent='Повноекранний режим недоступний у цьому браузері.';});});full.setAttribute('aria-label','На весь екран');
    const star=button(prefs.favorites.includes(channel.id)?'★':'☆',()=>favorite(channel.id));star.dataset.favorite='';star.setAttribute('aria-label','В обране');star.setAttribute('aria-pressed',String(prefs.favorites.includes(channel.id)));
    controls.append(audio,refresh,full,star);
    const quality=el('select','tv-quality');quality.setAttribute('aria-label','Якість відео');quality.add(new Option('Якість: авто','-1'));quality.onchange=()=>players.get(slot)?.quality(quality.value);controls.append(quality);
    if(channel.sources.every(s=>s.type==='youtube')){quality.hidden=true;meta.append(' · Якість: ⚙ у плеєрі');}
    if(channel.sources[0]?.type==='iframe'){audio.hidden=true;quality.hidden=true;meta.append(' · Звук і якість: у плеєрі мовника');}
    if(channel.sources.length>1){const sourceSelect=el('select','tv-source');sourceSelect.setAttribute('aria-label','Джерело трансляції');
      for(const s of channel.sources)sourceSelect.add(new Option(s.type==='iframe'?'Плеєр мовника':s.candidateVideoId?'Прямий ефір YouTube':s.type==='youtube'?'Актуальний ефір каналу':'Офіційний '+s.type.toUpperCase(),s.id));
      sourceSelect.onchange=()=>{players.get(slot)?.selectSource(sourceSelect.value);};controls.append(sourceSelect);}
    const move=el('select','tv-move');move.setAttribute('aria-label','Перемістити канал до комірки');move.add(new Option('Перемістити…',''));
    for(let i=0;i<prefs.layout;i++)if(i!==slot)move.add(new Option('Комірка '+(i+1),String(i)));
    move.onchange=()=>{if(move.value==='')return;const target=Number(move.value);[prefs.slots[slot],prefs.slots[target]]=[prefs.slots[target],prefs.slots[slot]];audioSlot=-1;save();renderSlot(slot);renderSlot(target);};
    if(prefs.layout>1)controls.append(move);
    tile.append(el('p','tv-connection'),el('p','tv-last-success'),link('Офіційний ефір ↗',channel.officialUrl));
    startPlayer(slot,host,channel);
    if(!channel.sources.length){host.append(el('div','tv-empty',channel.status==='RESTRICTED'?'Вбудовування обмежено':'Вбудований LIVE ще не підтверджено'));audio.disabled=true;refresh.disabled=true;}
    observer?.observe(tile);selectSlot(activeSlot);
  }
  function renderWall(){for(const p of players.values())p.destroy();players.clear();tiles.clear();states.clear();observer?.disconnect();wall.replaceChildren();wall.dataset.layout=String(prefs.layout);
    audioSlot=-1;activeSlot=Math.min(activeSlot,prefs.layout-1);
    for(let slot=0;slot<prefs.layout;slot++)renderSlot(slot);
    for(const b of layoutButtons.children)b.setAttribute('aria-pressed',String(Number(b.dataset.layout)===prefs.layout));updateCounts();}
  function renderCatalog(){list.replaceChildren();const found=channels.filter(c=>matchesChannel(c,query,region,prefs.favorites));
    for(const c of found){const row=el('div','tv-channel');const choose=button('',()=>setChannel(c.id),'tv-channel-select');
      const logo=el('span','tv-monogram',c.name.replace(/[^\p{L}\p{N}]/gu,'').slice(0,2).toUpperCase());
      const text=el('span');text.append(el('strong','',c.name),el('small','',`${c.country} · ${c.language}`),el('small','tv-catalog-state',c.sources.length?'Вбудоване джерело':c.status==='RESTRICTED'?'RESTRICTED':'Не перевірено'));
      choose.append(logo,text);row.append(choose,button(prefs.favorites.includes(c.id)?'★':'☆',()=>favorite(c.id),'tv-favorite'));
      row.lastChild.setAttribute('aria-label','Обране: '+c.name);row.lastChild.setAttribute('aria-pressed',String(prefs.favorites.includes(c.id)));list.append(row);}
    if(!found.length)list.append(el('p','tv-empty','Каналів за цим запитом немає.'));
    for(const b of regionNav.children)b.setAttribute('aria-pressed',String(b.textContent===region));}
  function presets(){presetSelect.replaceChildren(new Option('Збережені стіни…',''));prefs.presets.forEach((p,i)=>presetSelect.add(new Option(p.name,String(i))));}
  async function refreshHealth(){const current=epoch;if(!drive?.readTvHealth)return;
    try {const snapshot=await drive.readTvHealth();if(current!==epoch||!alive())return;channels=mergeHealth(channels,snapshot);
      // Update source metadata in place. Running players are not restarted by archive refreshes.
      for(const [slot,p] of players){const c=channels.find(c=>c.id===prefs.slots[slot]);if(c)p.sources=p.sources.map(s=>c.sources.find(n=>n.id===s.id)||s);}
      if(snapshot)note.textContent='Хмарна перевірка: '+new Date(snapshot.checkedAt).toLocaleString('uk-UA');
    } catch {if(current===epoch&&note)note.textContent='Хмарна перевірка недоступна; статуси визначають локальні плеєри.';}}
  function clear(){epoch++;clearInterval(refreshTimer);observer?.disconnect();observer=null;for(const p of players.values())p.destroy();players.clear();tiles.clear();states.clear();main=null;audioSlot=-1;}
  return {clear,refresh:refreshHealth,
    async mount(target){clear();main=target;const current=epoch;main.replaceChildren(el('p','','Завантаження Global Live TV…'));
      try {const response=await fetch(new URL('./tv-catalog.json',import.meta.url));if(!response.ok)throw new Error('Каталог недоступний');
        const data=await response.json();if(current!==epoch||!alive())return;channels=validateCatalog(data);prefs=normalizePreferences(read(key,{}),channels);
        const shell=el('section','argus-tv');const top=el('header','tv-top');
        const heading=el('div');heading.append(el('p','tv-eyebrow','ARGUS / GLOBAL LIVE TV'),el('h1','','Телебачення'));
        count=el('p','tv-summary');heading.append(count);top.append(heading);layoutButtons=el('div','tv-layouts');layoutButtons.setAttribute('aria-label','Розкладка TV Wall');
        for(const layout of LAYOUTS){const b=button(layout===1?'1 канал':`${Math.sqrt(layout)} × ${Math.sqrt(layout)}`,()=>{prefs.layout=layout;save();renderWall();});b.dataset.layout=layout;layoutButtons.append(b);}top.append(layoutButtons);shell.append(top);
        const toolbar=el('div','tv-toolbar');const volume=el('input');volume.type='range';volume.min='0';volume.max='100';volume.value=prefs.volume;volume.setAttribute('aria-label','Гучність активного каналу');volume.oninput=()=>{prefs.volume=Number(volume.value);syncAudio();save();};
        toolbar.append(button('Вимкнути весь звук',()=>{audioSlot=-1;syncAudio();}),el('span','','Гучність'),volume);
        presetSelect=el('select');presetSelect.setAttribute('aria-label','Збережені стіни');presetSelect.onchange=()=>{const p=prefs.presets[Number(presetSelect.value)];if(presetSelect.value!==''&&p){prefs=normalizePreferences({...prefs,...p},channels);save();renderWall();}};toolbar.append(presetSelect);
        const presetName=el('input');presetName.placeholder='Назва конфігурації';presetName.maxLength=60;presetName.setAttribute('aria-label','Назва конфігурації');toolbar.append(presetName,button('Зберегти стіну',()=>{
          const name=presetName.value.trim();if(!name){note.textContent='Введіть назву конфігурації.';return;}prefs.presets=prefs.presets.filter(p=>p.name!==name).slice(-7);prefs.presets.push({name,layout:prefs.layout,slots:[...prefs.slots]});save();presets();note.textContent='Конфігурацію збережено в цьому браузері.';}));
        async function cloud(action){if(cloudBusy)return;cloudBusy=true;note.textContent='З’єднання з особистим Google Drive…';const revision=epoch;
          try {const value=action==='save'?await drive.saveTvPreferences(prefs):await drive.loadTvPreferences();if(revision!==epoch)return;
            if(action==='load'&&value){prefs=normalizePreferences(value,channels);save();presets();renderWall();renderCatalog();volume.value=prefs.volume;}
            note.textContent=value?'Налаштування '+(action==='save'?'збережено в':'відновлено з')+' особистому Google Drive.':'У Drive ще немає налаштувань TV.';
          }catch(e){if(revision===epoch)note.textContent=e.message;}finally{cloudBusy=false;}}
        if(drive?.saveTvPreferences)toolbar.append(button('У хмару',()=>void cloud('save')),button('Із хмари',()=>void cloud('load')));
        shell.append(toolbar);note=el('p','tv-note','Звук вимкнено. Виберіть комірку, потім канал.');note.setAttribute('role','status');shell.append(note);
        const body=el('div','tv-body'),aside=el('aside','tv-sidebar');const search=el('input','tv-search');search.type='search';search.placeholder='Канал, країна, мова';search.setAttribute('aria-label','Пошук телеканалів');search.oninput=()=>{query=search.value;renderCatalog();};aside.append(search);
        regionNav=el('nav','tv-regions');regionNav.setAttribute('aria-label','Регіони телебачення');for(const name of REGIONS)regionNav.append(button(name,()=>{region=name;renderCatalog();}));aside.append(regionNav);
        list=el('div','tv-catalog');aside.append(list);wall=el('div','tv-wall');body.append(aside,wall);shell.append(body);
        main.replaceChildren(shell);presets();renderCatalog();renderWall();
        // Resource conservation without repeatedly recreating off-screen players: pause only muted videos;
        // preserve YouTube native state and never reclaim the active audio tile.
        observer=new IntersectionObserver(entries=>{for(const entry of entries){const slot=Number(entry.target.dataset.slot),p=players.get(slot);if(!p||slot===audioSlot)continue;
          if(!entry.isIntersecting&&p.media&&!p.media.paused){p.media.pause();entry.target.dataset.autoPaused='media';}
          else if(!entry.isIntersecting&&p.youtube?.getPlayerState?.()===1){p.youtube.pauseVideo();entry.target.dataset.autoPaused='youtube';}
          else if(entry.isIntersecting&&entry.target.dataset.autoPaused){const kind=entry.target.dataset.autoPaused;delete entry.target.dataset.autoPaused;if(kind==='youtube')p.youtube?.playVideo();else p.media?.play().catch(()=>{});}
          if(p.hls)p.hls.autoLevelCapping=entry.isIntersecting?-1:0;
        }},{rootMargin:'160px'});for(const tile of tiles.values())observer.observe(tile);
        void refreshHealth();refreshTimer=setInterval(()=>void refreshHealth(),300000);
      }catch(error){if(current===epoch&&alive())main.replaceChildren(el('p','tv-note',error.message));}
    }
  };
}
