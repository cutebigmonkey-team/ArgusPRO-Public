import test from 'node:test';import assert from 'node:assert/strict';import fs from 'node:fs';
import {StreamHealth,validateCatalog,normalizePreferences,matchesChannel,mergeHealth} from './tv-core.mjs';
// Synthetic fixtures preserve validation/search/retry tests without a private programming catalog.
const catalog={format:'argus-tv-catalog-v1',channels:Array.from({length:58},(_,i)=>({
 id:i===0?'fixture-a':i===1?'fixture-b':'fixture-'+i,
 name:i===1?'Тестовий канал':'Fixture '+i,country:'Україна',region:'Україна',language:'українська',languageCode:'uk',
 officialUrl:'https://example.invalid/channel/'+i,processingAllowed:false,
 sources:[{id:'fixture-source-'+i,type:'youtube',channelId:'UC'+'a'.repeat(22),
 evidenceUrl:'https://example.invalid/evidence/'+i,permission:'official-embed'}]}))};
test('58 synthetic candidates exercise URL and source-identity validation',()=>{
 const channels=validateCatalog(catalog);assert.equal(channels.length,58);assert.equal(new Set(channels.map(c=>c.id)).size,58);
 assert.equal(channels.some(c=>c.sources.some(s=>s.videoId)),false);
 assert.equal(channels.every(c=>c.processingAllowed===false),true);
});
test('preferences reject unknown channels, preserve empty slots, cap saved walls',()=>{
 const p=normalizePreferences({layout:16,slots:['bad','fixture-a',null],favorites:['fixture-a','bad','fixture-a'],volume:900},catalog.channels);
 assert.equal(p.slots[0],null);assert.equal(p.slots[1],'fixture-a');assert.equal(p.slots[2],null);assert.equal(p.volume,100);assert.deepEqual(p.favorites,['fixture-a']);
});
test('search supports Ukrainian case and region/favorite filters',()=>{
 const channel=catalog.channels.find(c=>c.id==='fixture-b');assert.ok(matchesChannel(channel,'ТЕСТОВИЙ','Україна'));
 assert.ok(matchesChannel(channel,'україн','Обрані канали',['fixture-b']));assert.equal(matchesChannel(channel,'','США'),false);
});
function harness(){let time=0,id=0;const jobs=new Map(),events=[],reconnects=[];
 const h=new StreamHealth({emit:e=>events.push(e),reconnect:()=>reconnects.push(time),now:()=>time,random:()=>0,
  setTimer:(f,ms)=>{jobs.set(++id,{f,ms});return id;},clearTimer:id=>jobs.delete(id)});
 return {h,jobs,events,reconnects,tick(){const [id,job]=jobs.entries().next().value;jobs.delete(id);time+=job.ms;job.f();},advance(ms){time+=ms;}};}
test('browser timers retain Window receiver instead of the StreamHealth instance',()=>{
 const originalSet=globalThis.setTimeout,originalClear=globalThis.clearTimeout;let cleared=false;
 try {
  globalThis.setTimeout=function(){assert.equal(this,globalThis);return 17;};
  globalThis.clearTimeout=function(){assert.equal(this,globalThis);cleared=true;};
  const h=new StreamHealth({emit:()=>{},reconnect:()=>{}});h.failure('network');h.dispose();assert.ok(cleared);
 }finally{globalThis.setTimeout=originalSet;globalThis.clearTimeout=originalClear;}
});
test('recovery has exponential backoff and finite circuit breaker',()=>{const x=harness();const waits=[];
 for(let i=0;i<6;i++){x.h.failure('network');waits.push([...x.jobs.values()][0].ms);x.h.failure('duplicate');assert.equal(x.jobs.size,1);x.tick();}
 x.h.failure('still offline');assert.deepEqual(waits,[2000,4000,8000,16000,32000,60000]);assert.equal(x.jobs.size,0);assert.equal(x.h.status,'OFFLINE');
 x.h.manual();assert.equal(x.h.attempt,0);assert.equal(x.reconnects.length,7);
});
test('brief playing cannot replenish retry budget; two stable minutes can',()=>{const x=harness();x.h.failure('network');x.tick();x.h.connected();assert.equal(x.h.attempt,1);
 x.advance(120001);x.h.progress();assert.equal(x.h.attempt,0);});
test('restriction does not reload and unmount cancels late retries',()=>{const x=harness();x.h.failure('geo',{restricted:true});assert.equal(x.jobs.size,0);assert.equal(x.h.status,'RESTRICTED');
 x.h.failure('network');x.h.dispose();assert.equal(x.jobs.size,0);x.h.connected();assert.equal(x.h.status,'RECONNECTING');});
test('stale and cross-channel metadata never selects a video',()=>{
 const channels=catalog.channels.filter(c=>c.sources.length),c=channels[0];const now=Date.now();
 const snapshot={format:'argus-tv-health-v1',checkedAt:new Date(now).toISOString(),channels:[{id:c.id,channelId:'wrong',status:'LIVE',videoId:'abcdefghijk',checkedAt:new Date(now).toISOString()}]};
 assert.equal(mergeHealth(channels,snapshot,now)[0].sources[0].videoId,undefined);
 snapshot.channels[0].channelId=c.sources[0].channelId;snapshot.checkedAt=new Date(now-21*60000).toISOString();
 assert.equal(mergeHealth(channels,snapshot,now)[0].sources[0].videoId,undefined);
 snapshot.checkedAt=new Date(now).toISOString();assert.equal(mergeHealth(channels,snapshot,now)[0].sources[0].videoId,'abcdefghijk');
});
