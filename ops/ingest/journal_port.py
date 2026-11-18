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

    def import_candidate(self,body,*,batch_id):
        self._owned();raw=json.loads(body);part=decode(body,now=_iso(raw['created_at']))
        if batch_id!=f"{part['group_id']}:{part['index']}":raise ValueError('invalid_batch_id')
        name=self._name(batch_id)
        previous=self.store.read_bytes(name,MAX_BYTES)
        if previous is not None:
            if previous!=body:raise ValueError('archive_readback_mismatch')
            return  # The input receipt is written only after journal delivery.
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
        self._owned()
        self.store.put(name,body)  # Existing fenced writer handle; immutable object.
        if self.store.read_bytes(name,MAX_BYTES)!=body:raise ValueError('archive_readback_mismatch')

    def readback(self,batch_id):
        self._owned()
        body=self.store.read_bytes(self._name(batch_id),MAX_BYTES)
        if body is None:raise ValueError('archive_readback_mismatch')
        raw=json.loads(body);part=decode(body,now=_iso(raw['created_at']))
        if batch_id!=f"{part['group_id']}:{part['index']}":raise ValueError('archive_readback_mismatch')
        return {'batch_id':batch_id,'sha256':hashlib.sha256(body).hexdigest(),'posts':part['posts']}
