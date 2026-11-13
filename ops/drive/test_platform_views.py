import copy
import unittest
from test_publish import MemoryStore
from platform_views import write_views,merge_catalog,monitor_stages

class ViewTests(unittest.TestCase):
    def test_recovery_keeps_the_newer_platform_health_report(self):
        store=MemoryStore()
        store.json('collection-report.json',{'run_id':'20'})
        store.put('status.txt',b'Newer status',mutable=True)
        old={'run_id':'10','sources':[],'platforms':[]}
        write_views({'rss':store},[],old)
        self.assertEqual(store.read_json('collection-report.json'),{'run_id':'20'})
        self.assertEqual(store.files['status.txt'],b'Newer status')

    def test_older_full_plan_retains_profiles_registered_while_running(self):
        feed={'id':'feed','url':'https://example.com','platform':'rss'}
        profile={'id':'media','name':'Media','origin':'general','url':'https://x.com/media','platform':'x',
                 'references':[{'kind':'verified_public_profile','url':'https://example.org'}]}
        old={'sources':[feed]}
        self.assertEqual(merge_catalog({'sources':[feed,profile]},old)['sources'],[feed,profile])

    def test_focused_run_preserves_other_catalog_views_and_monitor_stages(self):
        previous={'sources':[{'id':'tg-old','platform':'telegram'},{'id':'rss-keep','platform':'rss'}]}
        current={'platform_scope':['telegram','max'],'sources':[{'id':'tg-new','platform':'telegram'}]}
        self.assertEqual([s['id'] for s in merge_catalog(previous,current)['sources']],['rss-keep','tg-new'])
        report={'platform_scope':['telegram','max'],'platforms':[{'platform':'telegram','status':'collected','posts':2,'results':{'ok':1}}],
                'sources':[],'updated_at':'2026-10-30T00:00:00Z','run_id':'456'}
        stores={p:MemoryStore() for p in ('telegram','rss')}
        stores['rss'].put('status.txt',b'Existing RSS result',mutable=True)
        write_views(stores,[],report)
        self.assertEqual(stores['rss'].files,{'status.txt':b'Existing RSS result'})
        old={'stages':[{'tool':'rss','status':'collected','observed_at':'previous'},{'tool':'telegram','status':'old'}]}
        stages=monitor_stages(old,report)
        self.assertEqual(stages[0],old['stages'][0])
        self.assertEqual(stages[1]['status'],'collected')

    def test_replay_reuses_views_and_keeps_platforms_separate(self):
        stores={p:MemoryStore() for p in ('telegram','vk')}
        posts=[{'id':'telegram:a/1','platform':'telegram','url':'https://t.me/a/1','text':'Real post',
                'observed_at':'2026-01-01T00:00:00Z','published_at':None,'sample':False},
               {'id':'sample:1','platform':'vk','sample':True}]
        report={'platforms':[{'platform':'telegram','status':'collected'},{'platform':'vk','status':'no_sources'}],
                'sources':[],'updated_at':'2026-01-01T00:00:00Z','run_id':'123'}
        self.assertEqual(write_views(stores,posts,report),{'telegram':1,'vk':0})
        before=copy.deepcopy(stores['telegram'].files)
        write_views(stores,posts,report)
        self.assertEqual(before,stores['telegram'].files)
        self.assertIn(b'Real post',stores['telegram'].files['posts.txt'])
        self.assertNotIn(b'Real post',stores['vk'].files['posts.txt'])

if __name__=='__main__': unittest.main()
