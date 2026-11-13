import test from 'node:test';
import assert from 'node:assert/strict';
import {matches,candidatePages,groupComments,threadOrder,sameCommentPages,createComments} from './comments.mjs';
const row={id:'1',post_id:'p1',platform:'telegram',source_id:'s',source_name:'Джерело',text:'Пожежа ліквідована',author:'Автор',published_at:'2026-11-04T23:30:00Z',sentiment:'unknown'};
test('comment filters combine Ukrainian search and Kyiv publication day',()=>{
  assert.equal(matches(row,{query:'ПОЖЕЖА',platform:'telegram',source:'s',from:'2026-11-05',to:'2026-11-05',post:'p1'}),true);
  assert.equal(matches(row,{from:'2026-11-06'}),false);
  assert.equal(matches(row,{sentiment:'positive'}),false);
  assert.equal(matches(row,{post:'other'}),false);
});

test('deep threads are iterative and invalid dates do not crash filtering',()=>{
  const rows=Array.from({length:20000},(_,i)=>({...row,id:String(i),parent_id:i?String(i-1):null}));
  assert.equal(threadOrder(rows).length,20000);
  assert.equal(matches({...row,published_at:'bad'},{from:'2026-11-05'}),false);
  assert.equal(sameCommentPages([{sha256:'a'}],[{sha256:'a'}]),true);
  assert.equal(sameCommentPages([{sha256:'a'}],[{sha256:'b'}]),false);
});

// Minimal DOM boundary: exercise the actual async controller and retained nodes.
class Element {
  constructor(tag){this.tag=tag;this.children=[];this.style={};this.dataset={};this.attrs={};this.isConnected=true;}
  append(...nodes){this.children.push(...nodes);}
  replaceChildren(...nodes){this.children=nodes;}
  setAttribute(k,v){this.attrs[k]=v;}
  add(option){this.children.push(option);}
  closest(){return null;}
  getBoundingClientRect(){return {top:0};}
  querySelectorAll(selector){return this.descendants().filter(n=>(n.className||'').split(' ').includes(selector.slice(1)));}
  descendants(){return this.children.flatMap(n=>[n,...(n.descendants?.()||[])]);}
}
function dom(t){
  const main=new Element('main');
  for(const [key,value] of Object.entries({document:{createElement:tag=>new Element(tag),getElementById:id=>main.descendants().find(n=>n.id===id)},
    location:{hash:'#/comments'},window:{scrollBy(){}},Option:class {constructor(text,value){this.text=text;this.value=value;}}})){
    const descriptor=Object.getOwnPropertyDescriptor(globalThis,key);
    Object.defineProperty(globalThis,key,{configurable:true,writable:true,value});
    t.after(()=>{if(descriptor)Object.defineProperty(globalThis,key,descriptor);else delete globalThis[key];});
  }
  return main;
}
const settle=()=>new Promise(resolve=>setImmediate(resolve));
const descriptor=id=>({id,sha256:id,platforms:['telegram'],sources:['s'],posts:['p1'],first:row.published_at,last:row.published_at});

test('live updates preserve loaded discussions, controls, and filters',async t=>{
  const main=dom(t);let catalog={revision:'1',pages:['a','b','c','d'].map(descriptor),metrics:{comments:4},sources:{}};
  const reads=[];
  const controller=createComments({readCommentsCatalog:async()=>catalog,readCommentsPage:async p=>{reads.push(p.id);return [{...row,id:p.id,text:'Comment '+p.id}];}});
  await controller.mount(main);await settle();
  const section=main.children[0];
  const more=main.descendants().find(n=>n.textContent==='Завантажити наступні коментарі');
  await more.onclick();
  assert.equal(main.querySelectorAll('.comment-row').length,4);
  catalog={...catalog,revision:'health-only'};
  await controller.refresh();
  assert.equal(main.children[0],section);
  assert.equal(main.querySelectorAll('.comment-row').length,4);
  assert.equal(reads.length,4);
  catalog={...catalog,revision:'new',pages:[descriptor('e'),...catalog.pages]};
  await controller.refresh();
  assert.equal(main.children[0],section);
  assert.equal(main.querySelectorAll('.comment-row').length,5);
  assert.equal(reads.length,5);
});

test('refresh is single flight and a late read cannot repopulate after logout',async t=>{
  const main=dom(t);let release;let calls=0;
  const catalog={revision:'1',pages:[],sources:{}};
  const drive={readCommentsCatalog:async()=>catalog,readCommentsPage:async()=>[]};
  const controller=createComments(drive);await controller.mount(main);await settle();
  drive.readCommentsCatalog=()=>{calls++;return new Promise(resolve=>{release=resolve;});};
  const first=controller.refresh();await controller.refresh();assert.equal(calls,1);
  controller.clear();release({...catalog,revision:'2',pages:[descriptor('new')]});await first;
  assert.equal(main.querySelectorAll('.comment-row').length,0);
});

test('new replies on unread old pages appear before unchanged backlog and drain across polls',async t=>{
  const main=dom(t);const initial=Array.from({length:20},(_,i)=>descriptor('old'+i));
  let catalog={revision:'1',pages:initial,metrics:{comments:20},sources:{}};
  const reads=[];let failOnce=true;
  const controller=createComments({readCommentsCatalog:async()=>catalog,readCommentsPage:async p=>{
    if(p.id==='new3'&&failOnce){failOnce=false;throw new Error('temporary read failure');}
    reads.push(p.id);return [{...row,id:p.id}];
  }});
  await controller.mount(main);await settle();const section=main.children[0];
  const changed=Array.from({length:4},(_,i)=>({...descriptor('new'+i),last:'2025-01-01T00:00:00Z'}));
  catalog={...catalog,revision:'2',pages:[...initial.slice(0,16),...changed]};
  await controller.refresh();
  assert.deepEqual(reads,['old0','old1','old2','new0','new1','new2']);
  await controller.refresh(); // Failed fourth page stays pending even without a catalog revision.
  await controller.refresh();
  assert.deepEqual(reads,['old0','old1','old2','new0','new1','new2','new3']);
  await controller.refresh();assert.equal(reads.length,7);
  assert.equal(main.children[0],section);
  assert.equal(main.querySelectorAll('.comment-row').length,7);
});
test('page metadata prunes platform source post and date without downloading bodies',()=>{
  const p={id:'page',platforms:['telegram'],sources:['s'],posts:['p1'],first:row.published_at,last:row.published_at};
  const catalog={pages:[p,{...p,id:'other',platforms:['youtube']}]};
  assert.equal(candidatePages(catalog,{platform:'telegram',post:'p1',from:'2026-11-05'}).length,1);
  assert.equal(candidatePages(catalog,{source:'else'}).length,0);
});
test('one discussion spans pages, deduplicates rows and orders replies under parents',()=>{
  const rows=[{...row,id:'reply',parent_id:'root'},{...row,id:'other',post_id:'p2'},
    {...row,id:'root'},{...row,id:'reply',parent_id:'root',text:'Updated'}];
  const groups=groupComments(rows);
  assert.equal(groups.length,2);assert.equal(groups[0].comments.length,2);
  assert.deepEqual(threadOrder(groups[0].comments).map(({row,depth})=>[row.id,depth]),[['root',0],['reply',1]]);
  assert.equal(groups[0].comments[0].text,'Updated');
  assert.equal(threadOrder([{...row,id:'a',parent_id:'b'},{...row,id:'b',parent_id:'a'}]).length,2);
});
