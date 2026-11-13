from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import worker  # Sets up the existing private module paths.
from planning import plan
from check_image import check


class PlanningTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 4, 15, 0, tzinfo=timezone.utc)
        self.platforms = ['telegram','rss','youtube','facebook','instagram','vk','ok','x','threads','tiktok','rutube']
        self.catalog = {'sources': [
            {'id': f'{platform}-{i}', 'url': f'https://example.org/{platform}/{i}',
             'platform': platform, 'enabled': True}
            for platform in self.platforms for i in range(8 if platform=='instagram' else 2)]}

    def test_small_batches_do_not_starve_platforms(self):
        state = {'incremental_polling': {}}
        seen = set()
        for i in range(3):
            current = self.now + timedelta(minutes=i)
            chosen = plan(self.catalog, state, set(self.platforms), current)
            self.assertLessEqual(len(chosen['sources']), 4)
            for source in chosen['sources']:
                seen.add(source['platform'])
                state['incremental_polling'][source['id']] = {
                    'url': source['url'], 'last_checked_at': current.isoformat(),
                    'collector_status': 'no_new_posts',
                    'next_check_at': (current+timedelta(minutes=20)).isoformat()}
        self.assertEqual(seen, set(self.platforms))

    def test_stored_deadlines_are_never_shortened(self):
        source = self.catalog['sources'][0]
        row = {'url':source['url'], 'last_checked_at':(self.now-timedelta(minutes=6)).isoformat(),
               'next_check_at':(self.now+timedelta(hours=1)).isoformat()}
        for status, expected in [('no_new_posts',0),('login_required',0),('http_429',0),('timeout',0)]:
            state={'incremental_polling':{source['id']:{**row,'collector_status':status}}}
            result=plan({'sources':[source]},state,{source['platform']},self.now)
            self.assertEqual(len(result['sources']),expected,status)
            self.assertEqual(state['incremental_polling'][source['id']]['next_check_at'],row['next_check_at'])

    def test_future_successes_target_five_minutes_but_keep_retry_after(self):
        from source_health import update, instant
        record={'platform':'vk','url':'https://example.org/vk','checked_at':self.now.isoformat(),
                'status':'no_new_posts','new_keys':[],'attempts':[],'seconds':1,'canonical_keys':[]}
        result=update({},record,target_seconds=300)
        self.assertEqual((instant(result['next_check_at'])-self.now).total_seconds(),300)
        retry=(self.now+timedelta(hours=2)).isoformat()
        result=update({}, {**record,'retry_after':retry},target_seconds=300)
        self.assertEqual(result['next_check_at'],retry)
        result=update({}, {**record,'status':'login_required'},target_seconds=300)
        self.assertGreaterEqual((instant(result['next_check_at'])-self.now).total_seconds(),86400)

    def test_disabled_registry_and_source_stay_disabled(self):
        catalog={'sources':[{**self.catalog['sources'][0],'enabled':False}, self.catalog['sources'][2]]}
        self.assertEqual(plan(catalog,{}, {'telegram'},self.now)['sources'],[])

    def test_durable_last_check_survives_scheduler_restart(self):
        source=self.catalog['sources'][0]
        state={'incremental_polling':{source['id']:{'url':source['url'],
            'last_checked_at':self.now.isoformat(),'collector_status':'ok',
            'next_check_at':(self.now+timedelta(minutes=5)).isoformat()}}}
        result=plan(self.catalog,state,set(self.platforms),self.now)
        self.assertNotIn('telegram',[s['platform'] for s in result['sources']])
        self.assertGreater(result['overdue_sources'],len(result['sources']))

    def test_actual_browser_assets_have_extraction_expressions(self):
        check(Path(__file__).resolve().parents[2])


if __name__=='__main__':unittest.main()
