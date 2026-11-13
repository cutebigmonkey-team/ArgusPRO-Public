import test from 'node:test';
import assert from 'node:assert/strict';
import {createWorkspaceData} from './workspace-data.mjs';
import {validateAnalysis,analysisForArchive} from './analysis.mjs';
import {archiveReport} from './archive-report.mjs';
import {createHash} from 'node:crypto';
const p={id:'one',platform:'rss',source_name:'ДСНС',text:'synthetic',published_at:'2026-10-30T10:00:00Z',files:[]};
const a={format:'argus-typesafe-v1',snapshot_sha256:'a'.repeat(64),reports:[],assessments:{one:{status:'completed',version:'argus-dsns-v1',needs_review:false,
  assessments:{topic:{value:'fire'},sentiment:{value:'negative'},critical:{value:true},complaint:{value:true},relevance:{value:true},resonance:{value:null},risk:{value:'high'}}}}};
test('classified topics, DSNS relevance, risk and sentiment reach the existing analytics contract',()=>{
  const response=createWorkspaceData({items:[p,{...p,id:'two'}]},a);
  const s=JSON.parse(response('/api/analytics/summary')).body;
  assert.equal(s.sentiment.negative,1);assert.equal(s.sentiment.neutral,0);
  assert.equal(s.ai.assessed,1);assert.equal(s.ai.unassessed,1);assert.equal(s.ai.relevant,1);assert.equal(s.ai.critical,1);
  assert.equal(s.ai.highRisk,1);assert.equal(s.ai.resonance,0);assert.equal(s.topics[0].label,'Пожежі');
  assert.equal(s.postsByDay[0].negative,1);
  assert.equal(JSON.parse(response('/api/analytics/summary?from=2026-10-31')).body.ai.assessed,0);
});
test('missing, blocked and uncertain results are never neutral or assessed',()=>{
  for(const status of ['blocked_privacy','uncertain','pending_budget']){
    const copy=structuredClone(a);copy.assessments.one.status=status;
    const s=JSON.parse(createWorkspaceData({items:[p]},copy)('/api/analytics/summary')).body;
    assert.equal(s.ai.assessed,0);assert.equal(s.ai.unassessed,1);assert.equal(s.sentiment.neutral,0);
  }
});
test('invalid analysis format fails closed',()=>{assert.throws(()=>validateAnalysis({}));assert.equal(validateAnalysis(a),a);});

test('after the archive changes only byte-matching completed assessments are reused',async()=>{
  const hash=bytes=>createHash('sha256').update(bytes).digest('hex');
  const records=['one','two','new'].map(id=>({...p,id}));
  const analysis={...a,assessments:Object.fromEntries(records.slice(0,2).map(item=>[item.id,
    {...a.assessments.one,content_sha256:hash(JSON.stringify(Object.fromEntries(Object.entries(item).sort(([a],[b])=>a.localeCompare(b)))))}]))};
  records[1].text+=' edited';
  const snapshot={items:records,snapshot_sha256:'b'.repeat(64)};
  const verified=await analysisForArchive(analysis,snapshot,hash);
  assert.deepEqual(Object.keys(verified.assessments),['one']);
  assert.deepEqual(verified.reports,[]);
  const report=archiveReport(snapshot,verified,{period:'all',platform:'all',region:'all',dsns:false},new Date('2026-11-03T12:00:00Z'));
  assert.equal(report.statistics.posts,3);
  assert.equal(report.statistics.assessed,1);
  assert.deepEqual(report.evidence.negative,['one']);
});

test('on-demand report filters by Kyiv day, platform, region and source without TypeSafe',()=>{
  const current={...p,published_at:'2026-10-24T21:00:00Z',origin:'dsns',region:'Київ'};
  const items=[current,...[{sample:true},{origin:'general'},{region:'Львів'},{platform:'telegram'},
    {published_at:'2026-10-24T20:59:59Z'},{published_at:'2026-10-25T23:00:00Z'},{published_at:''}].map((change,i)=>({...current,id:String(i),...change}))];
  const r=archiveReport({items},null,{period:'today',platform:'rss',region:'Київ',dsns:true},new Date('2026-10-25T18:00:00Z'));
  assert.deepEqual(r.recent,['one']);
  assert.equal(r.statistics.posts,1);
  assert.equal(r.statistics.assessed,0);
  assert.equal(r.statistics.negative_percent,null);
  assert.equal(r.statistics.engagement,null);
});

