import copy
import unittest
from test_publish import MemoryStore
from social_discovery import publish_discovery,load_state,render
from social_links import from_posts


class DiscoveryPublicationTests(unittest.TestCase):
    def test_registry_write_retries_after_snapshot_and_survives_full_or_scoped_publication(self):
        from platform_views import merge_catalog
        from source_discovery import candidate,registered_discoveries
        store=MemoryStore()
        store.json('sources.json',{'sources':[]})
        result=self.result()
        row=candidate('rss','https://press.test/feed','publisher_feed','https://press.test/','2026-11-03T00:00:00Z')
        row.update(status='confirmed',readable_publications=1,sample_permalink='https://press.test/a')
        result['source_candidates']=[row]
        store.fail='sources.json'
        with self.assertRaises(RuntimeError):
            publish_discovery(store,result)
        self.assertEqual(len(load_state(store)['source_candidates']),1)
        store.fail=None
        publish_discovery(store,result)
        catalog=store.read_json('sources.json')
        self.assertEqual(len(registered_discoveries(catalog)),1)
        files=copy.deepcopy(store.files)
        publish_discovery(store,result)
        self.assertEqual(store.files,files)
        for current in ({'sources':[]},{'sources':[],'platform_scope':['rss']}):
            self.assertEqual(merge_catalog(catalog,current)['sources'],catalog['sources'])

    def result(self):
        row=from_posts([{'id':'telegram:a/1','platform':'telegram','url':'https://t.me/a/1',
            'observed_at':'2026-10-30T14:00:00+00:00','text':'https://instagram.com/p/ABCDE/'}])[0]
        return {'records':[row],'baselines':{},'report':{'run_id':'1'}}

    def test_index_replay_and_post_archive_unchanged(self):
        store=MemoryStore(); store.json('catalog.json',{'sentinel':'existing posts'})
        state=publish_discovery(store,self.result()); files=copy.deepcopy(store.files)
        self.assertEqual(publish_discovery(store,self.result()),state)
        self.assertEqual(store.files,files)
        self.assertEqual(store.read_json('catalog.json'),{'sentinel':'existing posts'})
        self.assertIn('Текст оригінальних дописів не отримано',render(state['records']).decode())

    def test_blocked_run_preserves_previous_good_baseline(self):
        store=MemoryStore(); result=self.result()
        result['baselines']={'https://example.com/':{'observed_at':'2026-10-30T14:00:00+00:00','text':'good'}}
        publish_discovery(store,result)
        following=self.result(); following['report']={'status':'login_required'}
        state=publish_discovery(store,following)
        self.assertEqual(state['baselines'],result['baselines'])

    def test_failed_state_upload_does_not_advance_pointer(self):
        store=MemoryStore(); publish_discovery(store,self.result()); pointer=store.read_json('discovery-catalog.json')
        store.fail='discovery-state-'; next_result=self.result(); next_result['report']['run_id']='2'
        with self.assertRaises(RuntimeError): publish_discovery(store,next_result)
        self.assertEqual(store.read_json('discovery-catalog.json'),pointer)

    def test_cannot_claim_discovery_as_full_post(self):
        result=self.result(); result['records'][0]['text']='invented body'
        with self.assertRaises(ValueError): publish_discovery(MemoryStore(),result)


if __name__=='__main__': unittest.main()
