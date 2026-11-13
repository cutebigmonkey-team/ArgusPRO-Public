// Translate the verified private snapshot into the existing Argus UI contract.
// No fixtures, remote images, or invented engagement/sentiment data.
import {assessment,enrichSummary} from './analysis.mjs';
import {captureLabel} from './catalog.mjs';
import {publicationDay,withinPublicationRange,continuousDays} from './analytics-date.mjs';
export const safeUrl = value => {
  try { const u = new URL(value); return ['https:', 'http:'].includes(u.protocol) ? u.href : ''; }
  catch { return ''; }
};
const string = value => typeof value === 'string' ? value : '';
const count = value => Number.isSafeInteger(value) && value >= 0 ? value : 0;
const known = value => Number.isSafeInteger(value) && value >= 0;
const aliases={views:['views','view_count','viewCount','playCount','play_count','impression_count'],likes:['likes','like_count','likeCount','diggCount','reactions'],comments:['comments','comments_count','comment_count','commentCount','reply_count','replyCount'],shares:['shares','shares_count','share_count','shareCount','reposts','repost_count','retweet_count','forwards']};
function publisherMetric(item,key) {
  for(const object of [item,item.engagement||{}])for(const name of aliases[key])if(known(object[name]))return object[name];
  return null;
}
function metricSummary(posts) {
  const metricCoverage={}, result={metricCoverage};
  for(const [name,out,value,has,complete] of [
    ['views','totalViews',p=>p.views,p=>p.viewsKnown,p=>p.viewsKnown],
    ['comments','totalComments',p=>p.commentsCount,p=>p.commentsKnown,p=>p.commentsKnown],
    ['interactions','totalInteractions',p=>p.likes+p.commentsCount,p=>p.likesKnown||p.commentsKnown,p=>p.engagementKnown]]) {
    const rows=posts.filter(has);
    result[out]=rows.length?rows.reduce((n,p)=>n+value(p),0):null;
    metricCoverage[name]={knownPosts:rows.length,totalPosts:posts.length,completePosts:posts.filter(complete).length};
  }
  return result;
}
function sourceIdentity(item,url,sourceUrl,name) {
  if(item.source_id||sourceUrl||name)return string(item.source_id)||sourceUrl||name;
  if(!url)return '';
  const u=new URL(url), parts=u.pathname.split('/').filter(Boolean);
  if(['t.me','telegram.me'].includes(u.hostname))return parts[0]?u.hostname+'/'+(parts[0]==='s'?parts[1]:parts[0]):'';
  if(['rss','web','website'].includes(item.platform))return u.hostname;
  return '';
}
export function toPost(item) {
  const views=publisherMetric(item,'views'),likes=publisherMetric(item,'likes'),comments=publisherMetric(item,'comments'),shares=publisherMetric(item,'shares');
  const url = safeUrl(item.url), sourceUrl = safeUrl(item.source_url);
  const name = string(item.source_name);
  let text = string(item.text)||string(item.content);
  const capture=captureLabel(item);
  if (capture) text = text ? capture+'\n\n'+text : capture;
  if (item.source_match === 'search_query_only') text = 'Авторство не підтверджено.\n\n' + text;
  return {
    id: item.id, platform: item.platform, origin: ['general','dsns'].includes(item.origin) ? item.origin : 'unknown',
    sourceId: sourceIdentity(item,url,sourceUrl,name),
    sourceName: name || item.platform, region: string(item.region),
    category:string(item.category), language:string(item.language), keywords:Array.isArray(item.keywords)?item.keywords:[],
    publishedAt: string(item.published_at),
    discoveredAt: string(item.discovered_at) || string(item.observed_at),
    publicationAt: string(item.published_at),
    publicationDay: publicationDay(item.published_at),
    text, title: string(item.title), url, imageUrl: '', mentionTone: '',
    likes:count(likes),views:count(views),commentsCount:count(comments),shares:count(shares),
    viewsKnown:known(views),likesKnown:known(likes),commentsKnown:known(comments),sharesKnown:known(shares),engagementKnown:known(likes)&&known(comments),
  };
}
export function createWorkspaceData(snapshot, analysis = null, registered = [], published = null, preferences = {}) {
  const orderTime = value => {
    const time = Date.parse(value);
    return Number.isFinite(time) ? time : -Infinity;
  };
  const newest = field => (a,b) => orderTime(b[field]) - orderTime(a[field]);
  const registry=new Map(registered.map(r=>[r.platform+':'+r.id,r]));
  const posts = [...new Map(snapshot.items.filter(p => !p.sample).map(p=>[p.id,p])).values()].map(item=>{
    const source=registry.get(item.platform+':'+item.source_id)||{};
    return toPost({...item,source_name:item.source_name||source.name,category:item.category||source.category,
      region:item.region||source.region});
  }).sort(newest('publishedAt'));
  for (const p of posts) {
    const a = assessment(analysis,p.id);
    p.mentionTone = {positive:'praise',negative:'criticism',neutral:'other'}[a?.sentiment?.value] || '';
  }
  const platforms = [...new Set([...posts,...registered].map(p => p.platform))].sort().map(id => ({id, label: id === 'rss' ? 'RSS' : id[0].toUpperCase() + id.slice(1)}));
  const regions = [...new Set(posts.map(p => p.region).filter(Boolean))].sort().map(name => ({name}));
  const sourceMap = new Map();
  for(const r of registered) {
    const id=r.platform+':'+r.id;
    sourceMap.set(id,{id,name:r.name,url:r.url,platform:r.platform,region:r.region || '',origin:r.origin,
      status:r.enabled===false?'inactive':'active',postCountTotal:0,postCount24h:0,lastPostAt:''});
  }
  for (const p of posts) {
    const key = p.platform + ':' + p.sourceId;
    if (!sourceMap.has(key)) sourceMap.set(key, {id:key, name:p.sourceName, platform:p.platform, region:p.region, origin:p.origin, status:'active', postCountTotal:0, postCount24h:0, lastPostAt:p.publishedAt, url:''});
    const s = sourceMap.get(key); s.postCountTotal++;
    if(!s.lastPostAt) s.lastPostAt=p.publishedAt;
    if (Date.now() - Date.parse(p.publishedAt) <= 86400000 && Date.parse(p.publishedAt) <= Date.now()) s.postCount24h++;
  }
  return function response(path) {
    const u = new URL(path, 'https://argus.invalid'), q = u.searchParams;
    const now=Date.now();
    const keyword=(q.get('keyword')||'').normalize('NFC').toLowerCase();
    const match = posts.filter(p =>
      (!['dsns','general'].includes(q.get('origin')) || p.origin === q.get('origin')) &&
      (!q.get('platform') || q.get('platform') === 'all' || p.platform === q.get('platform')) &&
      (!q.get('region') || p.region === q.get('region')) &&
      (!q.get('category') || q.get('category')==='all' || p.category===q.get('category')) &&
      (!keyword || [p.title,p.text,...p.keywords].some(text=>String(text).normalize('NFC').toLowerCase().includes(keyword))) &&
      withinPublicationRange(p.publicationAt,q.get('from'),q.get('to'),now));
    let body;
    switch (u.pathname) {
      case '/api/posts': case '/fixtures/posts.json': {
        const saved=String(preferences.feedKeyword||'').normalize('NFC').toLocaleLowerCase();
        const filtered=saved?match.filter(p=>[p.title,p.text,...p.keywords].some(text=>String(text).normalize('NFC').toLocaleLowerCase().includes(saved))):match;
        body = {posts: q.get('sort') === 'discovered_at' ? filtered.sort(newest('discoveredAt')) : filtered}; break;
      }
      case '/api/platforms': case '/fixtures/platforms.json': body = {platforms:[{id:'all',label:'Усі'},...platforms]}; break;
      case '/fixtures/regions.json': body = {regions,platforms,categories:[...new Set(posts.map(p=>p.category).filter(Boolean))].sort()}; break;
      case '/api/sources': body = {sources:[...sourceMap.values()]}; break;
      case '/api/analytics/summary': {
        const key=JSON.stringify([q.get('origin')||'',q.get('platform')==='all'?'':q.get('platform')||'',q.get('region')||'',q.get('category')==='all'?'':q.get('category')||'']);
        const prepared=!q.get('from')&&!q.get('to')&&!keyword && published?.summaries?.[key];
        if(prepared) {
          body=structuredClone(prepared);
          Object.assign(body,metricSummary(match));
          body.postsByDay=continuousDays(new Map(body.postsByDay.map(d=>[d.date,d.posts])));
          enrichSummary(body,match,analysis);
          body.aggregation={mode:'cloud',generatedAt:published.generated_at};
          break;
        }
        const days = new Map(), byPlatform = new Map(), byRegion = new Map();
        for (const p of match) {
          const date = publicationDay(p.publicationAt);
          if (/^\d{4}-\d{2}-\d{2}$/.test(date)) days.set(date, (days.get(date) || 0) + 1);
          byPlatform.set(p.platform, (byPlatform.get(p.platform) || 0) + 1);
          if (p.region) byRegion.set(p.region, (byRegion.get(p.region) || 0) + 1);
        }
        body = {totalPosts:match.length, uniqueSources:new Set(match.filter(p=>p.sourceId).map(p => p.platform+':'+p.sourceId)).size,
          ...metricSummary(match),
          sentiment:{positive:0,neutral:0,negative:0},
          undatedPosts:match.filter(p=>!publicationDay(p.publicationAt)).length,
          postsByDay:continuousDays(days,q.get('from'),q.get('to')),
          platformBreakdown:[...byPlatform].map(([platform,count])=>({platform,count})).sort((a,b)=>b.count-a.count||a.platform.localeCompare(b.platform)),
          regionBreakdown:[...byRegion].map(([region,count])=>({region,count}))};
        enrichSummary(body,match,analysis);
        body.aggregation={mode:'verified_archive',generatedAt:snapshot.updated_at||null};
        break;
      }
      default: return JSON.stringify({status:503,body:{error:'Цей розділ ще не підключений до хмарних даних.'}});
    }
    return JSON.stringify({status:200,body});
  };
}
