import copy
import hashlib
import unittest
from unittest.mock import patch
from test_publish import MemoryStore
from cursor_journal import CursorJournal,BASE,HEAD,CHECKPOINT


class MetadataStore(MemoryStore):
    def find(self,name):
        raw=self.files.get(name)
        return {'id':'fixture_'+hashlib.sha256(name.encode()).hexdigest(),
                'md5Checksum':hashlib.md5(raw).hexdigest(),'size':str(len(raw))} if raw is not None else None


class CursorJournalTests(unittest.TestCase):
    def test_small_delta_does_not_read_or_rewrite_full_cursor_snapshot(self):
        store=MetadataStore()
        base={'schema':'argus-collection-cursors-v1','sources':{},'incremental_polling':
              {str(i):{'url':'https://fixture.example/'+str(i),'metadata':'x'*4000} for i in range(1000)}}
        store.json(BASE,base);journal=CursorJournal(store);original=store.files[BASE]
        next_state=copy.deepcopy(base);next_state['incremental_polling']['42']['metadata']='changed'
        read=store.read_json
        def no_full(name):
            if name==BASE:raise AssertionError('full cursor read during microbatch')
            return read(name)
        with patch.object(store,'read_json',side_effect=no_full):journal.commit(next_state)
        self.assertEqual(store.files[BASE],original)
        page=store.files['cursors-'+journal.head['sha']+'.json']
        self.assertLess(len(page),8192)
        self.assertEqual(CursorJournal(store).state,next_state)

    def test_lost_head_ack_restart_recovers_without_advancing_again(self):
        store=MemoryStore();journal=CursorJournal(store)
        state={**journal.state,'incremental_sources':{'one':{'before':123,'url':'https://example.org'}}}
        write=store.json
        def lose(name,value,mutable=False):
            answer=write(name,value,mutable)
            if name==HEAD:raise TimeoutError('lost acknowledgement')
            return answer
        with patch.object(store,'json',side_effect=lose):
            with self.assertRaises(TimeoutError):journal.commit(state)
        restored=CursorJournal(store);head=restored.head
        self.assertEqual(restored.state,state);restored.commit(state)
        self.assertEqual(restored.head,head)

    def test_compaction_preserves_legacy_state_and_skips_compacted_pages(self):
        store=MemoryStore();journal=CursorJournal(store)
        state={**journal.state,'incremental_sources':{'one':{'before':123}}}
        journal.commit(state);head=journal.head;journal.compact(force=True)
        self.assertEqual(store.read_json(BASE)['incremental_sources'],state['incremental_sources'])
        self.assertEqual(store.read_json(BASE)[CHECKPOINT],head)
        read=store.read_bytes
        def no_old(name,limit):
            if name.startswith('cursors-'):raise AssertionError('compacted cursor page fetched')
            return read(name,limit)
        with patch.object(store,'read_bytes',side_effect=no_old):self.assertEqual(CursorJournal(store).state,journal.state)

    def test_bad_page_never_publishes_head(self):
        store=MemoryStore();journal=CursorJournal(store);read=store.read_bytes
        with patch.object(store,'read_bytes',side_effect=lambda name,limit:b'{}' if name.startswith('cursors-') else read(name,limit)):
            with self.assertRaisesRegex(ValueError,'checksum'):journal.commit({**journal.state,'run_id':'fixture'})
        self.assertIsNone(store.read_json(HEAD))

    def test_external_legacy_writer_is_not_silently_overwritten(self):
        store=MemoryStore();journal=CursorJournal(store)
        store.json(BASE,{'schema':'changed','sources':{}},mutable=True)
        with self.assertRaisesRegex(ValueError,'writer_conflict'):journal.commit({**journal.state,'run_id':'fixture'})
