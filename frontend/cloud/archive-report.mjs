import {assessment, enrichSummary, topicLabels} from './analysis.mjs';
import {publicationDay, publicationTime} from './analytics-date.mjs';
import {toPost} from './workspace-data.mjs';

function startOfKyivDay(now) {
  const midnight=publicationDay(now.toISOString())+'T00:00:00';
  const zone=new Intl.DateTimeFormat('en',{timeZone:'Europe/Kyiv',timeZoneName:'longOffset'});
  const offset=zone.formatToParts(new Date(midnight+'Z')).find(p=>p.type==='timeZoneName').value.slice(3);
  return Date.parse(midnight+offset);
}

export function archiveReport(snapshot,analysis,selection,now=new Date()) {
  const {period,platform,region,dsns}=selection, end=now.getTime();
  const hours={'24h':24,'48h':48,'7d':168,'30d':720};
  const start=period==='all'?null:period==='today'?startOfKyivDay(now):end-hours[period]*3600000;
  const posts=snapshot.items.filter(p=>{
    if(p.sample || (platform!=='all' && platform!==p.platform) || (region!=='all' && region!==p.region) || (dsns && p.origin!=='dsns')) return false;
    const t=publicationTime(p.published_at);
    return Number.isFinite(t) ? t<=end && (start===null || t>=start) : start===null;
  }).map(toPost);
  const orderTime=p=>{const t=publicationTime(p.publishedAt);return Number.isFinite(t)?t:-Infinity;};
  posts.sort((a,b)=>orderTime(b)-orderTime(a)||a.id.localeCompare(b.id));
  const summary=enrichSummary({postsByDay:[]},posts,analysis);
  const {positive,neutral,negative}=summary.sentiment, classified=positive+neutral+negative;
  const percent=n=>classified?Math.round(1000*n/classified)/10:null;
  const evidence={top:[],positive:[],negative:[],critical:[],complaints:[],resonance:[],innovation:[],partners:[]};
  const topicPosts=new Map(), platforms=new Map();
  for(const p of posts) {
    platforms.set(p.platform,(platforms.get(p.platform)||0)+1);
    const a=assessment(analysis,p.id);
    if(!a) continue;
    const topic=a.topic?.value;
    if(Object.hasOwn(topicLabels,topic) && !topicPosts.has(topic)) topicPosts.set(topic,p.id);
    const matches={top:a.relevance?.value,critical:a.critical?.value,complaints:a.complaint?.value,resonance:a.resonance?.value,
      positive:a.sentiment?.value==='positive',negative:a.sentiment?.value==='negative',innovation:topic==='innovation',partners:topic==='partners'};
    for(const [key,match] of Object.entries(matches)) if(match===true && evidence[key].length<5) evidence[key].push(p.id);
  }
  return {
    filters:{period,platform,region,dsns,start:start===null?null:start/1000,end:end/1000},
    statistics:{posts:posts.length,assessed:summary.ai.assessed,critical:summary.ai.critical,complaints:summary.ai.complaints,resonance:summary.ai.resonance,
      positive,neutral,negative,sentiment_classified:classified,negative_percent:percent(negative),positive_neutral_percent:percent(positive+neutral),
      engagement:posts.length && posts.every(p=>p.engagementKnown)?posts.reduce((n,p)=>n+p.likes+p.commentsCount,0):null},
    evidence,topics:summary.topics.slice(0,5).map(t=>({topic:t.topic,count:t.count,post_id:topicPosts.get(t.topic)})),
    recent:posts.slice(0,5).map(p=>p.id),platforms:[...platforms].map(([platform,posts])=>({platform,posts})),
    generated_at:now.toISOString(),status:'draft',note:'Показники розраховано за публікаціями архіву. Оцінки TypeSafe враховано лише для перевірених версій публікацій.'
  };
}
