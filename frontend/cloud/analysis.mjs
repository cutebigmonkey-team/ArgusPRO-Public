export const topicLabels = {fire:'Пожежі',rescue:'Рятувальні роботи',mines:'Мінна безпека',weather:'Погодні загрози',prevention:'Профілактика',partners:'Допомога партнерів',innovation:'Інновації',other:'Інше'};
import {publicationDay} from './analytics-date.mjs';
export function validateAnalysis(value) {
  if (!value || value.format !== 'argus-typesafe-v1' || !/^[a-f0-9]{64}$/.test(value.snapshot_sha256) ||
      !value.assessments || typeof value.assessments !== 'object' || !Array.isArray(value.reports) || value.reports.length > 5000)
    throw new Error('Невірний формат оцінок TypeSafe.');
  return value;
}
export function assessment(analysis,id) {
  const row = analysis?.assessments?.[id];
  return row?.status === 'completed' && row.version === 'argus-dsns-v1' ? row.assessments : null;
}
export async function analysisForArchive(result,snapshot,checksum) {
  if(!snapshot.liveSequence && result.snapshot_sha256===snapshot.snapshot_sha256) return result;
  // The worker hashes each complete item as compact, sorted UTF-8 JSON.
  // The verified archive arrives in that key order. An edited item or a
  // representation that cannot reproduce the digest gets no assessment.
  const assessments=Object.create(null), encoder=new TextEncoder();
  const canonical=value=>Array.isArray(value)?value.map(canonical):value&&typeof value==='object'?
    Object.fromEntries(Object.keys(value).sort().map(k=>[k,canonical(value[k])])):value;
  for(const item of snapshot.items) {
    const row=result.assessments[item.id];
    if(row?.status==='completed' && await checksum(encoder.encode(JSON.stringify(canonical(item))))===row.content_sha256)
      assessments[item.id]=row;
  }
  return {...result,snapshot_sha256:snapshot.snapshot_sha256,reports:[],assessments};
}
export function enrichSummary(body, posts, analysis) {
  const topics = new Map();
  const counters = {assessed:0,unassessed:0,needsReview:0,relevant:0,critical:0,complaints:0,resonance:0,highRisk:0};
  const days = new Map(body.postsByDay.map(d => [d.date,d]));
  for (const d of days.values()) Object.assign(d,{positive:0,neutral:0,negative:0});
  body.sentiment = {positive:0,neutral:0,negative:0};
  const unclassified={topic:'unclassified',label:'Тему не визначено',count:0,positive:0,neutral:0,negative:0};
  body.undatedSentiment={positive:0,neutral:0,negative:0};
  for (const post of posts) {
    const row = analysis?.assessments?.[post.id], a = assessment(analysis,post.id);
    if (!a) {counters.unassessed++;continue;}
    counters.assessed++;
    if (row.needs_review) counters.needsReview++;
    const sentiment = a.sentiment?.value;
    if (Object.hasOwn(body.sentiment,sentiment)) {
      body.sentiment[sentiment]++;
      const day = days.get(publicationDay(post.publicationAt));
      if (day) day[sentiment]++;else body.undatedSentiment[sentiment]++;
    }
    const topic = a.topic?.value;
    if (Object.hasOwn(topicLabels,topic)) {
      if (!topics.has(topic)) topics.set(topic,{topic,label:topicLabels[topic],count:0,positive:0,neutral:0,negative:0});
      const bucket = topics.get(topic);bucket.count++;
      if (Object.hasOwn(body.sentiment,sentiment)) bucket[sentiment]++;
    } else if(Object.hasOwn(body.sentiment,sentiment)) {
      unclassified.count++;unclassified[sentiment]++;
    }
    for (const [key,field] of [['relevant','relevance'],['critical','critical'],['complaints','complaint'],['resonance','resonance']])
      if (a[field]?.value === true) counters[key]++;
    if (a.risk?.value === 'high') counters.highRisk++;
  }
  body.ai = {...counters,updatedAt:analysis?.updated_at || '',available:!!analysis};
  body.topics = [...topics.values()].sort((a,b) => b.count-a.count || a.topic.localeCompare(b.topic));
  body.topicSentiment=body.topics.map(t=>({...t,unknown:t.count-t.positive-t.neutral-t.negative}));
  if(unclassified.count)body.topicSentiment.push({...unclassified,unknown:0});
  return body;
}
