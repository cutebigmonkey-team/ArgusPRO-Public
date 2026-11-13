"""Synthetic partial-publication trace through existing durable delivery."""
import json
import time
import unittest
import test_continuous as fixtures
from cloud_social import library_rows
from publish import load_snapshot
from schedule_store import iso

class PartialSocialDeliveryTests(unittest.TestCase):
    def test_valid_partial_rows_are_archived_once_without_claiming_completed_success(self):
        fixture=fixtures.ContinuousTests('test_historical_pending_survives_restart_and_replay_ack_is_atomic')
        fixture.setUp()
        try:
            source={'id':'ig-partial','platform':'instagram','url':'https://www.instagram.com/example','name':'Fixture','origin':'general'}
            fixture.schedule.sync({'sources':[source]}, {},{'instagram'})
            source=fixture.schedule.claim(time.time(),limit=1,platforms={'instagram'})[0]
            rows=library_rows(source,[{'url':'https://www.instagram.com/p/ABC/','owner':'example','text':'synthetic partial extraction'}],2,'instaloader')
            keys=[row['canonical_key'] for row in rows]
            now=time.time()
            record={'source_id':source['id'],'url':source['url'],'platform':'instagram',
                'status':'partial_results','checked_at':iso(now),'started_at':iso(now-1),'seconds':1,
                'collector_rows':len(rows),'canonical_keys':keys,'new_keys':[],'rejections':[],
                'attempts':[{'tool':'instaloader','status':'partial_results','posts':1,'stop_reason':'timeout'}]}
            fixture.schedule.stage({'token':source['_lease_token'],'posts':rows,'record':record,'state_update':None},now)
            fixture.stage(2*1024**2)
            item=fixture.pending.prepare();fixture.pending.publish(fixture.store,item)
            self.assertEqual(item['report']['sources'][0]['delivery']['saved_ids'],1)
            # Simulate retry after upload/readback before acknowledgement.
            fixture.pending.publish(fixture.store,item)
            self.assertEqual([p['id'] for p in load_snapshot(fixture.store)['items']],['instagram:ABC'])
            fixture.pending.acknowledge(item)
            self.assertIsNone(fixture.pending.prepare());self.assertEqual(fixture.schedule.pending(),[])
            health=json.loads(fixture.db.execute('SELECT health FROM runtime_schedule WHERE id=?',(source['id'],)).fetchone()['health'])
            self.assertEqual(health['collector_status'],'partial_results')
            self.assertIsNone(health['last_success_at'])
            self.assertIsNotNone(health['last_extraction_at'])
        finally:fixture.doCleanups()
