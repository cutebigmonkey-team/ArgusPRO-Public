"""Small verified cursor deltas over the existing legacy cursor snapshot.

One archive session owner writes immutable pages then a single mutable pointer.
Periodic compaction preserves the original collection-cursors.json contract.
Before rolling back to a legacy writer, force compact and verify it while fenced.
"""
import hashlib
import json
import time
from drive_store import encoded,MAX_JSON
from feed_journal import sequence
from verified_replay import VerifiedReplay

BASE='collection-cursors.json'
HEAD='live-cursors.json'
CHECKPOINT='cursor_checkpoint'
CONTAINERS={'sources','incremental_sources','history_sources','polling',
            'incremental_polling','history_polling','first_news'}
MAX_PAGES=512


def clone(state):
    return {key:dict(value) if key in CONTAINERS else value for key,value in state.items()}


def patch(before,after):
    maps={};globals={};removed=[]
    for key,value in after.items():
        if key in CONTAINERS:
            prior=before.get(key,{})
            changed={sid:row for sid,row in value.items() if sid not in prior or prior[sid]!=row}
            deleted=[sid for sid in prior if sid not in value]
            if key not in before or changed or deleted:maps[key]={'set':changed,'delete':deleted}
        elif before.get(key)!=value or key not in before:globals[key]=value
    removed=[key for key in before if key not in after]
    return {'maps':maps,'globals':globals,'remove':removed}


def apply(state,delta):
    if not isinstance(delta,dict) or set(delta)!={'maps','globals','remove'}:raise ValueError('invalid_cursor_delta')
    result=clone(state)
    for key in delta['remove']:result.pop(key,None)
    result.update(delta['globals'])
    for key,change in delta['maps'].items():
        if key not in CONTAINERS or set(change)!={'set','delete'}:raise ValueError('invalid_cursor_container')
        rows=result.setdefault(key,{})
        for sid in change['delete']:rows.pop(sid,None)
        rows.update(change['set'])
    return result


class CursorJournal:
    def __init__(self,store):
        self.store=store
        before=self.identity()
        self.base=store.read_json(BASE)
        self.base_identity=self.identity()
        if before!=self.base_identity:raise ValueError('cursor_base_changed_during_startup')
        self.state=self.base or {'schema':'argus-collection-cursors-v1','sources':{}}
        self.head=store.read_json(HEAD);cursor=self.head
        checkpoint=self.state.get(CHECKPOINT);floor=sequence(checkpoint)
        if sequence(cursor)<floor:raise ValueError('cursor_head_rollback')
        with VerifiedReplay(MAX_PAGES) as pages:
            while sequence(cursor)>floor:
                page=self.read(cursor)
                if cursor['first']<=floor:raise ValueError('cursor_checkpoint_inside_page')
                pages.append(page);cursor=page['previous']
            if cursor!=checkpoint:raise ValueError('cursor_checkpoint_mismatch')
            for page in pages:self.state=apply(self.state,page['delta'])
            self.pages=len(pages)
        self.bytes=0;self.compacted=time.monotonic()
        self.verify()

    def identity(self):
        # Real Drive uses metadata only; no full cursor download per delivery.
        if hasattr(self.store,'find'):
            entry=self.store.find(BASE)
            return (entry.get('id'),entry.get('md5Checksum'),entry.get('size')) if entry else None
        value=self.store.read_json(BASE)  # fixture stores without Drive metadata API
        return hashlib.sha256(encoded(value)).hexdigest() if value is not None else None

    def verify(self):
        if self.store.read_json(HEAD)!=self.head:raise ValueError('cursor_writer_conflict')
        if self.identity()!=self.base_identity:raise ValueError('cursor_base_writer_conflict')

    def read(self,head):
        sequence(head)
        if head['first']!=head['last']:raise ValueError('invalid_cursor_sequence')
        raw=self.store.read_bytes('cursors-'+head['sha']+'.json',MAX_JSON)
        if raw is None or hashlib.sha256(raw).hexdigest()!=head['sha']:raise ValueError('cursor_page_checksum')
        page=json.loads(raw)
        if page.get('format')!='argus-cursor-delta-v1' or sequence(page.get('previous'))!=head['first']-1:raise ValueError('cursor_page_gap')
        return page

    def commit(self,state):
        self.verify()
        if self.pages>=MAX_PAGES-1:self.compact(force=True)
        # Compaction may have advanced only the snapshot checkpoint marker.
        state={**state}
        if CHECKPOINT in self.state:state[CHECKPOINT]=self.state[CHECKPOINT]
        delta=patch(self.state,state)
        if not any(delta.values()):return self.state
        page={'format':'argus-cursor-delta-v1','previous':self.head,'delta':delta}
        raw=encoded(page)
        if len(raw)>MAX_JSON:raise ValueError('cursor_delta_too_large')
        sha=hashlib.sha256(raw).hexdigest();file=self.store.put('cursors-'+sha+'.json',raw)
        head={'id':file,'sha':sha,'first':sequence(self.head)+1,'last':sequence(self.head)+1}
        if self.read(head)!=page:raise ValueError('cursor_page_readback')
        self.verify();self.store.json(HEAD,head,mutable=True)
        if self.store.read_json(HEAD)!=head:raise ValueError('cursor_head_readback')
        self.state=apply(self.state,delta);self.head=head;self.pages+=1;self.bytes+=len(raw)
        return self.state

    def compact(self,force=False):
        if not force and self.pages<120 and self.bytes<1024**2 and time.monotonic()-self.compacted<300:return False
        self.verify();state={**self.state,CHECKPOINT:self.head}
        self.store.json(BASE,state,mutable=True)
        if self.store.read_json(BASE)!=state:raise ValueError('cursor_compaction_readback')
        self.base=state;self.state=state;self.base_identity=self.identity()
        self.pages=0;self.bytes=0;self.compacted=time.monotonic()
        return True
