import json
import unittest
from datetime import datetime,timezone,timedelta
from audit_collection import operational_report,NOT_MEASURABLE

class OperationalReportTests(unittest.TestCase):
    def setUp(self):
        self.now=datetime(2026,11,13,tzinfo=timezone.utc)
        self.source={'id':'ig','platform':'instagram','url':'https://www.instagram.com/example'}
        self.catalog={'sources':[self.source]}

    def test_unknown_metrics_and_provider_identity_are_not_zero_or_claimed_verified(self):
        r=operational_report(self.catalog,[],[],{'runtime':{'deployment_sha':'a'*40}},self.now)
        p=r['platforms'][0]
        for key in ['scheduled_sources','last_attempt','last_successful_extraction','extracted_post_count','accepted_post_count','rejected_post_count','archived_post_count','frontend_visible_post_count']:
            self.assertEqual(p[key],NOT_MEASURABLE)
        self.assertEqual(r['current_deployed_commit'],NOT_MEASURABLE)
        self.assertEqual(r['monitor_reported_commit'],'a'*40)
        self.assertEqual(r['pending_package_count'],NOT_MEASURABLE)

    def test_latest_acknowledged_counts_and_both_pending_ages(self):
        at=(self.now-timedelta(seconds=40)).isoformat()
        health={'url':self.source['url'],'last_attempt_at':at,'last_extraction_at':at,'pipeline':{'collector_rows':3,'unique_keys':2,'rejected_records':1},'delivery':{'saved_ids':2},'collector_status':'timeout'}
        rows=[{'id':'ig','enabled':1,'health':json.dumps(health)}]
        r=operational_report(self.catalog,rows,[{'source_id':'ig','created_at':self.now.timestamp()-60}],{},self.now,[{'staged_at':at}])
        p=r['platforms'][0]
        self.assertEqual([p[k] for k in ['scheduled_sources','extracted_post_count','accepted_post_count','rejected_post_count','archived_post_count']],[1,3,2,1,2])
        self.assertEqual(p['oldest_pending_result_seconds'],60)
        self.assertEqual(r['oldest_pending_package_seconds'],40)
        self.assertEqual(p['instagram_gateway_candidate_sources'],1)
        self.assertEqual(p['error_reasons'],{'timeout':1})

    def test_wrong_source_unknown_age_and_secret_values_not_exported(self):
        health={'url':'https://elsewhere.test/?token=SECRET','last_attempt_at':'SECRET','collector_status':'https://secret.test/?token=SECRET'}
        r=operational_report(self.catalog,[{'id':'ig','enabled':1,'health':json.dumps(health)}],[],{'runtime':{'deployment_sha':'SECRET'}},self.now,[{'staged_at':None}])
        self.assertNotIn('SECRET',json.dumps(r));self.assertNotIn('instagram.com',json.dumps(r))
        self.assertEqual(r['oldest_pending_package_seconds'],NOT_MEASURABLE)


class PrivateReportTests(unittest.TestCase):
    def test_file_is_private_and_existing_evidence_cannot_be_overwritten(self):
        import os
        import tempfile
        from pathlib import Path
        from audit_collection import write_private_report
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'private.json'
            write_private_report(path,{'mode':'read_only'})
            self.assertEqual(os.stat(path).st_mode & 0o777,0o600)
            with self.assertRaises(FileExistsError):write_private_report(path,{'mode':'changed'})
            self.assertEqual(json.loads(path.read_text()),{'mode':'read_only'})


class StagedAttemptTests(unittest.TestCase):
    def test_staged_attempt_is_visible_before_archive_ack(self):
        now=datetime(2026,11,13,tzinfo=timezone.utc)
        source={'id':'s','platform':'rss','url':'https://example.test/rss'}
        r=operational_report({'sources':[source]},[{'id':'s','enabled':1,'health':'{}','last_attempt_at':now.timestamp()-10}],[],{},now)
        self.assertEqual(r['platforms'][0]['last_attempt'],(now-timedelta(seconds=10)).isoformat())
        self.assertEqual(r['platforms'][0]['extracted_post_count'],NOT_MEASURABLE)
