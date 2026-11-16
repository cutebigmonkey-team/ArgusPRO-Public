"""Real advisory-lock/rollback gate; only the existing disposable image-test DB."""
from contextlib import ExitStack
import copy
import hashlib
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
import uuid
from unittest.mock import Mock,patch

OPS=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(OPS/p) for p in ('hostless','drive','collect','local-storage')]
import worker
import continuous_runtime as runtime
from startup import Startup,StartupFailure,StartupCheckFailed
from test_publish import MemoryStore
from comments_store import Comments
from schedule_store import ScheduleStore


@unittest.skipUnless(os.getenv('ARGUS_COMMENTS_TEST_DSN'),'disposable PostgreSQL DSN unavailable')
class StartupPostgresTests(unittest.TestCase):
    def setUp(self):
        import psycopg
        from psycopg.rows import dict_row
        self.dsn=os.environ['ARGUS_COMMENTS_TEST_DSN']
        self.admin=psycopg.connect(self.dsn,autocommit=True,row_factory=dict_row)
        self.addCleanup(self.admin.close)
        if self.admin.execute('SELECT current_database() AS name').fetchone()['name']!='argus_comments_test':
            self.fail('only disposable argus_comments_test is permitted')
        self.store=MemoryStore();self.store.root='startup-fixture-'+uuid.uuid4().hex
        self.store.service=SimpleNamespace(close=Mock())
        self.schema='argus_drive_'+hashlib.sha256(self.store.root.encode()).hexdigest()[:24]
        self.owner=runtime.open_journal(self.dsn,self.schema)
        self.addCleanup(lambda:self.admin.execute('DROP SCHEMA '+self.schema+' CASCADE'))
        self.addCleanup(self.owner.close)
        self.db=self.owner.db.connection
        self.queue=ScheduleStore(self.db)
        self.source={'id':'preserve','platform':'rss','url':'https://fixture.invalid/feed'}
        self.store.json('sources.json', {'sources':[self.source]})
        self.queue.sync({'sources':[self.source]}, {}, {'rss'})
        worker.Pending(self.owner,self.queue);Comments(self.db).close()
        self.db.execute("INSERT INTO runtime_pending VALUES(1,'preserve',%s,%s)",(b'pending-bytes','{"sources":[]}'))
        self.db.execute('UPDATE runtime_schedule SET lease_until=123456,lease_token=%s',('preserve-lease',))
        self.store.json('collection-cursors.json',{'sources':{'preserve':{'before':123}}})
        self.before_drive=copy.deepcopy(self.store.files)
        self.before=self.snapshot()

    def snapshot(self):
        # Existing durable rows only. Additive constructor DDL is not a data reset.
        return {name:self.admin.execute('SELECT * FROM '+self.schema+'.'+name).fetchall()
                for name in ('runtime_schedule','runtime_pending','runtime_results','comment_jobs')}

    def test_competing_startup_keeps_existing_session_and_pending(self):
        events=[]
        with self.assertRaises(StartupFailure) as caught:
            with Startup(lambda e,**k:events.append((e,k))).phase('database_session'):
                runtime.open_journal(self.dsn,self.schema)
        self.assertEqual(caught.exception.code,'database_writer_lock_occupied')
        self.owner.check_session();self.assertEqual(self.snapshot(),self.before)
        self.assertEqual(self.store.files,self.before_drive)

    def test_same_pid_without_owned_lock_is_rejected(self):
        key=int.from_bytes(hashlib.sha256(self.schema.encode()).digest()[:8],'big',signed=True)
        self.db.execute('SELECT pg_advisory_unlock(%s)',(key,))
        with self.assertRaises(StartupCheckFailed) as caught:self.owner.check_session()
        self.assertEqual(str(caught.exception),'database_writer_lock_lost')
        self.assertEqual(self.snapshot(),self.before)

    def test_real_backend_switch_is_rejected_before_archive_write(self):
        import psycopg
        other=psycopg.connect(self.dsn,autocommit=True,row_factory=__import__('psycopg.rows',fromlist=['dict_row']).dict_row)
        original=self.owner.db.connection
        try:
            self.owner.db.connection=other
            with self.assertRaises(StartupCheckFailed) as caught:self.owner.check_session()
            self.assertEqual(str(caught.exception),'database_session_changed')
        finally:
            self.owner.db.connection=original;other.close()
        self.owner.check_session();self.assertEqual(self.snapshot(),self.before)

    def test_real_connection_limit_is_distinct_from_writer_contention(self):
        import psycopg
        from psycopg.conninfo import make_conninfo
        from session_store import failure_code,database_sqlstate
        role='argus_limit_'+uuid.uuid4().hex
        self.admin.execute('CREATE ROLE '+role+' LOGIN CONNECTION LIMIT 1')
        first=None
        try:
            first=psycopg.connect(make_conninfo(self.dsn,user=role),autocommit=True)
            with self.assertRaises(psycopg.Error) as caught:
                psycopg.connect(make_conninfo(self.dsn,user=role),autocommit=True)
            self.assertIn(database_sqlstate(caught.exception),(None,'53300'))
            self.assertEqual(failure_code(caught.exception),'database_connection_limit')
            self.owner.check_session();self.assertEqual(self.snapshot(),self.before)
        finally:
            if first is not None:first.close()
            self.admin.execute('DROP ROLE '+role)

    def test_server_sqlstate_53300_remains_authoritative(self):
        import psycopg
        from session_store import failure_code,database_sqlstate
        with self.assertRaises(psycopg.Error) as caught:
            self.db.execute("DO $$ BEGIN RAISE EXCEPTION 'disposable capacity fixture' USING ERRCODE='53300'; END $$")
        self.assertEqual(database_sqlstate(caught.exception),'53300')
        self.assertEqual(failure_code(caught.exception),'database_connection_limit')
        self.owner.check_session();self.assertEqual(self.snapshot(),self.before)

    def fail_runtime(self,phase):
        events=[];stack=ExitStack()
        with stack:
            stack.enter_context(patch.dict(os.environ,{
                'ARGUS_COLLECTOR_RUNTIME':'hostless','ARGUS_RUNTIME_MODE':'collect','ARGUS_MIN_PRIVATE_SOURCES':'1',
                'ARGUS_ARCHIVE_WRITER_OWNER':'hostless','DATABASE_URL':self.dsn,
                'ARGUS_DRIVE_OAUTH':'fixture','ARGUS_DRIVE_FOLDER_ID':'fixture',
                'ARGUS_JSONBIN_KEY':'fixture','ARGUS_JSONBIN_BIN_ID':'fixture'},clear=True))
            stack.enter_context(patch.object(worker,'emit',side_effect=lambda e,**k:events.append((e,k))))
            stack.enter_context(patch.object(worker,'from_environment',return_value=self.store))
            stack.enter_context(patch.object(worker,'fetch_registry',return_value={'collectors':[]}))
            stack.enter_context(patch.object(runtime,'open_journal',return_value=self.owner))
            if phase=='archive_index_initialization':
                stack.enter_context(patch.object(runtime,'IndexedFeedJournal',side_effect=RuntimeError('fixture')))
            else:
                feed=SimpleNamespace(index=SimpleNamespace(rows=lambda:iter([])),verified_state={'sources':{}},close=Mock(),restore_cursors=Mock(),verify_head=Mock(),cursor=SimpleNamespace(verify=Mock()))
                stack.enter_context(patch.object(runtime,'IndexedFeedJournal',return_value=feed))
                stack.enter_context(patch.object(worker,'with_submissions',side_effect=lambda store,catalog:catalog))
                stack.enter_context(patch.object(worker,'merge_catalog',return_value={'sources':[self.source]}))
                stack.enter_context(patch.object(runtime,'Loop',side_effect=RuntimeError('fixture')))
            recover=stack.enter_context(patch.object(worker,'recover_drive'))
            with self.assertRaises(StartupFailure) as caught:runtime.run(worker)
            self.assertEqual(caught.exception.phase,phase);recover.assert_not_called()
        self.assertTrue(self.db.closed)
        self.assertEqual(self.snapshot(),self.before)
        self.assertEqual(self.store.files,self.before_drive)
        # Only the failed worker's own connection was closed; same schema remains
        # recoverable under the original advisory key without deleting state.
        successor=runtime.open_journal(self.dsn,self.schema)
        try:successor.check_session();self.assertEqual(self.snapshot(),self.before)
        finally:successor.close()

    def test_index_failure_releases_only_own_session(self):
        self.fail_runtime('archive_index_initialization')

    def test_pipeline_failure_rolls_back_real_postgres_initialization(self):
        self.fail_runtime('pipeline_initialization')
