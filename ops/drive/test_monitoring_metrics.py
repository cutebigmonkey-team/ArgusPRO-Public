from datetime import datetime,timezone,timedelta
import unittest
from monitoring_metrics import summarize


class CoverageTests(unittest.TestCase):
    def test_attempts_not_successes_and_inaccessible_remain_in_total(self):
        now=datetime(2026,11,5,tzinfo=timezone.utc);stamp=now.isoformat()
        catalog={'sources':[{'id':str(i),'url':f'https://example.com/{i}','platform':'rss'} for i in range(4)]}
        state={'incremental_polling':{str(i):{'url':f'https://example.com/{i}',
            'collector_status':status,'recent_checks':[{'at':stamp,'status':status}],
            'last_success_at':stamp if status=='ok' else None}
            for i,status in enumerate(('ok','timeout','robots_denied','no_new_posts'))}}
        result=summarize(catalog,state,[],now)
        self.assertEqual(result['attempted_sources_5m'],4)
        self.assertEqual(result['successful_sources_5m'],2)
        self.assertEqual(result['registered'],4)
        self.assertEqual(result['eligible'],3)
        self.assertEqual(result['coverage_5m_all_percent'],50)
        self.assertIsNone(result['delivery_seconds_1h']['mean'])
        platform=result['platforms']['rss']
        self.assertEqual(platform['checked_sources_1h'],4)
        self.assertEqual(platform['successful_sources_1h'],2)
        self.assertEqual(platform['collection_state'],'checked_no_new_posts')
        self.assertEqual(platform['latest_statuses']['timeout'],1)
        self.assertEqual(platform['coverage_5m_percent'],50)

    def test_stale_or_future_receipts_do_not_count(self):
        now=datetime(2026,11,5,tzinfo=timezone.utc)
        catalog={'sources':[{'id':'1','url':'https://example.com','platform':'rss'}]}
        for age in (-1,301):
            state={'incremental_polling':{'1':{'url':'https://example.com','last_success_at':(now-timedelta(seconds=age)).isoformat()}}}
            self.assertEqual(summarize(catalog,state,[],now)['successful_sources_5m'],0)

    def test_platform_windows_distinguish_archive_time_from_publication_and_edits(self):
        now=datetime(2026,11,5,tzinfo=timezone.utc)
        items=[{'platform':'rss','first_archived_at':(now-timedelta(seconds=age)).isoformat(),
                'published_at':(now-timedelta(days=2)).isoformat()} for age in (60,600,1800,4000,-1)]
        result=summarize({'sources':[]},{},items,now)
        p=result['platforms']['rss']
        self.assertEqual([p['new_posts_'+w] for w in ('5m','15m','1h')],[1,2,3])
        self.assertEqual(result['new_posts']['15m'],2)
        self.assertEqual(p['last_archive_insert_at'],items[0]['first_archived_at'])
        self.assertEqual(p['collector_availability'],'unknown')

    def test_disabled_sources_do_not_inflate_operational_coverage(self):
        now=datetime(2026,11,5,tzinfo=timezone.utc);stamp=now.isoformat()
        catalog={'sources':[{'id':str(i),'url':f'https://example.com/{i}',
                            'platform':'rss','enabled':i!=2} for i in range(3)]}
        state={'incremental_polling':{str(i):{'url':f'https://example.com/{i}',
            'last_checked_at':stamp,'collector_status':status,'remaining_ranges':2,
            'recent_checks':[{'at':stamp,'status':status}]}
            for i,status in enumerate(('ok','timeout','ok'))}}
        p=summarize(catalog,state,[],now)['platforms']['rss']
        self.assertEqual(p['active'],2)
        self.assertEqual(p['checked_sources_5m'],2)
        self.assertEqual(p['failed_sources_5m'],1)
        self.assertEqual(p['attempts_1h'],2)
        self.assertEqual(p['successful_sources_1h'],1)
        self.assertEqual(p['overdue'],2)
        self.assertEqual(p['pending_history_sources'],2)
        self.assertEqual(p['error_categories_1h'],{'timeout':1})
