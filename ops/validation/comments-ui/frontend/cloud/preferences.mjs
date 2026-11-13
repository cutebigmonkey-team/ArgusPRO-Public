// Account-scoped values only. Global collector/AI controls never enter this object.
export const tabs=['/','/dsns','/sources','/keywords','/analytics','/comments','/maps','/tv','/argus-ai','/reports','/settings'];
export const defaults=()=>({theme:'system',accent:'#2563eb',language:'uk',fontSize:14,density:'standard',pageSize:20,
  images:true,videos:true,previews:true,refreshSeconds:7,tabOrder:[...tabs],startPage:'/',timezone:'Europe/Kyiv',dateFormat:'short',
  name:'',avatar:'',feedKeyword:'',notifications:{enabled:false,keywords:[],sources:[],errors:false,sound:false,push:false,frequency:60,priority:'all'}});
export function validatePreferences(input) {
  if(!input || typeof input!=='object' || Array.isArray(input))throw new Error('Invalid preferences');
  const d=defaults(), p={...d,...input,notifications:{...d.notifications,...input.notifications}};
  if(Object.keys(input).some(k=>!(k in d)))throw new Error('Unknown preference');
  if(typeof p.feedKeyword!=='string'||p.feedKeyword.length>200)throw new Error('Invalid feed keyword');
  for(const [key,values] of Object.entries({theme:['light','dark','system'],language:['uk','en'],density:['standard','compact'],
    dateFormat:['short','long','iso'],refreshSeconds:[7,15,30,60],pageSize:[10,20,50,100],fontSize:[12,14,16,18],startPage:tabs}))
    if(!values.includes(p[key]))throw new Error('Invalid '+key);
  if(!/^#[a-f\d]{6}$/i.test(p.accent))throw new Error('Invalid accent');
  for(const k of ['images','videos','previews'])if(typeof p[k]!=='boolean')throw new Error('Invalid '+k);
  try {new Intl.DateTimeFormat('uk',{timeZone:p.timezone}).format();}catch{throw new Error('Invalid timezone');}
  if(!Array.isArray(p.tabOrder)||p.tabOrder.length!==tabs.length||new Set(p.tabOrder).size!==tabs.length||p.tabOrder.some(t=>!tabs.includes(t)))throw new Error('Invalid tab order');
  if(typeof p.name!=='string'||p.name.length>100||typeof p.avatar!=='string'||p.avatar.length>12000||
    (p.avatar && !/^data:image\/(png|jpeg|webp);base64,[A-Za-z0-9+/=]+$/.test(p.avatar)))throw new Error('Invalid profile');
  const n=p.notifications;
  if(Object.keys(n).some(k=>!(k in d.notifications)))throw new Error('Unknown notification setting');
  for(const k of ['enabled','errors','sound','push'])if(typeof n[k]!=='boolean')throw new Error('Invalid notification');
  for(const k of ['keywords','sources'])if(!Array.isArray(n[k])||n[k].length>50||n[k].some(v=>typeof v!=='string'||!v.trim()||v.length>200))throw new Error('Invalid '+k);
  if(![0,30,60,300,900].includes(n.frequency)||!['all','critical'].includes(n.priority))throw new Error('Invalid notification frequency/priority');
  return structuredClone(p);
}
export function formatDate(value,p,dateOnly=false) {
  const date=new Date(value);if(!Number.isFinite(date.getTime()))return '—';
  return new Intl.DateTimeFormat(p.dateFormat==='iso'?'sv-SE':p.language==='en'?'en-GB':'uk-UA',{
    timeZone:p.timezone,dateStyle:p.dateFormat==='long'?'long':'short',...(!dateOnly?{timeStyle:'short'}:{})}).format(date);
}
export function matchingPosts(posts,n) {
  if(!n.enabled)return [];
  return posts.filter(p=>!p.sample && (!n.sources.length||n.sources.includes(p.source_id)) &&
    (!n.keywords.length||n.keywords.some(k=>(String(p.title||'')+' '+String(p.text||p.content||'')).toLocaleLowerCase().includes(k.toLocaleLowerCase()))) &&
    (n.priority==='all'||p.critical===true));
}
