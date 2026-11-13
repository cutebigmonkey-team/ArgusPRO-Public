import json
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
for p in ('hostless','drive','collect','local-storage'):
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/p))
from comments_store import Comments, iso
from comment_schedule import interval


class DurableSchedule(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(':memory:');self.db.row_factory=sqlite3.Row
        self.addCleanup(self.db.close)
        self.now=1793880000
        self.c=Comments(self.db,sqlite=True,clock=lambda:self.now)
        self.post=dict(id='live',platform='telegram',source_id='s',url='https://t.me/channel/1',published_at=iso(self.now))
        self.c.sync([self.post])

    def job(self,id='live'):
        return self.db.execute('SELECT * FROM comment_jobs WHERE id=?',(id,)).fetchone()

    def test_targets_and_activity_decay(self):
        self.assertEqual(interval(300,0,0,self.now),120)
        self.assertEqual(interval(90000,self.now-100,0,self.now),180)
        self.assertEqual(interval(36000,0,0,self.now),600)
        self.assertEqual(interval(200000,0,0,self.now),1800)
        self.assertEqual(interval(900000,0,0,self.now),21600)
        self.assertEqual(interval(900000,0,5,self.now),86400)

    def test_activity_counts_new_ids_not_edits_or_replay(self):
        job=self.job()
        self.c.acknowledge(job,dict(status='success',rows=[dict(id='a',text='One')]))
        self.now+=600
        self.c.acknowledge(self.job(),dict(status='success',rows=[dict(id='a',text='Edited')]))
        activity=self.db.execute('SELECT * FROM comment_activity').fetchone()
        self.assertEqual(activity['last_new'],self.now-600)
        self.assertEqual(activity['new_count'],0)
        self.assertEqual(self.c.acknowledge(job,dict(status='success',rows=[dict(id='bad',text='Delayed')])),0)

    def test_cursor_and_retry_after_are_atomic_and_replayable(self):
        self.c.acknowledge(self.job(),dict(status='partial',rows=[dict(id='a',text='One')],cursor={'page':'2'},retry_after=900))
        self.assertEqual(self.job()['due'],self.now+900)
        self.assertEqual(self.c.due(),[])
        self.now+=901
        resumed=Comments(self.db,sqlite=True,clock=lambda:self.now)
        self.assertEqual(json.loads(resumed.due()[0]['cursor']),{'page':'2'})
        with patch.object(resumed,'upsert',side_effect=RuntimeError('write failure')):
            with self.assertRaises(RuntimeError):resumed.acknowledge(self.job(),dict(status='success',rows=[]))
        self.assertEqual(json.loads(self.job()['cursor']),{'page':'2'})

    def test_stalled_pagination_retains_checkpoint_with_backoff(self):
        self.c.acknowledge(self.job(),dict(status='partial',rows=[],cursor={'page':'same'}))
        self.now+=31
        self.c.acknowledge(self.job(),dict(status='partial',rows=[dict(id='a',text='Keep')],cursor={'page':'same'}))
        self.assertEqual(self.job()['reason'],'pagination_stalled')
        self.assertEqual(self.job()['status'],'failed')
        self.assertGreaterEqual(self.job()['due'],self.now+120)
        self.assertEqual(self.c.metrics()['comments'],1)

    def test_backfill_cannot_starve_live_or_continuation(self):
        self.c.acknowledge(self.job(),dict(status='success',rows=[dict(id='a',text='Fresh')]))
        self.now+=121
        old=[{**self.post,'id':str(i),'published_at':iso(self.now-864000),'url':'https://t.me/channel/'+str(i+10)} for i in range(100)]
        self.c.sync(old)
        # An active lane must select the live discussion even with 100 unchecked jobs.
        self.db.execute("UPDATE comment_lane_turns SET turns=2");self.db.commit()
        self.assertEqual(self.c.due(1)[0]['id'],'live')
        self.c.acknowledge(self.job(),dict(status='partial',rows=[],cursor={'page':'2'}))
        self.now+=31
        self.db.execute("UPDATE comment_lane_turns SET turns=1");self.db.commit()
        self.assertEqual(self.c.due(1)[0]['id'],'live')
        self.db.execute("UPDATE comment_lane_turns SET turns=0");self.db.commit()
        self.assertNotEqual(self.c.due(1)[0]['id'],'live')

    def test_source_disabled_during_request_discards_result(self):
        job=self.job()
        self.c.sync([self.post],[])
        self.assertEqual(self.c.acknowledge(job,dict(status='success',rows=[dict(id='a',text='Late')])),0)
        self.assertEqual(self.c.metrics()['comments'],0)

    def test_status_health_contains_pending_and_pause(self):
        self.c.acknowledge(self.job(),dict(status='rate_limited',retry_after=3600))
        health=self.c.metrics()['platforms']['telegram']
        self.assertEqual(health['statuses'],{'rate_limited':1})
        self.assertGreaterEqual(health['paused_until'],self.now+3600)


if __name__=='__main__':unittest.main()
