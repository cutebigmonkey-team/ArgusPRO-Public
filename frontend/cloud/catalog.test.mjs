import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {validateCatalog,validateSnapshot,filterItems,summarize,captureLabel,readSnapshot} from './catalog.mjs';
const file={sha256:'a'.repeat(64),drive_id:'valid_file_123',size:10,name:'file.png'};
const items=[{id:'one',text:'Перевірка новини',platform:'rss',sample:false,observed_at:'2026-10-29T00:00:00Z',files:[file]},
 {id:'two',text:'Тестовий матеріал',platform:'tool-demo',sample:true,files:[file]}];
test('search previews cannot be labelled as collected public cards',()=>{
 assert.match(captureLabel({capture_mode:'search_preview',text_may_be_truncated:true}),/Лише пошукове прев’ю/);
 assert.equal(captureLabel({capture_mode:'visible_public_card',text_may_be_truncated:true}), '');
 assert.equal(captureLabel({}), '');
});

test('filters search, platform and sample data',()=>{
 assert.equal(filterItems(items,'НОВИНИ').length,1);
 assert.equal(filterItems(items,'','rss').length,1);
 assert.equal(filterItems(items,'','','production')[0].id,'one');
 assert.equal(filterItems(items,'','','demo')[0].id,'two');
});
test('analytics deduplicates shared media',()=>{
 const s=summarize(items);assert.equal(s.media,1);assert.equal(s.bytes,10);assert.equal(s.posts,2);assert.equal(s.sources,2);
});
test('catalog and snapshot fail closed on invalid metadata',()=>{
 assert.throws(()=>validateCatalog({format:'wrong'}));
 assert.throws(()=>validateCatalog({format:'argus-drive-catalog-v1',snapshot_id:'https://evil.test/',snapshot_sha256:'a'.repeat(64)}));
 assert.throws(()=>validateSnapshot({items:[items[0],items[0]],packages:[]}));
 assert.throws(()=>validateSnapshot({items:[{...items[0],files:[{...file,drive_id:'bad'}]}],packages:[]}));
 assert.equal(validateSnapshot({items,packages:[]}).items.length,2);
});

test('v1 and partitioned snapshots have identical posts and reject corrupted parts',async()=>{
 const snapshot={items,packages:['first']};
 const bytes=Buffer.from(JSON.stringify(snapshot));
 const sha=buffer=>createHash('sha256').update(buffer).digest('hex');
 const split=bytes.indexOf(Buffer.from('П'))+1;
 const chunks=[bytes.subarray(0,split),bytes.subarray(split)];
 const parts=chunks.map((chunk,i)=>({id:'archive_part_'+i,sha256:sha(chunk),size:chunk.length}));
 const catalog={format:'argus-drive-catalog-v2',snapshot_sha256:sha(bytes),snapshot_parts:parts,posts:2,packages:1};
 const read=async id=>chunks[parts.findIndex(p=>p.id===id)];
 assert.deepEqual(await readSnapshot(catalog,read,sha),snapshot);
 assert.deepEqual(await readSnapshot({format:'argus-drive-catalog-v1',snapshot_sha256:sha(bytes),snapshot_id:'archive_legacy_id'},async()=>bytes,sha),snapshot);
 await assert.rejects(readSnapshot({...catalog,posts:3},read,sha));
 await assert.rejects(readSnapshot({...catalog,snapshot_sha256:'0'.repeat(64)},read,sha));
 await assert.rejects(readSnapshot({...catalog,snapshot_parts:Array(17).fill(parts[0])},read,sha));
 await assert.rejects(readSnapshot(catalog,async id=>Buffer.alloc((await read(id)).length),sha));
});

test('archive row count is independent of the verified byte budget',()=>{
 const rows=Array.from({length:50001},(_,i)=>({...items[0],id:String(i)}));
 assert.equal(validateSnapshot({items:rows,packages:[]}).items.length,50001);
 const part={id:'archive_part_0',sha256:'a'.repeat(64),size:100};
 assert.equal(validateCatalog({format:'argus-drive-catalog-v2',snapshot_sha256:part.sha256,
   snapshot_parts:[part],posts:rows.length,packages:0}).posts,50001);
});
