import json
import unittest
from analytics_snapshot import AnalyticsIndex, normalize, POINTER
from test_publish import MemoryStore

class AnalyticsTests(unittest.TestCase):
    def setUp(self):
        self.item={'id':'test:1','platform':'rss','origin':'general','source_id':'test','text':'ISOLATED TEST',
                   'published_at':'2026-10-25T21:59:59Z','observed_at':'2026-10-26T00:00:00Z'}
        self.key='["general","","",""]'

    def test_normalization_preserves_unknowns_and_legacy_content(self):
        p=normalize(self.item,{'name':'Test publisher','category':'media','region':'Kyiv'})
        for key in ('id','source_id','source_name','platform','category','title','content','url','published_at','collected_at','engagement','keywords','language'):
            self.assertIn(key,p)
        self.assertEqual(p['content'],self.item['text']);self.assertIsNone(p['engagement']['views'])

    def test_insert_edit_replay_and_source_partition(self):
        index=AnalyticsIndex([self.item]);self.assertEqual(index.summaries()[self.key]['totalPosts'],1)
        index.update([self.item,self.item]);self.assertEqual(index.summaries()[self.key]['totalPosts'],1)
        edited={**self.item,'origin':'dsns','views':0,'likes':2,'comments_count':3}
        index.update([edited]);self.assertNotIn(self.key,index.summaries())
        s=index.summaries()['["dsns","","",""]']
        self.assertEqual(s['totalPosts'],1);self.assertEqual(s['totalViews'],0);self.assertEqual(s['totalInteractions'],5)
        self.assertEqual(s['postsByDay'],[{'date':'2026-10-25','posts':1}])
        self.assertEqual(index.summaries(),AnalyticsIndex([edited]).summaries())

    def test_unknown_metric_is_not_zero_and_sample_excluded(self):
        index=AnalyticsIndex([self.item,{**self.item,'id':'test:2','sample':True}])
        self.assertIsNone(index.summaries()[self.key]['totalViews'])
        self.assertEqual(index.summaries()[self.key]['totalPosts'],1)

    def test_partial_metrics_are_summed_with_coverage_and_survive_updates(self):
        rows=[self.item,{**self.item,'id':'test:2','engagement':{'views':100,'likes':5}},
              {**self.item,'id':'test:3','engagement':{'comments':2}}]
        index=AnalyticsIndex(rows)
        summary=index.summaries()[self.key]
        self.assertEqual(summary['totalViews'],100)
        self.assertEqual(summary['totalInteractions'],7)
        self.assertEqual(summary['totalComments'],2)
        self.assertEqual(summary['metricCoverage']['interactions'],{'knownPosts':2,'totalPosts':3,'completePosts':0})
        rows[1]={**rows[1],'engagement':{'views':110,'likes':0,'comments':0}}
        index.update([rows[1]])
        self.assertEqual(index.summaries(),AnalyticsIndex(rows).summaries())

    def test_failed_upload_keeps_previous_pointer_and_retries(self):
        store=MemoryStore();index=AnalyticsIndex([self.item]);base={'snapshot_sha256':'a'*64}
        self.assertTrue(index.publish(store,base,None,'2026-10-26T00:00:00Z'))
        pointer=store.read_json(POINTER)
        self.assertFalse(index.publish(store,base,None,'2026-10-26T00:00:00Z'))
        index.update([{**self.item,'id':'test:2'}]);head={'sha':'b'*64,'last':1}
        store.fail='analytics-'
        with self.assertRaises(RuntimeError):index.publish(store,base,head,'2026-10-26T00:01:00Z')
        self.assertEqual(store.read_json(POINTER),pointer)
        store.fail=None;self.assertTrue(index.publish(store,base,head,'2026-10-26T00:01:00Z'))
        self.assertEqual(store.read_json(POINTER)['live_sequence'],1)

    def test_registry_metadata_and_combined_filters(self):
        index=AnalyticsIndex([self.item], [{'id':'test','platform':'rss','category':'media','region':'Kyiv'}])
        self.assertEqual(index.summaries()['["general","rss","Kyiv","media"]']['totalPosts'],1)

    def test_incremental_projection_preserves_source_dimensions_and_reverts_edits(self):
        sources=[{'platform':'rss','id':'test','name':'Publisher','category':'media','region':'Kyiv'}]
        index=AnalyticsIndex([self.item],sources)
        key='["general","rss","Kyiv","media"]'
        self.assertEqual(index.summaries()[key]['totalPosts'],1)
        initial=index.summaries()
        self.assertEqual(index.update([self.item,self.item]),0)
        index.update([{**self.item,'region':'Odesa','views':12}])
        self.assertNotIn(key,index.summaries())
        index.update([self.item])
        self.assertEqual(index.summaries(),initial)

if __name__=='__main__':unittest.main()
