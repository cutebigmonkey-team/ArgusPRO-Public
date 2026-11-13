import json
import unittest
from datetime import datetime,timezone,timedelta
from audit_collection import audit

class AuditTests(unittest.TestCase):
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
