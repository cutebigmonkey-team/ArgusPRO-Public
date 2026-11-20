import json
import sqlite3
import unittest
from datetime import datetime,timezone,timedelta
from audit_collection import audit
from check_metrics import initialize, record, summarize

class AuditTests(unittest.TestCase):
    def test_read_only_check_windows_preserve_the_ledger(self):
        db=sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        db.row_factory=sqlite3.Row
        initialize(db.execute,1000)
        for token,status in [('first','ok'),('second','timeout')]:
            record(db.execute,{'source_id':'a','platform':'rss','status':status},token,1200)
        db.commit()
        db.execute('PRAGMA query_only=ON')
        window=summarize(db.execute,1300,prune=False)['5m']
        self.assertTrue(window['complete_window'])
        observed=window['platforms']['rss']['observed']
        self.assertEqual((observed['attempted'],observed['successful_check_rate']),(2,.5))
        self.assertEqual(db.execute('SELECT COUNT(*) FROM runtime_check_events').fetchone()[0],2)

    def test_sql_schedule_deadline_takes_precedence_over_archived_health(self):
        now=datetime(2026,11,11,tzinfo=timezone.utc)
        source={'id':'a','url':'https://example.org/rss','platform':'rss'}
        health={'url':source['url'],'next_check_at':(now-timedelta(seconds=60)).isoformat(),
                'actual_interval_seconds':298}
        row={'id':'a','health':json.dumps(health),'enabled':1,'failures':0,
             'lease_until':0,'next_check_at':now.timestamp()+60}
        platform=audit({'sources':[source]}, {},{},[row],[],now)['platforms'][0]
        self.assertEqual(platform['overdue'],0)
        self.assertEqual(platform['check_interval_seconds']['p95'],298)
        self.assertEqual(platform['schedule_lag_seconds']['samples'],0)

    def test_stale_archive_is_unknown_while_database_health_is_current(self):
        now=datetime(2026,11,11,tzinfo=timezone.utc)
        source={'id':'a','url':'https://example.org/rss','platform':'rss'}
        health={'url':source['url'],'last_checked_at':now.isoformat(),
                'collector_status':'timeout','recent_checks':[{'at':now.isoformat(),'status':'timeout'}]}
        rows=[{'id':'a','health':json.dumps(health),'enabled':1,'failures':1,'lease_until':0,'next_check_at':0}]
        receipt={'coverage':{'at':(now-timedelta(minutes=10)).isoformat(),
                            'platforms':{'rss':{'new_posts_1h':42}}}}
        result=audit({'sources':[source]}, {},receipt,rows,[],now)
        p=result['platforms'][0]
        self.assertEqual(p['checked_sources_5m'],1)
        self.assertEqual(p['overdue'],1)
        self.assertIsNone(p['new_archived_posts']['1h'])
        self.assertIsNone(result['sources'][0]['latest_public_post_archived'])

    def test_missing_runtime_source_is_not_reported_as_enabled_or_checked(self):
        now=datetime(2026,11,11,tzinfo=timezone.utc)
        result=audit({'sources':[{'id':'a','url':'https://example.org','platform':'rss'}]}, {},{},[],[],now)
        self.assertEqual(result['platforms'][0]['configured_sources'],1)
        self.assertIsNone(result['platforms'][0]['runtime_enabled_sources'])
        self.assertEqual(result['platforms'][0]['sources_without_runtime_row'],1)
        self.assertIsNone(result['sources'][0]['runtime_enabled'])
