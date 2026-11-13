"""Existing journal protocol with a disposable disk index instead of a RAM archive.

One delivery lane owns this instance and its Drive client. SQL pending/leases
remain exclusively owned by the supervisor, never by the delivery thread.
"""
import copy
import tempfile
import time
from pathlib import Path
from archive_index import ArchiveIndex,Rows
from feed_journal import FeedJournal,HEAD,CHECKPOINT,MAX_REPLAY_PAGES,read_page,sequence
from publish import load_base_snapshot,publish,confirm_collection,now
from storage import validate_package
from verified_replay import VerifiedReplay


class IndexedFeedJournal(FeedJournal):
    def __init__(self,store,*,observe=lambda phase:None,restore_cursors=True):
        self.store=store
        self.directory=tempfile.TemporaryDirectory(prefix='argus-read-model-')
        self.index=None
        try:
            self.index=ArchiveIndex(str(Path(self.directory.name)/'archive.sqlite'))
            observe('index_bootstrap_before')
            self.base=store.read_json('catalog.json')
            self.head=store.read_json(HEAD)
            with self.index.db,VerifiedReplay(MAX_REPLAY_PAGES) as pages:
                meta=load_base_snapshot(store,self.base,item_consumer=lambda row:self.index.put(row,initial=True))
                floor=sequence(meta.get(CHECKPOINT));cursor=self.head
                # Only descriptors remain in memory; each decoded page is discarded.
                while sequence(cursor)>floor:
                    if len(pages)>=MAX_REPLAY_PAGES:raise ValueError('feed_compaction_required')
                    page=read_page(store,cursor)
                    if cursor['first']<=floor:raise ValueError('feed_checkpoint_inside_page')
                    pages.append(page);cursor=page.get('previous')
                if cursor!=meta.get(CHECKPOINT):raise ValueError('feed_checkpoint_mismatch')
                for page in pages:
                    for row in page['items']:self.index.put(row)
                    package=page.get('package_id')
                    if package and package not in meta['packages']:meta['packages'].append(package)
                    if package:meta.setdefault('collection_deltas',{})[package]=page.get('new_keys',[])
                    meta['updated_at']=page.get('updated_at',meta.get('updated_at'))
                    del page
            meta['collection_deltas']=dict(list(meta.get('collection_deltas',{}).items())[-8:])
            self.snapshot={**meta,'items':Rows(self.index),CHECKPOINT:self.head}
            self.index_head=self.head;self.index_snapshot=dict(self.snapshot)
            self.verify_head()
            self.compacted=time.monotonic();self.pages=len(pages);self.bytes=0
            self.last_write_seconds=None;self.verified_state=None;self.monitor=None;self.catalog=None
            self.cursor=None
            if restore_cursors:self.restore_cursors()
            from analytics_snapshot import AnalyticsIndex
            self.analytics=AnalyticsIndex(self.index.rows(),(store.read_json('sources.json') or {}).get('sources',[]))
            self.changed=[];self.reconciled=False
            observe('index_bootstrap_after')
        except BaseException:
            self.close();raise

    def restore_cursors(self):
        from cursor_journal import CursorJournal
        self.cursor=CursorJournal(self.store);self.verified_state=self.cursor.state

    def commit(self,snapshot,previous,base):
        # Existing upload, page integrity, fencing and final HEAD readback protocol.
        FeedJournal.commit(self,snapshot,previous,base)
        with self.index.db:
            for item in snapshot['items']:self.index.put(item)
        self.changed=snapshot['items']
        self.snapshot={**self.snapshot,'items':Rows(self.index)}
        self.index_head=self.head;self.index_snapshot=dict(self.snapshot)

    def deliver(self,package,report):
        if getattr(self,'invalid',False):
            self.reconcile()
        try:return self._deliver(package,report)
        except BaseException:
            # A lost HEAD acknowledgement is ambiguous: rebuild from Drive
            # before retry, never advance SQL or guess that the upload failed.
            self.invalid=True;raise

    def reconcile(self):
        # Failed upload/readback does not imply the remote write failed. Reconcile
        # only the verified tail since our last committed local index checkpoint.
        if self.store.read_json('catalog.json')!=self.base:
            # A genuinely different compacted base requires full verification.
            store=self.store;catalog=self.catalog;self.close();self.__init__(store);self.catalog=catalog
            self.invalid=False;self.reconciled=True;return
        head=self.store.read_json(HEAD);cursor=head;meta=dict(self.index_snapshot)
        with VerifiedReplay(MAX_REPLAY_PAGES) as pages:
            while sequence(cursor)>sequence(self.index_head):
                page=read_page(self.store,cursor);pages.append(page);cursor=page.get('previous')
            if cursor!=self.index_head:raise ValueError('feed_recovery_checkpoint_mismatch')
            with self.index.db:
                for page in pages:
                    for row in page['items']:self.index.put(row)
                    package=page.get('package_id')
                    if package and package not in meta['packages']:meta['packages']=[*meta['packages'],package]
                    if package:meta['collection_deltas']={**meta.get('collection_deltas',{}),package:page.get('new_keys',[])}
                    meta['updated_at']=page.get('updated_at',meta.get('updated_at'))
                if self.store.read_json(HEAD)!=head or self.store.read_json('catalog.json')!=self.base:
                    raise ValueError('feed_changed_during_recovery')
            self.pages+=len(pages);self.bytes+=pages.size
        meta['collection_deltas']=dict(list(meta.get('collection_deltas',{}).items())[-8:])
        self.head=head;self.index_head=head
        self.snapshot={**meta,'items':Rows(self.index),CHECKPOINT:head}
        self.index_snapshot=dict(self.snapshot)
        from cursor_journal import CursorJournal
        self.cursor=CursorJournal(self.store);self.verified_state=self.cursor.state
        from analytics_snapshot import AnalyticsIndex
        self.analytics=AnalyticsIndex(self.index.rows(),(self.catalog or self.store.read_json('sources.json') or {}).get('sources',[]))
        self.changed=[];self.verify_head();self.invalid=False;self.reconciled=True

    def _deliver(self,package,report):
        from collection_state import commit
        if self.pages>=MAX_REPLAY_PAGES-4:self.compact(force=True)
        self.verify_head();self.changed=[]
        # Normalization/dedup touches only identities in this bounded package.
        with tempfile.TemporaryDirectory() as temp,validate_package(package,temp) as (manifest,posts,_):
            previous={**self.snapshot,'items':self.index.touched(posts)}
            package_id=manifest['package_id']
        result=publish(self.store,package,measure=True,changes_only=True,
                       previous_snapshot=previous,commit_callback=self.commit,
                       import_time=report.setdefault('archive_attempt_at',now()))
        self.verify_head()
        with tempfile.TemporaryDirectory() as temp,validate_package(package,temp) as (_,posts,_):
            touched={**self.snapshot,'items':self.index.touched(posts)}
        confirm_collection(touched,result,report)
        if not report.get('permalink_backfill'):
            self.verified_state=commit(self.store,report,snapshot=self.snapshot,return_state=True,
                                       identities=self.index,news_items=self.index.news(),
                                       previous_state=self.cursor.state,persist=self.cursor.commit,commit_key=package_id)
        return {'state':copy.deepcopy(self.verified_state),'changed':self.changed,
                'archive_posts':self.index.count(),'feed_sequence':sequence(self.head)}

    def maintenance(self,report):
        # Throttled separately from microbatch delivery; streams rows from disk.
        from publish_incremental import write_monitor
        self.compact()
        self.cursor.compact()
        self.publish_analytics()
        self.monitor=write_monitor(self.store,report,self.snapshot,
                                   verified_state=self.verified_state,catalog=self.catalog)
        return self.monitor

    def close(self):
        if self.index is not None:self.index.close()
        self.directory.cleanup()
