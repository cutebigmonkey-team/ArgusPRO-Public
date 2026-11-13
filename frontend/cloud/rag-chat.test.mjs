import test from 'node:test';
import assert from 'node:assert/strict';
import {safeLink,readEvents,createRagChat} from './rag-chat.mjs';
const stream=(chunks)=>new ReadableStream({start(controller){for(const chunk of chunks)controller.enqueue(new TextEncoder().encode(chunk));controller.close();}});
test('source links reject scripts and embedded credentials',()=>{assert.equal(safeLink('javascript:alert(1)'),'');assert.equal(safeLink('https://key@host.test/a'),'');assert.equal(safeLink('https://t.me/test/12'),'https://t.me/test/12');});
test('SSE survives split frames and preserves Ukrainian',async()=>{const events=[];await readEvents(stream(['data: {"type":"del','ta","text":"Привіт"}\n','\ndata: {"type":"done"}\n\n']),event=>events.push(event));assert.equal(events[0].text,'Привіт');assert.equal(events.length,2);});
test('interrupted stream never pretends completion',async()=>{await assert.rejects(()=>readEvents(stream(['data: {"type":"delta","text":"a"}\n\n']),()=>{}),/перервано/);});
test('rate limit has clear user message',async()=>{await assert.rejects(()=>readEvents(stream(['data: {"type":"error","code":"rate_limit"}\n\n']),()=>{}),/Ліміт/);});
test('abort stops stream',async()=>{const controller=new AbortController();controller.abort();await assert.rejects(()=>readEvents(stream([]),()=>{},controller.signal),{name:'AbortError'});});
import {dateFilters} from './rag-chat.mjs';
test('date filters use Kyiv and preserve daylight-saving day lengths',()=>{
  const spring=dateFilters('2026-03-29','2026-03-29');
  assert.equal(spring.from,'2026-03-28T22:00:00.000Z');assert.equal(spring.to,'2026-03-29T21:00:00.000Z');
  const autumn=dateFilters('2026-10-25','2026-10-25');
  assert.equal(autumn.from,'2026-10-24T21:00:00.000Z');assert.equal(autumn.to,'2026-10-25T22:00:00.000Z');
});

function chatDom(t){
  class Element{
    constructor(tag){this.tag=tag;this.children=[];this.value='';this.isConnected=true;}
    append(...items){this.children.push(...items);}
    replaceChildren(...items){this.children=items;}
    setAttribute(key,value){this[key]=value;}
    add(item){this.children.push(item);}
    remove(){} focus(){}
  }
  const prior=Object.fromEntries(['window','document','Option'].map(k=>[k,globalThis[k]]));
  globalThis.window={addEventListener(){}};
  globalThis.document={createElement:tag=>new Element(tag),createTextNode:text=>({textContent:text})};
  globalThis.Option=class extends Element{constructor(text,value){super('option');this.textContent=text;this.value=value;}};
  t.after(()=>{for(const [k,v] of Object.entries(prior)){if(v===undefined)delete globalThis[k];else globalThis[k]=v;}});
  const main=new Element('main');
  const find=predicate=>{const walk=node=>predicate(node)?node:node.children?.map(walk).find(Boolean);return walk(main);};
  return {main,find};
}

test('an unavailable server never sends a message or enables a new conversation',async t=>{
  const {main,find}=chatDom(t),calls=[];
  const chat=createRagChat(async path=>{calls.push(path);throw new Error('AI-сервер ще не підключено.');});
  await chat.mount(main);
  assert.equal(find(n=>n.textContent==='Надіслати').disabled,true);
  assert.equal(find(n=>n.tag==='button'&&n.textContent==='Нова розмова').disabled,true);
  find(n=>n.tag==='textarea').value='Привіт';
  await find(n=>n.tag==='form').onsubmit({preventDefault(){}});
  assert.deepEqual(calls,['/api/ai/status']);
  assert.equal(find(n=>n.role==='status').textContent,'AI-сервер ще не підключено.');
});

test('retry enables chat only after the API becomes reachable',async t=>{
  const {main,find}=chatDom(t);let online=false;
  const chat=createRagChat(async path=>{if(!online)throw new Error('offline');return Response.json(path.endsWith('/status')?{providers:{configured:true}}:{conversations:[]});});
  await chat.mount(main);online=true;
  find(n=>n.textContent==='Перевірити підключення').onclick();
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(find(n=>n.textContent==='Надіслати').disabled,false);
  assert.equal(find(n=>n.textContent==='Перевірити підключення').hidden,true);
});

test('leaving the AI route aborts a pending check and ignores its late response',async t=>{
  const {main,find}=chatDom(t);let resolveStatus,signal;
  const chat=createRagChat((_path,options)=>{signal=options.signal;return new Promise(resolve=>{resolveStatus=resolve;});});
  const mounting=chat.mount(main);chat.unmount();
  assert.equal(signal.aborted,true);
  resolveStatus(Response.json({providers:{configured:true}}));await mounting;
  assert.equal(find(n=>n.textContent==='Надіслати').disabled,true);
});
