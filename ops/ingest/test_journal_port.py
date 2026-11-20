"""Real package wire format with a deterministic remote-store fixture, not Drive evidence."""
import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from envelope import canonical
from journal_port import ExistingJournalPort
from trusted_consumer import verify_readback
from test_phase2 import parts


from indexed_journal import IndexedFeedJournal
from test_publish import MemoryStore


class Store(MemoryStore):
    def __init__(self):
        super().__init__();self.data=self.files;self.reads=0;self.corrupt=False
    def put(self,name,body,mutable=False):
        if self.corrupt and name.startswith('ingest-'):body=b'corrupt'
        return super().put(name,body,mutable)
    def read_bytes(self,name,limit):
        self.reads+=1
        return super().read_bytes(name,limit)


class Journal(IndexedFeedJournal):
    def __init__(self):
        super().__init__(Store());self.deliveries=0;self.reports=[]
    @property
    def posts(self):return {row['id']:row for row in self.index.rows()}
    def deliver(self,package,report):
        result=super().deliver(package,report)
        self.deliveries+=1;self.reports.append(copy.deepcopy(report))
        return result


class ExistingJournalBridgeTests(unittest.TestCase):
    def journal(self):
        journal=Journal();self.addCleanup(journal.close);return journal

    def test_existing_writer_package_readback_and_retry_are_idempotent(self):
        packet=parts(2)[0];body=canonical(packet);bid=f"{packet['group_id']}:0"
        journal=self.journal();port=ExistingJournalPort(journal,lambda:True,enabled=True)
        port.import_candidate(body,batch_id=bid)
        self.assertEqual(verify_readback(body,port.readback(bid)),hashlib.sha256(body).hexdigest())
        port.import_candidate(body,batch_id=bid)
        self.assertEqual(journal.deliveries,1);self.assertEqual(len(journal.posts),2)
        self.assertEqual(journal.reports[0]['state_updates'],{})
        self.assertTrue(journal.reports[0]['permalink_backfill'])
        self.assertGreaterEqual(journal.store.reads,4)

    def test_200_style_put_result_does_not_replace_content_verification(self):
        packet=parts(1)[0];body=canonical(packet);bid=f"{packet['group_id']}:0"
        journal=self.journal();journal.store.corrupt=True
        port=ExistingJournalPort(journal,lambda:True,enabled=True)
        with self.assertRaises(ValueError):port.import_candidate(body,batch_id=bid)

    def test_default_disabled_and_lost_writer_never_write(self):
        packet=parts(1)[0];body=canonical(packet);bid=f"{packet['group_id']}:0"
        for port in (ExistingJournalPort(self.journal(),lambda:True),ExistingJournalPort(self.journal(),lambda:False,enabled=True)):
            with self.assertRaises(ValueError):port.import_candidate(body,batch_id=bid)
            self.assertEqual(port.journal.deliveries,0)
            self.assertEqual(port.store.data,{})

    def test_zero_post_checkpoint_still_requires_durable_input_receipt(self):
        packet=parts(0)[0];body=canonical(packet);bid=f"{packet['group_id']}:0"
        journal=self.journal();port=ExistingJournalPort(journal,lambda:True,enabled=True)
        with self.assertRaises(ValueError):port.readback(bid)
        port.import_candidate(body,batch_id=bid)
        self.assertEqual(verify_readback(body,port.readback(bid)),hashlib.sha256(body).hexdigest())
        self.assertEqual(journal.deliveries,0)
        self.assertEqual(len(journal.store.data),1)

    def test_failed_journal_commit_never_creates_an_ingestion_receipt(self):
        journal=self.journal();journal.deliver=Mock(side_effect=ValueError('fixture failure'))
        packet=parts(1)[0];port=ExistingJournalPort(journal,lambda:True,enabled=True)
        with self.assertRaises(ValueError):port.import_candidate(canonical(packet),batch_id=f"{packet['group_id']}:0")
        self.assertEqual(journal.store.data,{})


class RealIndexedJournalContractTests(unittest.TestCase):
    def test_existing_indexed_journal_import_preserves_cursors_and_old_posts(self):
        from indexed_journal import IndexedFeedJournal
        from test_publish import MemoryStore
        from publish import load_snapshot
        from storage import pack
        from test_phase2 import posts, SOURCE, WHEN
        store=MemoryStore()
        store.json('sources.json',{'sources':[{'id':SOURCE['source_id'],'platform':'rss',
                   'url':'https://example.org/feed','origin':'general'}]})
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);media=root/'media';media.mkdir()
            original=posts(1)[0];original['canonical_key']='rss:preserved';original['post_url']='https://example.org/old-preserved';original['text']='Old post with comment'
            original['public_comments']=[{'id':'fixture-comment','text':'Preserved comment'}]
            (root/'old.jsonl').write_bytes(canonical(original)+b'\n')
            pack(root/'old.jsonl',media,root/'old.zip',created_at=WHEN.isoformat())
            from publish import publish
            publish(store,root/'old.zip',measure=True)
        before=copy.deepcopy(load_snapshot(store)['items'])
        journal=IndexedFeedJournal(store)
        try:
            cursor_before=(store.read_json('collection-cursors.json'),store.read_json('live-cursors.json'))
            port=ExistingJournalPort(journal,lambda:True,enabled=True)
            for packet in parts(648):
                body=canonical(packet);bid=f"{packet['group_id']}:{packet['index']}"
                port.import_candidate(body,batch_id=bid)
                self.assertEqual(verify_readback(body,port.readback(bid)),hashlib.sha256(body).hexdigest())
                port.import_candidate(body,batch_id=bid)
            after=load_snapshot(store)['items']
            self.assertEqual(len(after),649)
            self.assertEqual(next(p for p in after if p['id']==before[0]['id']),before[0])
            self.assertEqual((store.read_json('collection-cursors.json'),store.read_json('live-cursors.json')),cursor_before)
        finally:journal.close()
