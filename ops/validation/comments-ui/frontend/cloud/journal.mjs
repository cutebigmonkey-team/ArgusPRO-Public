import {fileId,validateSnapshot} from './catalog.mjs';

// Immutable pages are linked backwards. Advance the local cursor only when every
// page down to the prior committed sequence has passed validation.
export function createJournal() {
  let last=0, digest=null, overlay=new Map(), previousBase, result, latestUpdated;
  return async function read(base,head,load) {
    // A verified base contains every event through its compacted checkpoint.
    // Never fetch that history again on cold start; keep the immutable chain
    // for clients already reading the prior base during publication.
    const checkpoint=base.live_checkpoint;
    if(checkpoint && checkpoint.last>last) {
      if(!fileId(checkpoint.id) || !/^[a-f0-9]{64}$/.test(checkpoint.sha) || !Number.isSafeInteger(checkpoint.last)) throw new Error('Невірний checkpoint LIVE.');
      last=checkpoint.last;digest=checkpoint.sha;overlay=new Map();result=null;latestUpdated=base.updated_at;
    }
    if(!head && last) throw new Error('Відсутній журнал LIVE.');
    if(head && (head.last<last || (head.last===last && head.sha!==digest))) throw new Error('Розрив журналу LIVE.');
    if(head && head.last!==last) {
      const pages=[];let nextUpdated;
      let cursor=head, expected=head.last;
      while(expected>last) {
        if(!cursor || !fileId(cursor.id) || !/^[a-f0-9]{64}$/.test(cursor.sha) ||
           !Number.isSafeInteger(cursor.first) || !Number.isSafeInteger(cursor.last) ||
           cursor.last!==expected || cursor.first<=last || cursor.first>expected) throw new Error('Неповний журнал LIVE.');
        const page=await load(cursor);
        if(!pages.length)nextUpdated=page.updated_at;
        validateSnapshot({items:page.items,packages:[]},false);
        if(page.items.length!==cursor.last-cursor.first+1 || page.items.some(p=>typeof p.id!=='string'||!p.id)) throw new Error('Неповна сторінка LIVE.');
        pages.push(page.items);
        expected=cursor.first-1; cursor=page.previous;
      }
      if((cursor?.sha??null)!==digest || (cursor?.last??0)!==last) throw new Error('Журнал LIVE змінився.');
      for(const page of pages.reverse()) for(const row of page) overlay.set(row.id,row);
      last=head.last; digest=head.sha; result=null;latestUpdated=nextUpdated||base.updated_at;
    }
    if(!overlay.size) return base;
    if(result && base===previousBase) return result;
    const items=new Map(base.items.map(p=>[p.id,p])), aliases=new Map();
    for(const p of base.items) for(const k of [p.id,...(p.identity_aliases||[])]) aliases.set(k,p.id);
    for(const row of overlay.values()) {
      const keys=[row.id,...(row.identity_aliases||[])];
      const id=keys.map(k=>aliases.get(k)).find(Boolean)||row.id, old=items.get(id);
      const newer=!old || Date.parse(row.observed_at)>=Date.parse(old.observed_at);
      const merged=newer?{...old,...row,id}: {...row,...old,id};
      if(old?.files?.length) merged.files=old.files;
      if(old?.discovered_at && Date.parse(old.discovered_at)<Date.parse(merged.discovered_at)) merged.discovered_at=old.discovered_at;
      if(old?.text && (!merged.text || (old.text_may_be_truncated===false && row.text_may_be_truncated===true))) merged.text=old.text;
      items.set(id,merged);
      for(const k of keys) aliases.set(k,id);
    }
    previousBase=base;
    result={...base,items:[...items.values()],liveSequence:last,live_sha256:digest,updated_at:latestUpdated||base.updated_at};
    return result;
  };
}
