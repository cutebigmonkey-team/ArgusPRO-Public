export const LAYOUTS=[1,4,9,16];
export const REGIONS=['Усі країни','Україна','Росія','Європа','США','Китай','Індія','Японія','Африка','Бразилія','Міжнародні','Обрані канали'];
export const fold=value=>String(value||'').normalize('NFKC').toLocaleLowerCase('uk-UA');
export function matchesChannel(channel,query,region,favorites=[]) {
  return (region==='Усі країни'||(region==='Обрані канали'?favorites.includes(channel.id):channel.region===region))&&
    fold([channel.name,channel.country,channel.region,channel.language,channel.languageCode].join(' ')).includes(fold(query));
}
export function normalizePreferences(input,channels) {
  const ids=new Set(channels.map(c=>c.id)),valid=id=>ids.has(id);
  const layout=LAYOUTS.includes(input?.layout)?input.layout:1;
  const slots=Array.from({length:16},(_,i)=>valid(input?.slots?.[i])?input.slots[i]:null);
  if(!slots.some(Boolean)) slots[0]=channels.find(c=>c.sources?.length)?.id||channels[0]?.id;
  return {version:1,layout,slots,volume:Math.max(0,Math.min(100,Number.isFinite(Number(input?.volume))?Number(input.volume):50)),
    favorites:[...new Set((Array.isArray(input?.favorites)?input.favorites:[]).filter(valid))].slice(0,100),
    presets:(Array.isArray(input?.presets)?input.presets:[]).slice(0,8).filter(p=>typeof p.name==='string'&&LAYOUTS.includes(p.layout)&&Array.isArray(p.slots))
      .map(p=>({name:p.name.slice(0,60),layout:p.layout,slots:p.slots.slice(0,16).map(id=>valid(id)?id:null)}))};
}
export function safeHttps(value) {
  try {const url=new URL(value);return url.protocol==='https:'&&!url.username&&!url.password?url.href:null;} catch {return null;}
}
export function validateCatalog(value) {
  if(value?.format!=='argus-tv-catalog-v1'||!Array.isArray(value.channels)||value.channels.length>200) throw new Error('Пошкоджений каталог TV');
  const ids=new Set();
  for(const c of value.channels) {
    if(!/^[a-z0-9-]{1,50}$/.test(c.id)||ids.has(c.id)||!safeHttps(c.officialUrl)||typeof c.name!=='string'||!Array.isArray(c.sources)) throw new Error('Невірний канал TV');
    ids.add(c.id);
    for(const s of c.sources) {
      if(!s.id||!safeHttps(s.evidenceUrl)||s.permission!=='official-embed') throw new Error('Джерело без підтвердження');
      if(s.type==='youtube') {if(!/^UC[\w-]{22}$/.test(s.channelId)||s.candidateVideoId&&!/^[\w-]{11}$/.test(s.candidateVideoId)) throw new Error('Невірний YouTube канал');}
      else if(!['hls','dash','iframe'].includes(s.type)||!safeHttps(s.url)) throw new Error('Невідомий плеєр');
    }
  }
  return value.channels;
}
// One finite retry budget per incident. A short PLAYING callback cannot reset it.
export class StreamHealth {
  constructor({emit,reconnect,now=Date.now,setTimer=(fn,ms)=>globalThis.setTimeout(fn,ms),clearTimer=id=>globalThis.clearTimeout(id),random=Math.random}) {
    Object.assign(this,{emit,reconnect,now,setTimer,clearTimer,random});this.attempt=0;this.generation=0;this.lastSuccess=null;this.disposed=false;
  }
  state(status,reason='') {if(!this.disposed){this.status=status;this.emit({status,reason,attempt:this.attempt,lastSuccess:this.lastSuccess});}}
  connected() {
    if(this.disposed)return;
    this.clearTimer(this.retry);this.retry=null;
    if(this.status!=='LIVE')this.stableSince=this.now();
    this.lastSuccess=this.now();this.state('LIVE','Прямий ефір відтворюється');
  }
  progress() {if(this.status==='LIVE'&&this.now()-this.stableSince>=120000)this.attempt=0;}
  failure(reason,{restricted=false}={}) {
    if(this.disposed||this.retry)return;
    if(restricted){this.state('RESTRICTED',reason);return;}
    if(this.attempt>=6){this.state('OFFLINE','Ліміт повторів вичерпано. '+reason+' Натисніть «Оновити».');return;}
    const delay=Math.min(60000,2000*2**this.attempt)+Math.round(this.random()*1000);
    this.attempt++;this.state('RECONNECTING',`${reason} · повтор ${this.attempt}/6 через ${Math.ceil(delay/1000)} с`);
    const generation=this.generation;
    this.retry=this.setTimer(()=>{this.retry=null;if(!this.disposed&&generation===this.generation)this.reconnect();},delay);
  }
  manual(reconnect=this.reconnect){this.clearTimer(this.retry);this.retry=null;this.attempt=0;this.generation++;reconnect();}
  dispose(){this.disposed=true;this.generation++;this.clearTimer(this.retry);this.retry=null;}
}
export function mergeHealth(channels,snapshot,now=Date.now()) {
  if(snapshot?.format!=='argus-tv-health-v1'||!Array.isArray(snapshot.channels))return channels;
  const fresh=now-Date.parse(snapshot.checkedAt)<20*60000 && Date.parse(snapshot.checkedAt)<=now+60000;
  return channels.map(c=>{const entry=snapshot.channels.find(e=>e.id===c.id);
    if(!entry)return c;
    // A remote observation never marks a local player as LIVE or introduces URLs.
    return {...c,remote:fresh?entry:{status:'UNKNOWN',reason:'Хмарна перевірка застаріла',checkedAt:entry.checkedAt},
      sources:c.sources.map(s=>s.type==='youtube'&&entry.channelId===s.channelId&&fresh&&entry.status==='LIVE'&&/^[\w-]{11}$/.test(entry.videoId||'')?
        {...s,videoId:entry.videoId,verifiedLiveAt:entry.checkedAt}:s)};
  });
}
