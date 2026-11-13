"""Required image gate: existing build-only Unix-socket test database, never prod."""
import os
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace
import unittest
import uuid
for folder in ('hostless','drive','collect','local-storage'):
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/folder))
from schedule_store import ScheduleStore,iso
from worker import Pending
from serialized_db import Connection


@unittest.skipUnless(os.getenv('ARGUS_COMMENTS_TEST_DSN'),'disposable PostgreSQL DSN unavailable')
class ContinuousPostgres(unittest.TestCase):
    def setUp(self):
        import psycopg
        from psycopg.rows import dict_row
        self.db=psycopg.connect(os.environ['ARGUS_COMMENTS_TEST_DSN'],autocommit=True,row_factory=dict_row)
        self.addCleanup(self.db.close)
        if self.db.execute('SELECT current_database() AS name').fetchone()['name']!='argus_comments_test':
            self.fail('only disposable argus_comments_test is permitted')
        self.schema='continuous_test_'+uuid.uuid4().hex
        self.db.execute('CREATE SCHEMA '+self.schema)
        self.addCleanup(lambda:self.db.execute('DROP SCHEMA '+self.schema+' CASCADE'))
        self.db.execute('SET search_path TO '+self.schema)
        self.queue=ScheduleStore(self.db)
        self.source={'id':'fixture','url':'https://fixture.example/rss','platform':'rss'}
        self.queue.sync({'sources':[self.source]},{},{'rss'})
        self.journal=SimpleNamespace(db=SimpleNamespace(connection=self.db),check_session=lambda:None)

    def staged(self):
        now=time.time();source=self.queue.claim(now,limit=1)[0]
        record={'source_id':'fixture','platform':'rss','url':self.source['url'],'status':'no_new_posts',
                'checked_at':iso(now),'seconds':1,'canonical_keys':[],'new_keys':[],'attempts':[]}
        self.queue.stage({'token':source['_lease_token'],'record':record,'posts':[]},now)
        return source,record

    def test_ack_delete_failure_rolls_back_source_result_and_health_together(self):
        source,record=self.staged();pending=Pending(self.journal,self.queue)
        self.db.execute("INSERT INTO runtime_pending VALUES(1,'digest',%s,'{}')",(b'fixture',))
        self.db.execute("""CREATE FUNCTION reject_delete() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'fixture interrupted acknowledgement'; RETURN OLD; END $$""")
        self.db.execute('CREATE TRIGGER interrupt_ack BEFORE DELETE ON runtime_pending FOR EACH ROW EXECUTE FUNCTION reject_delete()')
        item={'digest':'digest','report':{'run_id':'fixture','sources':[record],
             'lease_tokens':{'fixture':source['_lease_token']}}}
        with self.assertRaises(Exception):pending.acknowledge(item)
        self.assertEqual(len(self.queue.pending()),1)
        self.assertEqual(self.db.execute('SELECT last_completed_at FROM runtime_schedule').fetchone()['last_completed_at'],0)
        self.assertEqual(self.db.execute('SELECT COUNT(*) AS n FROM runtime_pending').fetchone()['n'],1)
        self.db.execute('DROP TRIGGER interrupt_ack ON runtime_pending');pending.acknowledge(item)
        self.assertEqual(self.queue.pending(),[])

    def test_restart_preserves_staged_result_and_lease(self):
        source,record=self.staged();restarted=ScheduleStore(self.db);restarted.release_abandoned()
        self.assertEqual(restarted.pending()[0]['record'],record)
        self.assertEqual(restarted.claim(time.time()+1000),[])

    def test_auxiliary_transaction_cannot_commit_coordinator_work(self):
        wrapped=Connection(self.db,threading.RLock());started=threading.Event();finished=threading.Event()
        def auxiliary():
            started.set()
            with wrapped.transaction():wrapped.execute("UPDATE runtime_schedule SET priority=2")
            finished.set()
        with self.assertRaises(RuntimeError):
            with wrapped.transaction():
                wrapped.execute('UPDATE runtime_schedule SET priority=1')
                thread=threading.Thread(target=auxiliary);thread.start();self.assertTrue(started.wait(1))
                self.assertFalse(finished.wait(.02));raise RuntimeError('rollback fixture')
        thread.join(2);self.assertTrue(finished.is_set())
        self.assertEqual(self.db.execute('SELECT priority FROM runtime_schedule').fetchone()['priority'],2)
