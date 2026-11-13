"""Verified immutable Drive deltas over the existing archive, under its writer lock.

The public shell already reads live-feed.json with the user's Drive token.
Neither journal pages nor the archive are made public. A base snapshot is
compacted periodically; its checkpoint makes replay bounded after restart.
"""
import hashlib
import json
import re
import time
import io
import tempfile
from stream_json import load as stream_load
from drive_store import encoded, MAX_JSON, valid_id

HEAD = 'live-feed.json'
CHECKPOINT = 'live_checkpoint'
MAX_REPLAY_PAGES = 512


def sequence(head):
    if head is None:return 0
    if (not isinstance(head,dict) or type(head.get('first')) is not int or
            type(head.get('last')) is not int or not 0<head['first']<=head['last'] or
            not re.fullmatch('[a-f0-9]{64}',str(head.get('sha','')))):
        raise ValueError('invalid_feed_head')
    valid_id(head.get('id'))
    return head['last']


def read_page(store,head):
    sequence(head)
    raw=store.read_bytes('live-'+head['sha']+'.json',MAX_JSON)
    if raw is None or hashlib.sha256(raw).hexdigest()!=head['sha']:
        raise ValueError('feed_page_checksum')
    with io.TextIOWrapper(io.BytesIO(raw),encoding='utf-8') as text:
        page=stream_load(text)
    items=page.get('items')
    if (not isinstance(items,list) or len(items)!=head['last']-head['first']+1 or
            any(not isinstance(r,dict) or not isinstance(r.get('id'),str) or not r['id'] for r in items)):
        raise ValueError('feed_page_count')
    if sequence(page.get('previous'))!=head['first']-1:raise ValueError('feed_page_gap')
    return page


def replay(store,base,head=None):
    if head is None:head=store.read_json(HEAD)
    checkpoint=base.get(CHECKPOINT)
    floor=sequence(checkpoint); end=sequence(head)
    if end<floor:raise ValueError('feed_head_rollback')
    # Keep verified pages on disposable disk while walking backwards. Retaining
    # 512 decoded pages alongside the reconstructed archive defeats the cap.
    with tempfile.TemporaryFile(mode='w+b') as spool:
        pages=[];cursor=head
        while sequence(cursor)>floor:
            if len(pages)>=MAX_REPLAY_PAGES:raise ValueError('feed_compaction_required')
            page=read_page(store,cursor)
            if cursor['first']<=floor:raise ValueError('feed_checkpoint_inside_page')
            raw=encoded(page);offset=spool.tell();spool.write(raw)
            pages.append((offset,len(raw)));cursor=page.get('previous')
            del page,raw
        if cursor!=checkpoint:raise ValueError('feed_checkpoint_mismatch')
        if not pages:return base
        items={p['id']:p for p in base['items']}
        if len(items)!=len(base['items']):raise ValueError('duplicate_base_id')
        packages=list(base['packages']);deltas=dict(base.get('collection_deltas',{}))
        updated=base.get('updated_at')
        for offset,size in reversed(pages):
            spool.seek(offset)
            with io.TextIOWrapper(io.BytesIO(spool.read(size)),encoding='utf-8') as text:
                page=stream_load(text,item_consumer=lambda item:items.__setitem__(item['id'],item))
            package=page.get('package_id')
            if package and package not in packages:packages.append(package)
            if package:deltas[package]=page.get('new_keys',[])
            updated=page.get('updated_at',updated)
        recent={p:deltas[p] for p in packages if p in deltas}
        return {**base,'items':list(items.values()),'packages':packages,'updated_at':updated,
                'collection_deltas':dict(list(recent.items())[-8:]),CHECKPOINT:head}



