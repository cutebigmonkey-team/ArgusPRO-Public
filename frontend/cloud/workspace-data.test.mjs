import test from 'node:test';
import assert from 'node:assert/strict';
import {createWorkspaceData, toPost} from './workspace-data.mjs';
const item = (id, extra={}) => ({id, origin:'general', platform:'telegram', text:'Матеріал', url:'https://t.me/channel/1', source_name:'Джерело', published_at:'2026-10-30T12:00:00Z', observed_at:'2026-10-30T13:00:00Z', files:[], ...extra});
const read = (response, path) => JSON.parse(response(path));

test('saved personal feed keyword filters feeds without hiding archive analytics',()=>{
  const snapshot={items:[item('match',{text:'ПОВІНЬ у громаді'}),item('other',{text:'Інша новина'})]};
  const response=createWorkspaceData(snapshot,null,[],null,{feedKeyword:'повінь'});
  assert.deepEqual(read(response,'/api/posts').body.posts.map(p=>p.id),['match']);
  assert.equal(read(response,'/api/analytics/summary').body.totalPosts,2);
});

test('post counters distinguish missing values from real zero and preserve shares',()=>{
  const unknown=toPost(item('missing'));
  assert.equal(unknown.likesKnown,false);assert.equal(unknown.viewsKnown,false);assert.equal(unknown.commentsKnown,false);assert.equal(unknown.sharesKnown,false);
  const real=toPost(item('real',{engagement:{likes:0,views:123,comments:2,shareCount:4}}));
  assert.deepEqual([real.likes,real.views,real.commentsCount,real.shares],[0,123,2,4]);
  assert(real.likesKnown&&real.viewsKnown&&real.commentsKnown&&real.sharesKnown);
});
test('the real DSNS Air Alert post is in the Kyiv November 2 feed', () => {
  const url='https://t.me/fixture123/79375';
  const post=item('telegram:dsns_telegram/79375',{url,origin:'dsns',published_at:'2026-11-01T22:00:03+00:00'});
  const response=createWorkspaceData({items:[post]});
  const from=Date.now; Date.now=()=>Date.parse('2026-11-02T12:00:00Z');
  try {
    const result=read(response,'/api/posts?origin=dsns&from=2026-11-02&to=2026-11-02');
    assert.equal(result.body.posts[0].publicationDay,'2026-11-02');
    assert.equal(result.body.posts[0].url,url);
    assert.equal(read(response,'/api/posts?origin=general').body.posts.length,0);
  } finally {Date.now=from;}
});
test('rediscovery does not promote an undated post or include it in a publication window', () => {
  const response = createWorkspaceData({items:[item('undated',{published_at:null,discovered_at:'2026-11-02T12:00:00Z'}),item('known')]});
  const ids = query => read(response,'/api/posts?'+query).body.posts.map(p=>p.id);
  assert.deepEqual(ids(''),['known','undated']);
  assert.deepEqual(ids('sort=discovered_at'),['undated','known']);
  assert.deepEqual(ids('from=2026-11-02'),[]);
});
test('real archive maps to the main feed; samples are excluded; all rows are searchable', () => {
  const response = createWorkspaceData({items:[...Array.from({length:1205}, (_,i)=>item(String(i))),item('sample',{sample:true})]});
  const result = read(response,'/api/posts?limit=1000&origin=general');
  assert.equal(result.status,200); assert.equal(result.body.posts.length,1205);
  assert.equal(read(response,'/api/analytics/summary').body.totalPosts,1205);
  assert.equal(read(response,'/api/sources').body.sources[0].postCountTotal,1205);
});
test('DSNS classification uses source identity, not news text, and filters dates and platforms', () => {
  const response = createWorkspaceData({items:[item('one',{source_name:'Обласне управління',origin:'dsns'}),item('two',{text:'ДСНС врятували людей',platform:'rss'}),item('three',{origin:'dsns',published_at:'2026-10-25T12:00:00Z'})]});
  assert.deepEqual(read(response,'/api/posts?origin=dsns').body.posts.map(p=>p.id),['one','three']);
  assert.equal(read(response,'/api/analytics/summary?origin=dsns&from=2026-10-29').body.totalPosts,1);
  assert.equal(read(response,'/api/analytics/summary?platform=rss').body.totalPosts,1);
});
test('untrusted links are blocked and partial capture remains visibly identified', () => {
  const p = toPost(item('x',{url:'javascript:alert(1)',capture_mode:'search_preview',source_match:'search_query_only',media_urls:['https://tracker.example/pixel']}));
  assert.equal(p.url,''); assert.equal(p.imageUrl,''); assert.match(p.text,/пошукове прев’ю/); assert.match(p.text,/Авторство не підтверджено/);
});
test('unknown functions fail explicitly and analytics do not invent sentiment', () => {
  const response = createWorkspaceData({items:[item('x')]});
  assert.equal(read(response,'/fixtures/ai-conversations.json').status,503);
  assert.deepEqual(read(response,'/api/analytics/summary').body.sentiment,{positive:0,neutral:0,negative:0});
});

test('the two tab requests partition confirmed sources and preserve unknowns in analytics', () => {
  const items = [
    item('unit-fb',{platform:'facebook',origin:'dsns',source_name:'Обухівське РУ'}),
    item('unit-tg',{origin:'dsns'}),
    item('unit-site',{platform:'rss',origin:'dsns'}),
    item('news',{platform:'rss',source_name:'Новини ДСНС',text:'ДСНС ліквідувала пожежу'}),
    item('repost',{text:'Перепублікація з каналу ДСНС'}),
    item('unverified',{origin:null,source_name:'ДСНС'}),
  ];
  const response=createWorkspaceData({items},{assessments:{news:{official_source:true}}});
  const ids = origin => read(response,'/api/posts?limit=1000&origin='+origin).body.posts.map(p=>p.id);
  assert.deepEqual(ids('dsns'),['unit-fb','unit-tg','unit-site']);
  assert.deepEqual(ids('general'),['news','repost']);
  assert.equal(toPost(items.at(-1)).origin,'unknown');
  assert.equal(read(response,'/api/analytics/summary').body.totalPosts,6);
});
