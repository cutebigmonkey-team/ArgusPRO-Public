export function publicationIdentity(value) {
  try {
    const u=new URL(value);if(!['http:','https:'].includes(u.protocol)||u.username||u.password)return null;
    let host=u.hostname.toLowerCase().replace(/^www\./,'');
    if(host==='twitter.com')host='x.com';if(host==='threads.net')host='threads.com';
    let path=u.pathname.replace(/\/+$/,'');
    if(host==='t.me')path=path.replace(/^\/s\//,'/');
    if(host==='instagram.com')path=path.replace(/^\/reel\//,'/p/');
    if(host==='youtu.be')return 'youtube.com/watch?v='+path.slice(1);
    if(host==='youtube.com'&&/^\/shorts\//.test(path))return 'youtube.com/watch?v='+path.split('/')[2];
    if(host==='youtube.com'&&path==='/watch')return host+path+'?v='+u.searchParams.get('v');
    for(const key of [...u.searchParams.keys()])if(/^utm_|^(fbclid|gclid|igsh|si)$/.test(key))u.searchParams.delete(key);
    u.searchParams.sort();return host+path+(u.searchParams.size?'?'+u.searchParams:'');
  } catch {return null;}
}

export function checkPublication(items,url,sources,coverage) {
  const identity=publicationIdentity(url);
  if(!identity)return 'Вкажіть коректний відкритий HTTP(S) URL публікації.';
  const post=items.find(p=>!p.sample&&publicationIdentity(p.url||p.post_url)===identity);
  if(post)return `Є в архіві. Платформа: ${post.platform}; джерело: ${post.source_id||'—'}; стрічка: ${post.origin==='dsns'?'ДСНС':post.origin==='general'?'загальна':'належність не визначено'}; записано: ${post.first_archived_at||'час невідомий'}. Перевірте фільтр дати, платформи та стрічки, якщо пост не видно.`;
  const source=sources.filter(s=>{const base=publicationIdentity(s.url);return base&&(identity===base||identity.startsWith(base+'/'));}).sort((a,b)=>b.url.length-a.url.length)[0];
  if(!source)return 'У поточному архіві не знайдено. Джерело не вдалося однозначно зіставити з реєстром; це не доводить, що пост недоступний.';
  const h=coverage?.sources?.[source.id];
  return `У поточному архіві не знайдено. Джерело: ${source.name||source.id}; ${source.enabled===false?'вимкнено':'увімкнено'}; останній статус: ${h?.status||'ще не перевірено'}; спроба: ${h?.last_attempt_at||'—'}; наступна перевірка: ${h?.next_check_at||'—'}. Це діагностика останнього збору, а не підтверджена причина відсутності саме цього поста.`;
}