class FeedJournal:
    def __init__(self,store,*,observe=lambda phase:None):
        from publish import load_snapshot
        self.store=store
        observe('feed_load_before')
        self.snapshot=load_snapshot(store)
        observe('feed_load_after')
        self.head=store.read_json(HEAD)
        if self.snapshot.get(CHECKPOINT)!=self.head:raise ValueError('feed_changed_during_startup')
        self.base=store.read_json('catalog.json')
        self.compacted=time.monotonic()
        self.pages=0; self.bytes=0
        self.last_write_seconds=None
        self.verified_state=None;self.monitor=None;self.catalog=None
        from analytics_snapshot import AnalyticsIndex
        observe('analytics_init_before')
        self.analytics=AnalyticsIndex(self.snapshot['items'],(store.read_json('sources.json') or {}).get('sources',[]))
        observe('analytics_init_after')

    def verify_head(self):
        if self.store.read_json(HEAD)!=self.head:raise ValueError('feed_writer_conflict')
        if self.store.read_json('catalog.json')!=self.base:raise ValueError('archive_writer_conflict')

    def commit(self,snapshot,previous,base):
        """Only remotely read-back bytes enter the cached acknowledged snapshot."""
        started=time.monotonic()
        self.verify_head()
        old={p['id']:p for p in previous['items']}
        # Existing normalized rows are reused by identity; only changed/new rows
        # are copied into the immutable journal, including edits and origin fixes.
        changed=[p for p in snapshot['items'] if p is not old.get(p['id']) and p!=old.get(p['id'])]
        if not changed:raise ValueError('empty_feed_commit')
        changed.sort(key=lambda p:(p.get('published_at') or '',p['id']),reverse=True)
        package=snapshot['packages'][-1]
        metadata={'package_id':package,
                  'new_keys':snapshot.get('collection_deltas',{}).get(package,[]),
                  'updated_at':snapshot['updated_at']}
        previous_head=self.head;count=0;written=0;batch=[];batch_bytes=0
        def upload(rows,previous):
            page={**metadata,'previous':previous,'items':rows}
            raw=encoded(page)
            if len(raw)>MAX_JSON:raise ValueError('feed_item_too_large')
            sha=hashlib.sha256(raw).hexdigest()
            file=self.store.put('live-'+sha+'.json',raw)
            new={'id':file,'sha':sha,'first':sequence(previous)+1,'last':sequence(previous)+len(rows)}
            if read_page(self.store,new)!=page:raise ValueError('feed_page_readback')
            return new,len(raw)
        # Budget the actual canonical normalized bytes, including envelope,
        # previous-head descriptor and commas. No raw-input expansion estimate.
        for item in changed:
            item_bytes=len(encoded(item))
            envelope=len(encoded({**metadata,'previous':previous_head,'items':[]}))
            if batch and envelope+batch_bytes+item_bytes+len(batch)>MAX_JSON:
                previous_head,size=upload(batch,previous_head)
                written+=size;count+=1;batch=[];batch_bytes=0
                envelope=len(encoded({**metadata,'previous':previous_head,'items':[]}))
            if envelope+item_bytes>MAX_JSON:raise ValueError('feed_item_too_large')
            batch.append(item);batch_bytes+=item_bytes
        if batch:
            previous_head,size=upload(batch,previous_head);written+=size;count+=1
        if self.pages+count>MAX_REPLAY_PAGES:
            raise ValueError('feed_compaction_required')
        # No intermediate HEAD is exposed: all bounded immutable pages have
        # independent readback before one atomic visibility/acknowledgement point.
        self.verify_head()
        self.store.json(HEAD,previous_head,mutable=True)
        if self.store.read_json(HEAD)!=previous_head:raise ValueError('feed_head_readback')
        # Every changed value was compared with independently decoded remote
        # bytes. Reuse those equal values instead of decoding the entire archive.
        self.snapshot={**snapshot,CHECKPOINT:previous_head}
        self.head=previous_head;self.pages+=count;self.bytes+=written
        self.analytics.update(changed)
        self.last_write_seconds=round(time.monotonic()-started,3)

    def publish_analytics(self):
        self.verify_head()
        return self.analytics.publish(self.store,self.base,self.head,self.snapshot.get('updated_at'))

    def deliver(self,package,report):
        from publish import publish,confirm_collection
        from collection_state import commit
        from publish_incremental import write_monitor
        self.verify_head()
        result=publish(self.store,package,measure=True,changes_only=True,
                       previous_snapshot=self.snapshot,commit_callback=self.commit)
        self.verify_head()
        confirm_collection(self.snapshot,result,report)
        if not report.get('permalink_backfill'):
            self.verified_state=commit(self.store,report,snapshot=self.snapshot,return_state=True)
        else:self.verified_state=None
        self.monitor=write_monitor(self.store,report,self.snapshot,verified_state=self.verified_state,catalog=self.catalog)

    def compact(self,force=False):
        if not force and self.pages<120 and self.bytes<16*1024**2 and time.monotonic()-self.compacted<3600:return False
        from publish import commit_snapshot,load_base_snapshot
        self.verify_head()
        # The old immutable pages remain available for in-flight clients. No
        # archive deletion is needed for a checkpointed compact snapshot.
        commit_snapshot(self.store,self.snapshot,base=self.base)
        base=self.store.read_json('catalog.json')
        expected=iter(self.snapshot['items']);count=0
        def compare(item):
            nonlocal count
            if item!=next(expected,None):raise ValueError('compaction_readback')
            count+=1
        checked=load_base_snapshot(self.store,base,item_consumer=compare)
        if count!=len(self.snapshot['items']) or {k:v for k,v in checked.items() if k!='items'}!={k:v for k,v in self.snapshot.items() if k!='items'}:
            raise ValueError('compaction_readback')
        self.base=base
        self.compacted=time.monotonic();self.pages=0;self.bytes=0
        return True
