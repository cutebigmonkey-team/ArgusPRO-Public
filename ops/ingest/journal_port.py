"""Disabled bridge into an ALREADY OPEN IndexedFeedJournal and its DriveStore.

Never constructs a journal, Drive service, credentials, worker or writer lock.
Only the existing fenced writer may call it. Retains an immutable bounded input
receipt AFTER the journal's own independently verified delivery. No cursor writes.
"""
import hashlib
import json
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from envelope import canonical, MAX_BYTES, _iso
from partition import decode


class ExistingJournalPort:
    def __init__(self, journal, owner_guard, *, enabled=False):
        if not callable(owner_guard) or not hasattr(journal,'index') or not callable(getattr(journal,'deliver',None)):
            raise ValueError('existing_indexed_writer_required')
        self.journal=journal;self.store=journal.store;self.guard=owner_guard;self.enabled=enabled

    def _owned(self):
        if not self.enabled:raise ValueError('production_archive_bridge_disabled')
        if self.guard() is not True:raise ValueError('writer_unavailable')
        self.journal.verify_head()

    def _name(self,batch_id):
        if not isinstance(batch_id,str) or not re.fullmatch('[0-9a-f]{64}:[0-9]{1,3}',batch_id):
            raise ValueError('invalid_batch_id')
        return 'ingest-'+batch_id.replace(':','-')+'.json'

    def _verify_records(self, posts):
        try:
            self._read_records(posts)
        except (ValueError, KeyError, TypeError, UnicodeError) as error:
            if isinstance(error, ValueError) and str(error)=='writer_unavailable':raise
            raise ValueError('archive_readback_mismatch') from None

    def _read_records(self, posts):
        """Independently read current referenced archive bytes, never just receipt/index.

        Bound retained rows to this part. Walk the entire active tail one page at
        a time; only unchanged/compacted identities need a streaming base scan.
        The index supplies expected normalization, not evidence of persistence.
        """
        from feed_journal import read_page, sequence, MAX_REPLAY_PAGES, CHECKPOINT
        from publish import load_base_snapshot
        from url_identity import identity_keys
        self._owned()
        head=self.journal.head;checkpoint=self.journal.base_checkpoint
        expected={}
        for post in posts:
            saved=None
            for key in identity_keys(post):
                saved=self.journal.index.get(key)
                if saved is not None:break
            if (saved is None or saved.get('platform')!=post['platform'] or
                post['canonical_key'] not in {saved['id'], *saved.get('identity_aliases',[])}):
                raise ValueError('archive_readback_mismatch')
            expected[saved['id']]=saved
        found=set()
        def compare(row):
            sid=row.get('id')
            if sid in expected and sid not in found:
                if canonical(row)!=canonical(expected[sid]):
                    raise ValueError('archive_readback_mismatch')
                found.add(sid)
        cursor=head;pages=0;floor=sequence(checkpoint)
        while sequence(cursor)>floor:
            if pages>=MAX_REPLAY_PAGES:raise ValueError('archive_readback_mismatch')
            page=read_page(self.store,cursor)
            if cursor['first']<=floor:raise ValueError('archive_readback_mismatch')
            for row in page['items']:compare(row)
            cursor=page.get('previous');pages+=1
        if cursor!=checkpoint:raise ValueError('archive_readback_mismatch')
        if found!=set(expected):
            meta=load_base_snapshot(self.store,self.journal.base,item_consumer=compare)
            if meta.get(CHECKPOINT)!=checkpoint:raise ValueError('archive_readback_mismatch')
        if found!=set(expected):raise ValueError('archive_readback_mismatch')
        # Fence a concurrent writer/base change during the remote reads.
        self._owned()

    def import_candidate(self,body,*,batch_id):
        self._owned();raw=json.loads(body);part=decode(body,now=_iso(raw['created_at']))
        if batch_id!=f"{part['group_id']}:{part['index']}":raise ValueError('invalid_batch_id')
        name=self._name(batch_id)
        previous=self.store.read_bytes(name,MAX_BYTES)
        if previous is not None:
            if previous!=body:raise ValueError('archive_readback_mismatch')
            self._verify_records(part['posts'])
            return  # An existing input receipt never substitutes for archive proof.
        if not part['posts']:self.store.assert_private()
        if part['posts']:
            from storage import pack
            with tempfile.TemporaryDirectory(prefix='argus-ingest-') as directory:
                root=Path(directory);posts=root/'posts.jsonl';media=root/'media';media.mkdir()
                posts.write_bytes(b''.join(canonical(p)+b'\n' for p in part['posts']))
                package=root/'package.zip'
                pack(posts,media,package,created_at=part['created_at'])
                row={**part['source'],'posts':len(part['posts']),
                     'canonical_keys':[p['canonical_key'] for p in part['posts']],
                     'checked_at':datetime.now(timezone.utc).isoformat(),'attempts':[]}
                # IndexedFeedJournal's existing import-only path bypasses both
                # CursorJournal.commit and collection_state.commit. SQL decides
                # checkpoint readiness after ALL parts, never this package.
                report={'sources':[row],'state_updates':{},'permalink_backfill':True}
                self.journal.deliver(package,report)
        self._verify_records(part['posts'])
        self.store.put(name,body)  # Existing fenced writer handle; immutable object.
        if self.store.read_bytes(name,MAX_BYTES)!=body:raise ValueError('archive_readback_mismatch')

    def readback(self,batch_id):
        self._owned()
        body=self.store.read_bytes(self._name(batch_id),MAX_BYTES)
        if body is None:raise ValueError('archive_readback_mismatch')
        raw=json.loads(body);part=decode(body,now=_iso(raw['created_at']))
        if batch_id!=f"{part['group_id']}:{part['index']}":raise ValueError('archive_readback_mismatch')
        self._verify_records(part['posts'])
        return {'batch_id':batch_id,'sha256':hashlib.sha256(body).hexdigest(),'posts':part['posts']}
