import test from 'node:test';
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {createWorkspaceData} from './workspace-data.mjs';
import {analysisForArchive} from './analysis.mjs';
const row={id:'test:1',platform:'rss',origin:'general',source_id:'s',text:'Пожежа',published_at:'2026-10-25T22:30:00Z',files:[]};
const hash=value=>createHash('sha256').update(value).digest('hex');
const sorted=value=>Array.isArray(value)?value.map(sorted):value&&typeof value==='object'?Object.fromEntries(Object.keys(value).sort().map(k=>[k,sorted(value[k])])):value;

test('publisher metrics sum available observations with honest coverage, including cached cloud summaries',()=>{
  const items=[row,{...row,id:'test:2',engagement:{views:100,likeCount:5}},
    {...row,id:'test:3',engagement:{reply_count:2,views:0}}];
  const expected={knownPosts:2,totalPosts:3,completePosts:0};
  for(const published of [null,{summaries:{'["general","","",""]':{totalPosts:3,totalViews:null,totalInteractions:null,postsByDay:[]}}}]){
    const summary=JSON.parse(createWorkspaceData({items},null,[],published)('/api/analytics/summary?origin=general')).body;
    assert.equal(summary.totalViews,100);assert.equal(summary.totalInteractions,7);assert.equal(summary.totalComments,2);
    assert.deepEqual(summary.metricCoverage.interactions,expected);
    assert.deepEqual(summary.metricCoverage.views,{knownPosts:2,totalPosts:3,completePosts:2});
  }
});

test('LIVE overlay preserves unchanged assessments, rejects edited content even with the same base digest',async()=>{
  const original={...row,engagement:{views:4,likes:2}};
  const a={snapshot_sha256:'a'.repeat(64),assessments:{[row.id]:{status:'completed',content_sha256:hash(JSON.stringify(sorted(original)))}},reports:[{}]};
  const snapshot={snapshot_sha256:a.snapshot_sha256,liveSequence:1,items:[original,{...row,id:'test:new'}]};
  assert.equal(Object.keys((await analysisForArchive(a,snapshot,hash)).assessments).length,1);
  snapshot.items[0]={...original,text:'Змінена публікація'};
  assert.equal(Object.keys((await analysisForArchive(a,snapshot,hash)).assessments).length,0);
});

test('all analytics filters select the same IDs as the feed, including Kyiv dates and registry categories',()=>{
  const items=[row,{...row,id:'test:2',origin:'dsns'},{...row,id:'test:3',text:'Інше'},row];
  const response=createWorkspaceData({items},null,[{platform:'rss',id:'s',name:'Publisher',category:'media',region:'Kyiv'}]);
  const filters='origin=general&platform=rss&category=media&region=Kyiv&keyword=ПОЖЕЖА&from=2026-10-26&to=2026-10-26';
  const posts=JSON.parse(response('/api/posts?'+filters)).body.posts;
  const summary=JSON.parse(response('/api/analytics/summary?'+filters)).body;
  assert.equal(posts.length,1);assert.equal(summary.totalPosts,1);assert.equal(summary.uniqueSources,1);
  assert.equal(summary.totalViews,null);assert.equal(summary.ai.assessed,0);
  assert.equal(JSON.parse(response('/api/analytics/summary?'+filters+'&unused=1')).body.platformBreakdown[0].count,1);
});

test('a new isolated publication increments totals exactly once and empty periods are explicit',()=>{
  const summary=items=>JSON.parse(createWorkspaceData({items})('/api/analytics/summary?origin=general')).body;
  assert.equal(summary([row]).totalPosts,1);
  assert.equal(summary([row,{...row,id:'test:2'},row]).totalPosts,2);
  const empty=JSON.parse(createWorkspaceData({items:[row]})('/api/analytics/summary?from=2027-01-01')).body;
  assert.equal(empty.totalPosts,0);assert.equal(empty.totalViews,null);assert.deepEqual(empty.postsByDay,[]);
});

test('Drive reads TypeSafe for a live snapshot and retries a temporary archive outage',async()=>{
  globalThis.window={isSecureContext:true,ARGUS_CLOUD_CONFIG:{folderId:'folder_ident_12345',clientId:'abc.apps.googleusercontent.com'}};
  globalThis.location=new URL('https://argus.example.org');
  window.google=globalThis.google={accounts:{oauth2:{hasGrantedAllScopes:()=>true,
    initTokenClient:options=>({requestAccessToken:()=>options.callback({access_token:'isolated-test',expires_in:3600})})}}};
  const analysis={format:'argus-typesafe-v1',snapshot_sha256:'a'.repeat(64),reports:[],assessments:{[row.id]:{
    status:'completed',version:'argus-dsns-v1',content_sha256:hash(JSON.stringify(sorted(row))),assessments:{sentiment:{value:'negative'}}}}};
  const bytes=JSON.stringify(analysis),pointer={format:'argus-typesafe-catalog-v1',id:'analysis_file_12345',sha256:hash(bytes),snapshot_sha256:analysis.snapshot_sha256};
  let outage=false,reads=0;
  globalThis.fetch=async url=>{
    if(outage)return new Response('',{status:503});
    const u=new URL(url);
    if(u.searchParams.has('q'))return Response.json({files:[{id:'pointer_file_12345'}]});
    if(u.pathname.endsWith('pointer_file_12345'))return Response.json(pointer);
    reads++;return new Response(bytes);
  };
  const drive=await import('./drive.mjs?recovery-test');await drive.signIn();
  const snapshot={items:[row],snapshot_sha256:analysis.snapshot_sha256,liveSequence:1};
  assert.equal(Object.keys((await drive.readAnalysis(snapshot)).assessments).length,1);
  await drive.readAnalysis(snapshot);assert.equal(reads,1);
  outage=true;await assert.rejects(drive.readAnalysis(snapshot),/Google Drive/);
  outage=false;assert.equal(Object.keys((await drive.readAnalysis(snapshot)).assessments).length,1);
  drive.clearSession();
});
