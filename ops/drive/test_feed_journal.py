import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from test_publish import MemoryStore
from storage import pack
from publish import publish,load_snapshot,load_base_snapshot
from feed_journal import FeedJournal,HEAD,CHECKPOINT


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);(self.root/'media').mkdir()
        self.store=MemoryStore()
        self.source={'id':'feed','url':'https://example.com/rss','platform':'rss','origin':'general'}
        self.store.json('sources.json',{'sources':[self.source]})
        publish(self.store,self.package(0),measure=True)
        self.journal=FeedJournal(self.store)

    def package(self,n):
        post={'canonical_key':'rss:https://example.com/'+str(n),'platform':'rss',
              'post_url':'https://example.com/'+str(n),'text':'article '+str(n),
              'published_at':'2026-11-05T10:00:00Z','observed_at':'2026-11-05T10:00:01Z',
              'source_id':'feed','source_url':self.source['url'],'media':[]}
        path=self.root/(str(n)+'.jsonl');path.write_text(json.dumps(post)+'\n')
        package=self.root/(str(n)+'.zip');pack(path,self.root/'media',package)
        self.record={'source_id':'feed','platform':'rss','url':self.source['url'],
                     'canonical_keys':[post['canonical_key']],'status':'ok','checked_at':'2026-11-05T10:00:02Z','attempts':[]}
        return package

    def report(self):return {'run_id':'test','updated_at':'2026-11-05T10:00:02Z','sources':[copy.deepcopy(self.record)],'incremental':True,'state_updates':{}}

    def test_verified_delta_keeps_base_and_restarts_without_duplicates(self):
        base=self.store.read_json('catalog.json');p=self.package(1);r=self.report()
        self.journal.deliver(p,r)
        self.assertEqual(r['new_to_archive'],1)
        self.assertEqual(self.store.read_json('catalog.json'),base)
        self.assertEqual(len(load_base_snapshot(self.store)['items']),1)
        self.assertEqual(len(load_snapshot(self.store)['items']),2)
        head=self.store.read_json(HEAD)
        recovered=FeedJournal(self.store);recovered.deliver(p,self.report())
        self.assertEqual(self.store.read_json(HEAD),head)
        self.assertEqual(len(recovered.snapshot['items']),2)

    def test_lost_head_ack_recovers_and_cursor_never_precedes_readback(self):
        p=self.package(1);r=self.report();original=self.store.json
        def lose(name,value,mutable=False):
            answer=original(name,value,mutable)
            if name==HEAD:raise OSError('lost acknowledgement')
            return answer
        with patch.object(self.store,'json',side_effect=lose):
            with self.assertRaises(OSError):self.journal.deliver(p,r)
        self.assertIsNone(self.store.read_json('collection-cursors.json'))
        recovered=FeedJournal(self.store);recovered.deliver(p,self.report())
        self.assertEqual(len(recovered.snapshot['items']),2)
        self.assertIsNotNone(self.store.read_json('collection-cursors.json'))

    def test_corrupt_uploaded_page_prevents_head_and_cursor(self):
        p=self.package(1);original=self.store.read_bytes
        def corrupt(name,limit):return b'{}' if name.startswith('live-') else original(name,limit)
        with patch.object(self.store,'read_bytes',side_effect=corrupt):
            with self.assertRaisesRegex(ValueError,'checksum'):self.journal.deliver(p,self.report())
        self.assertIsNone(self.store.read_json(HEAD))
        self.assertIsNone(self.store.read_json('collection-cursors.json'))

    def test_compaction_retains_chain_and_checkpoint_skips_old_pages(self):
        self.journal.deliver(self.package(1),self.report())
        head=self.journal.head
        self.assertTrue(self.journal.compact(force=True))
        self.assertEqual(load_base_snapshot(self.store)[CHECKPOINT],head)
        self.assertEqual(self.store.read_json(HEAD),head)
        self.journal.deliver(self.package(2),self.report())
        original=self.store.read_bytes
        def forbid_old(name,limit):
            if name=='live-'+head['sha']+'.json':raise AssertionError('compacted page reread')
            return original(name,limit)
        with patch.object(self.store,'read_bytes',side_effect=forbid_old):
            self.assertEqual(len(FeedJournal(self.store).snapshot['items']),3)

    def test_store_failure_retains_last_confirmed_feed(self):
        p=self.package(1);self.store.fail='live-'
        with self.assertRaises(RuntimeError):self.journal.deliver(p,self.report())
        self.assertEqual(len(self.journal.snapshot['items']),1)
        self.assertIsNone(self.store.read_json('collection-cursors.json'))

    def test_external_head_change_refuses_writer(self):
        self.store.json(HEAD,{'changed':True},mutable=True)
        with self.assertRaisesRegex(ValueError,'conflict'):self.journal.deliver(self.package(1),self.report())

    def test_monitor_reuses_verified_cursors_without_removing_readback(self):
        package=self.package(1);report=self.report()
        self.journal.catalog={'sources':[self.source]}
        with patch.object(self.store,'read_json',wraps=self.store.read_json) as reads:
            self.journal.deliver(package,report)
        cursor_reads=[call for call in reads.call_args_list if call.args[0]=='collection-cursors.json']
        self.assertEqual(len(cursor_reads),2)  # original state + independent write readback
        self.assertEqual(self.journal.verified_state,self.store.read_json('collection-cursors.json'))
        self.assertEqual(self.journal.monitor,self.store.read_json('monitor.json'))
        self.assertEqual(self.journal.monitor['coverage']['registered'],1)


    def large_batch(self):
        posts=[]
        for n in range(1,8):
            posts.append({'canonical_key':'rss:https://example.com/'+str(n),'platform':'rss',
                'post_url':'https://example.com/'+str(n),'text':str(n)+' Публікація 🔥 '*45,
                'published_at':'2026-11-05T10:00:00Z','observed_at':'2026-11-05T10:00:01Z',
                'source_id':'feed','source_url':self.source['url'],'media':[]})
        source=self.root/'large.jsonl';source.write_text(''.join(json.dumps(p)+'\n' for p in posts))
        package=self.root/'large.zip';pack(source,self.root/'media',package)
        self.record={'source_id':'feed','platform':'rss','url':self.source['url'],
            'canonical_keys':[p['canonical_key'] for p in posts],'status':'ok',
            'checked_at':'2026-11-05T10:00:02Z','attempts':[]}
        return package

    def test_size_bounded_pages_atomic_head_restart_and_replay(self):
        package=self.large_batch()
        with patch('feed_journal.MAX_JSON',6000):
            self.journal.deliver(package,self.report())
            pages=[raw for name,raw in self.store.files.items() if name.startswith('live-') and name!=HEAD]
            self.assertGreater(len(pages),1)
            self.assertTrue(all(len(raw)<=6000 for raw in pages))
            head=self.store.read_json(HEAD)
            recovered=FeedJournal(self.store)
            self.assertEqual(len(recovered.snapshot['items']),8)
            report=self.report();recovered.deliver(package,report)
            self.assertEqual(report['new_to_archive'],7)
            self.assertEqual(self.store.read_json(HEAD),head)
            self.assertEqual(len({p['id'] for p in recovered.snapshot['items']}),8)

    def test_failed_later_page_leaves_head_and_checkpoint_then_retries(self):
        package=self.large_batch();put=self.store.put;writes=0
        def fail(name,data,mutable=False):
            nonlocal writes
            if name.startswith('live-') and name!=HEAD:
                writes+=1
                if writes==2:raise OSError('interrupted page upload')
            return put(name,data,mutable)
        with patch('feed_journal.MAX_JSON',6000):
            with patch.object(self.store,'put',side_effect=fail):
                with self.assertRaises(OSError):self.journal.deliver(package,self.report())
            self.assertIsNone(self.store.read_json(HEAD))
            self.assertIsNone(self.store.read_json('collection-cursors.json'))
            self.assertEqual(len(FeedJournal(self.store).snapshot['items']),1)
            self.journal.deliver(package,self.report())
            self.assertEqual(len(FeedJournal(self.store).snapshot['items']),8)

    def test_single_oversized_item_never_advances_checkpoint(self):
        package=self.large_batch()
        with patch('feed_journal.MAX_JSON',1000):
            with self.assertRaisesRegex(ValueError,'feed_item_too_large'):
                self.journal.deliver(package,self.report())
        self.assertIsNone(self.store.read_json(HEAD))
        self.assertIsNone(self.store.read_json('collection-cursors.json'))
