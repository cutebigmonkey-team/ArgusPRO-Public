import test from 'node:test';import assert from 'node:assert/strict';
import {TVPlayer} from './tv-player.mjs';
const flush=()=>new Promise(resolve=>setImmediate(resolve));
class Element {
 constructor(tag){this.tagName=tag;this.children=[];this.muted=true;this.paused=true;this.currentTime=0;}
 append(n){this.children.push(n);}
 replaceChildren(...children){this.children=children;}
 setAttribute(name,value){this[name]=value;}
 removeAttribute(name){delete this[name];}
 play(){this.paused=false;this.onplaying?.();return Promise.resolve();}
 pause(){this.paused=true;this.onpause?.();}
 load(){}
 canPlayType(){return '';}
}
function environment(t){
 t.mock.timers.enable({apis:['setTimeout','setInterval','Date'],now:1000000});
 const originals=Object.fromEntries(['document','window','YT','Hls','location'].map(k=>[k,globalThis[k]]));
 const instances=[];
 class YouTube {
  constructor(iframe,{events}){this.iframe=iframe;this.events=events;this.data={isLive:true,video_id:'abcdefghijk'};this.muted=true;this.time=0;instances.push(this);}
  getVideoData(){return this.data;}getPlayerState(){return this.state??-1;}getCurrentTime(){return this.time;}setVolume(v){this.volume=v;}mute(){this.muted=true;}unMute(){this.muted=false;}isMuted(){return this.muted;}
  playVideo(){this.played=true;}pauseVideo(){this.paused=true;}destroy(){this.destroyed=true;}
 }
 globalThis.document={createElement:tag=>new Element(tag)};globalThis.window={YT:{Player:YouTube}};globalThis.YT=window.YT;globalThis.location={origin:'https://argus.example'};
 t.after(()=>{for(const [key,value] of Object.entries(originals))if(value===undefined)delete globalThis[key];else globalThis[key]=value;t.mock.timers.reset();});
 return instances;
}
const source={id:'official',type:'youtube',channelId:'UCabcdefghijklmnopqrstuv'};
const channel={id:'news',name:'News',sources:[source]};
function player(t,overrides={}){const states=[];const p=new TVPlayer({host:new Element('div'),channel:structuredClone(channel),onState:s=>states.push(s),...overrides});t.after(()=>p.destroy());return {p,states};}
test('autoplay already running before API readiness still confirms LIVE',async t=>{
 const instances=environment(t),{states}=player(t);await flush();instances[0].state=1;instances[0].events.onReady();
 assert.equal(states.at(-1).status,'LIVE');
});
test('explicit source choice cancels pending recovery and connects the selected source',async t=>{
 const instances=environment(t),{p}=player(t,{channel:{...channel,sources:[source,{...source,id:'candidate',candidateVideoId:'abcdefghijk'}]}});await flush();
 instances[0].events.onError({data:5});p.selectSource('candidate');await flush();
 assert.match(instances[1].iframe.src,/embed\/abcdefghijk/);assert.equal(instances[0].destroyed,true);
 t.mock.timers.tick(4000);await flush();assert.equal(instances.length,2);
});
test('two wall players are independent; switching one destroys only its own instance',async t=>{
 const instances=environment(t),a=player(t),b=player(t);await flush();assert.equal(instances.length,2);
 instances[0].events.onReady();instances[1].events.onReady();assert.ok(instances.every(i=>i.muted));
 instances[0].events.onStateChange({data:1});assert.equal(a.states.at(-1).status,'LIVE');assert.equal(b.states.at(-1).status,'CONNECTING');
 a.p.destroy();assert.equal(instances[0].destroyed,true);assert.notEqual(instances[1].destroyed,true);
});
test('a finished broadcast is never accepted as LIVE',async t=>{
 const instances=environment(t),{p,states}=player(t);await flush();instances[0].data.isLive=false;instances[0].events.onStateChange({data:1});
 assert.equal(states.some(s=>s.status==='LIVE'),false);assert.equal(instances[0].destroyed,true);assert.equal(p.health.status,'RECONNECTING');
});
test('ending a fresh fixed live ID retries the channel resolver, never the recording',async t=>{
 const instances=environment(t),{p}=player(t,{channel:{...channel,sources:[{...source,videoId:'abcdefghijk',verifiedLiveAt:new Date().toISOString()}]}});await flush();
 assert.match(instances[0].iframe.src,/embed\/abcdefghijk/);instances[0].events.onStateChange({data:0});
 t.mock.timers.tick(4000);await flush();assert.match(instances[1].iframe.src,/embed\/live_stream\?/);assert.match(instances[1].iframe.src,/channel=UC/);
});
test('embed restriction terminates retries and exposes RESTRICTED',async t=>{
 const instances=environment(t),{p,states}=player(t);await flush();instances[0].events.onError({data:150});
 t.mock.timers.tick(200000);await flush();assert.equal(instances.length,1);assert.equal(states.at(-1).status,'RESTRICTED');assert.equal(p.health.attempt,0);
});
test('autoplay denial waits for user and does not repeatedly reconnect',async t=>{
 const instances=environment(t),{states}=player(t);await flush();instances[0].events.onAutoplayBlocked();t.mock.timers.tick(200000);
 assert.equal(instances.length,1);assert.equal(states.at(-1).status,'WAITING');
});
test('late callbacks from replaced players cannot overwrite the active channel',async t=>{
 const instances=environment(t),{p,states}=player(t);await flush();const old=instances[0];p.retry();await flush();old.events.onError({data:150});
 assert.equal(states.at(-1).status,'CONNECTING');instances[1].events.onStateChange({data:1});assert.equal(states.at(-1).status,'LIVE');
});
test('unknown live metadata pauses video rather than playing an unverified recording',async t=>{
 const instances=environment(t),{states}=player(t);await flush();instances[0].data={};instances[0].events.onStateChange({data:1});
 assert.equal(instances[0].paused,true);assert.equal(states.at(-1).status,'UNKNOWN');
});
test('reviewed broadcaster video IDs still require live playback and expire when ended',async t=>{
 const instances=environment(t),{p,states}=player(t,{channel:{...channel,sources:[{...source,candidateVideoId:'abcdefghijk'}]}});await flush();
 assert.match(instances[0].iframe.src,/embed\/abcdefghijk/);instances[0].data={};instances[0].events.onStateChange({data:1});
 assert.equal(states.at(-1).status,'UNKNOWN');assert.equal(instances[0].paused,true);
 instances[0].events.onStateChange({data:0});t.mock.timers.tick(4000);await flush();
 assert.match(instances[1].iframe.src,/embed\/live_stream\?/);assert.equal(p.sources[0].candidateVideoId,undefined);
});
test('muting and native YouTube audio intent are exposed to the wall controller',async t=>{
 const instances=environment(t);let intent=0;const {p}=player(t,{onAudioIntent:()=>intent++});await flush();
 p.setAudio(false,35);assert.equal(instances[0].muted,false);assert.equal(instances[0].volume,35);
 p.setAudio(true);instances[0].muted=false;t.mock.timers.tick(500);assert.equal(intent,1);
});
test('HLS VOD is rejected even if the manifest is valid',async t=>{
 environment(t);let hls;
 class Hls {static isSupported(){return true;}static Events={ERROR:'error',LEVEL_LOADED:'level',MANIFEST_PARSED:'manifest'};
 constructor(){hls=this;this.events={};}on(k,fn){this.events[k]=fn;}attachMedia(){}loadSource(){}destroy(){this.destroyed=true;}}
 globalThis.Hls=Hls;window.Hls=Hls;
 const {states}=player(t,{channel:{...channel,sources:[{id:'hls',type:'hls',url:'https://publisher.example/live.m3u8'}]}});await flush();
 hls.events.level(null,{details:{live:false}});assert.equal(hls.destroyed,true);assert.equal(states.at(-1).status,'RECONNECTING');
});
