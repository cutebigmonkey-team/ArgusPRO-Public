import assert from 'node:assert/strict';
import test from 'node:test';
import {createJournal} from './journal.mjs';
import {createWorkspaceData} from './workspace-data.mjs';

test('verified compaction skips old pages and resumes deltas without duplicate rows',async()=>{
  const ref=n=>({first:n,last:n,id:'file000000'+n,sha:String(n).padStart(64,'0')});
  const post=id=>({id,text:id,platform:'rss',files:[],origin:'general',published_at:'2026-11-05T10:00:00Z',observed_at:'2026-11-05T10:00:00Z'});
  const read=createJournal(),base={items:[post('a'),post('b')],packages:[],live_checkpoint:ref(2)};
  assert.equal(await read(base,ref(2),()=>assert.fail('old page fetched')),base);
  const result=await read(base,ref(3),async r=>{assert.equal(r.last,3);return {previous:ref(2),items:[post('c')],updated_at:'2026-11-05T11:00:00Z'};});
  assert.equal(result.items.length,3);
  assert.equal(result.updated_at,'2026-11-05T11:00:00Z');
  const compacted={...result,live_checkpoint:ref(3)};
  assert.equal((await createJournal()(compacted,ref(3),()=>assert.fail())).items.length,3);
});

test('disconnect midway through catch-up, retry, duplicate and late publication',async()=>{
  const journal=createJournal(), pages=new Map();
  const ref=n=>({first:n,last:n,id:'file000000'+n,sha:String(n).padStart(64,'0')});
  const post=(id,date,origin)=>({id,text:id,platform:'rss',files:[],origin,published_at:date,observed_at:date});
  const one=post('a','2026-11-03T10:00:00Z','general'), two=post('b','2026-11-03T09:00:00Z','general'), three=post('c','2026-11-03T11:00:00Z','dsns');
  for(const [i,row] of [one,two,three].entries()) pages.set(i+1,{previous:i?ref(i):null,items:[row]});
  const base={items:[one],packages:[]}, load=async r=>pages.get(r.last);
  assert.equal((await journal(base,ref(1),load)).items.length,1);
  await assert.rejects(journal(base,ref(3),async r=>{if(r.last===2) throw Error('offline');return load(r);}));
  const snap=await journal(base,ref(3),load);
  assert.equal(snap.items.length,3);
  const api=createWorkspaceData(snap);
  assert.deepEqual(JSON.parse(api('/api/posts?origin=general')).body.posts.map(p=>p.id),['a','b']);
  assert.deepEqual(JSON.parse(api('/api/posts?origin=dsns')).body.posts.map(p=>p.id),['c']);
  assert.equal(await journal(base,ref(3),()=>assert.fail()),snap);
});
