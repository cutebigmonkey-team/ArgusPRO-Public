"""Failure injection uses only the in-memory Drive fixture and disposable SQLite."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from test_publish import MemoryStore
from storage import pack
from publish import publish,load_snapshot,load_base_snapshot
from feed_journal import HEAD
from indexed_journal import IndexedFeedJournal


class IndexedJournalTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);(self.root/'media').mkdir()
        self.store=MemoryStore()
        self.source={'id':'feed','url':'https://example.com/rss','platform':'rss','origin':'general'}
        self.store.json('sources.json',{'sources':[self.source]})
        publish(self.store,self.package(0),measure=True)
        self.feed=IndexedFeedJournal(self.store);self.addCleanup(lambda:self.feed.close())

    def package(self,n,*,text=None,published=None):
        post={'canonical_key':'rss:item'+str(n),'platform':'rss','post_url':'https://example.com/'+str(n),
              'text':text or 'article '+str(n),'published_at':published,
              'observed_at':'2026-11-13T10:00:01Z','source_id':'feed','source_url':self.source['url'],'media':[],
              'public_comments':[{'id':'c1','text':'retained comment'}]}
        path=self.root/(str(n)+'.jsonl');path.write_text(json.dumps(post)+'\n')
        package=self.root/(str(n)+'.zip');pack(path,self.root/'media',package)
        self.record={'source_id':'feed','platform':'rss','url':self.source['url'],
            'canonical_keys':[post['canonical_key']],'status':'ok','new_keys':[],
            'checked_at':'2026-11-13T10:00:02Z','seconds':1,'attempts':[]}
        return package
    def report(self):return {'run_id':'test','updated_at':'2026-11-13T10:00:02Z','sources':[copy.deepcopy(self.record)],'incremental':True,'state_updates':{}}

    def test_normal_delivery_never_loads_archive_and_preserves_old_records(self):
        before=load_snapshot(self.store)['items'][0];base=self.store.read_json('catalog.json')
        package=self.package(1);report=self.report()
        with patch('publish.load_snapshot',side_effect=AssertionError('full archive load')):
            self.feed.deliver(package,report)
        self.assertEqual(report['new_to_archive'],1)
        self.assertEqual(self.store.read_json('catalog.json'),base)
        after=load_snapshot(self.store)
        self.assertEqual(len(after['items']),2)
        self.assertEqual(next(p for p in after['items'] if p['id']==before['id']),before)
        self.assertIsNone(next(p for p in after['items'] if p['id']=='rss:item1')['published_at'])
        self.assertEqual(self.feed.index.get('rss:item1')['id'],'rss:item1')

    def test_upload_then_lost_head_ack_retries_without_duplicate(self):
        package=self.package(1);report=self.report();original=self.store.json;lost=[False]
        def upload(name,value,mutable=False):
            answer=original(name,value,mutable)
            if name==HEAD and not lost[0]:lost[0]=True;raise TimeoutError('lost ack')
            return answer
        with patch.object(self.store,'json',side_effect=upload):
            with self.assertRaises(TimeoutError):self.feed.deliver(package,report)
        self.assertIsNone(self.store.read_json('collection-cursors.json'))
        self.feed.deliver(package,self.report())
        head=self.store.read_json(HEAD)
        self.feed.deliver(package,self.report())
        self.assertEqual(head,self.store.read_json(HEAD))
        self.assertEqual(len(load_snapshot(self.store)['items']),2)

    def test_insert_telemetry_uses_readback_not_old_upload_attempt(self):
        package=self.package(1);report=self.report()
        report['archive_attempt_at']='2020-01-01T00:00:00+00:00'
        self.feed.deliver(package,report)
        self.assertEqual(report['platform_insertions']['rss']['rss:item1'],report['archive_verified_at'])
        self.assertNotEqual(report['archive_verified_at'],report['archive_attempt_at'])

    def test_page_failure_never_advances_cursor(self):
        package=self.package(1);self.store.fail='live-'
        with self.assertRaises(RuntimeError):self.feed.deliver(package,self.report())
        self.assertIsNone(self.store.read_json(HEAD))
        self.assertIsNone(self.store.read_json('collection-cursors.json'))
        self.store.fail=None;self.feed.deliver(package,self.report())
        self.assertEqual(len(load_snapshot(self.store)['items']),2)

    def test_remote_checksum_failure_prevents_ack(self):
        package=self.package(1);original=self.store.read_bytes
        with patch.object(self.store,'read_bytes',side_effect=lambda name,limit:b'{}' if name.startswith('live-') else original(name,limit)):
            with self.assertRaises(ValueError):self.feed.deliver(package,self.report())
        self.assertIsNone(self.store.read_json(HEAD))

    def test_streamed_compaction_restarts_without_losing_metadata(self):
        self.feed.deliver(self.package(1),self.report())
        before=load_snapshot(self.store)
        self.assertTrue(self.feed.compact(force=True))
        after=load_snapshot(self.store)
        self.assertEqual({p['id']:p for p in after['items']},{p['id']:p for p in before['items']})
        self.assertEqual({k:v for k,v in after.items() if k!='items'},{k:v for k,v in before.items() if k!='items'})
        self.feed.close();self.feed=IndexedFeedJournal(self.store)
        self.assertEqual(self.feed.index.count(),2)
        self.assertEqual(list(self.feed.snapshot['items']),load_base_snapshot(self.store)['items'])

    def test_large_normalized_result_splits_before_visibility(self):
        package=self.package(1,text='я'*4000)
        with patch('feed_journal.MAX_JSON',1000):
            with self.assertRaisesRegex(ValueError,'feed_item_too_large'):self.feed.deliver(package,self.report())
        self.assertIsNone(self.store.read_json(HEAD))
        self.assertIsNone(self.store.read_json('collection-cursors.json'))

    def test_bootstrap_fetches_each_verified_page_once(self):
        from collections import Counter
        for n in range(1,4):self.feed.deliver(self.package(n),self.report())
        read=self.store.read_bytes;calls=Counter()
        def count(name,limit):
            if name.startswith('live-'):calls[name]+=1
            return read(name,limit)
        with patch.object(self.store,'read_bytes',side_effect=count):
            restored=IndexedFeedJournal(self.store)
            try:self.assertEqual(restored.index.count(),4)
            finally:restored.close()
        self.assertEqual(len(calls),3);self.assertTrue(all(n==1 for n in calls.values()))

    def test_ambiguous_head_retry_never_reloads_full_archive(self):
        package=self.package(1);report=self.report();write=self.store.json;lost=[False]
        def lose(name,value,mutable=False):
            answer=write(name,value,mutable)
            if name==HEAD and not lost[0]:lost[0]=True;raise TimeoutError('lost acknowledgement')
            return answer
        with patch.object(self.store,'json',side_effect=lose):
            with self.assertRaises(TimeoutError):self.feed.deliver(package,report)
        with patch('indexed_journal.load_base_snapshot',side_effect=AssertionError('unnecessary archive reload')):
            self.feed.deliver(package,report)
        self.assertEqual(self.feed.index.count(),2)
        self.assertTrue(self.feed.reconciled)  # recovered edits/posts require a bounded comments resweep
        self.assertEqual(report['new_to_archive'],1)

    def test_failed_local_index_after_remote_commit_reconciles_tail(self):
        package=self.package(1);report=self.report()
        with patch.object(self.feed.index,'put',side_effect=OSError('local transaction interrupted')):
            with self.assertRaises(OSError):self.feed.deliver(package,report)
        self.assertEqual(self.feed.index.count(),1)
        with patch('indexed_journal.load_base_snapshot',side_effect=AssertionError('unnecessary archive reload')):
            self.feed.deliver(package,report)
        self.assertEqual(self.feed.index.count(),2)
        self.assertEqual(report['new_to_archive'],1)

    def test_corrupted_recovery_page_is_never_skipped(self):
        package=self.package(1);report=self.report();write=self.store.json
        def lose(name,value,mutable=False):
            answer=write(name,value,mutable)
            if name==HEAD:raise TimeoutError('lost acknowledgement')
            return answer
        with patch.object(self.store,'json',side_effect=lose):
            with self.assertRaises(TimeoutError):self.feed.deliver(package,report)
        head=self.store.read_json(HEAD);self.store.files['live-'+head['sha']+'.json']=b'corrupt'
        with self.assertRaisesRegex(ValueError,'checksum'):self.feed.deliver(package,report)
        self.assertEqual(self.feed.index.count(),1)