test('daily totals, platform totals and topic sentiment reconcile including uncertain topics',()=>{
  const items=[
    {...p,id:'one',published_at:'2026-10-27T22:30:00Z'}, // 28 October in Kyiv
    {...p,id:'two',published_at:'2026-10-30T10:00:00Z',platform:'telegram'},
    {...p,id:'unknown',published_at:'',observed_at:'2026-10-30T10:00:00Z'},
    {...p,id:'old',published_at:'2010-01-01T10:00:00Z'},
    {...p,id:'future',published_at:'2099-01-01T10:00:00Z'},
    {...p,id:'sample',sample:true}];
  const copy=structuredClone(a);
  copy.assessments.two=structuredClone(copy.assessments.one);
  copy.assessments.two.assessments.topic.value=null;
  copy.assessments.two.assessments.sentiment.value='positive';
  copy.assessments.unknown=structuredClone(copy.assessments.one);
  const read=q=>JSON.parse(createWorkspaceData({items},copy)('/api/analytics/summary'+q)).body;
  const s=read('');
  assert.equal(s.totalPosts,4);assert.equal(s.undatedPosts,1);
  assert.equal(s.platformBreakdown.reduce((n,b)=>n+b.count,0),s.totalPosts);
  assert.equal(s.postsByDay.reduce((n,d)=>n+d.posts,0)+s.undatedPosts,s.totalPosts);
  assert.equal(s.postsByDay.find(d=>d.date==='2026-10-28').posts,1);
  assert.equal(s.postsByDay.find(d=>d.date==='2026-10-29').posts,0);
  for(const tone of ['positive','neutral','negative']){
    assert.equal(s.postsByDay.reduce((n,d)=>n+d[tone],0)+s.undatedSentiment[tone],s.sentiment[tone]);
    assert.equal(s.topicSentiment.reduce((n,t)=>n+t[tone],0),s.sentiment[tone]);
  }
  assert.equal(s.topicSentiment.find(t=>t.topic==='unclassified').positive,1);
  const day=read('?from=2026-10-28&to=2026-10-28');
  assert.equal(day.totalPosts,1);assert.equal(day.sentiment.negative,1);
  assert.equal(read('?platform=telegram').totalPosts,1);
  assert.equal(read('?from=2026-10-29&to=2026-10-30').totalPosts,1);
  assert.equal(s.totalViews,null);assert.equal(s.totalInteractions,null);
});

test('Kyiv day boundaries include both offsets across daylight-saving transition',()=>{
  const items=[{...p,id:'one',published_at:'2026-10-24T21:30:00Z'},
    {...p,id:'two',published_at:'2026-10-25T21:30:00Z'},
    {...p,id:'three',published_at:'2026-10-25T22:30:00Z'}];
  const body=JSON.parse(createWorkspaceData({items})('/api/analytics/summary?from=2026-10-25&to=2026-10-25')).body;
  assert.equal(body.totalPosts,2);assert.deepEqual(body.postsByDay,[{date:'2026-10-25',posts:2,positive:0,neutral:0,negative:0}]);
});

test('missing source identity is not replaced with a fictitious platform source',()=>{
  const body=JSON.parse(createWorkspaceData({items:[{...p,source_name:'',url:'https://news.example/article/1'},
    {...p,id:'two',source_name:'',url:'https://news.example/article/2'},
    {...p,id:'three',platform:'youtube',source_name:'',url:'https://youtube.com/watch?v=unknown'}]})('/api/analytics/summary')).body;
  assert.equal(body.uniqueSources,1);
});
