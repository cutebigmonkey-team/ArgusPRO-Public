"""Offline startup failure injection; no credentials, network or production DB."""
from contextlib import contextmanager, ExitStack
import builtins
import copy
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

OPS=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(OPS/p) for p in ('hostless','drive','collect','local-storage')]
import worker
import continuous_runtime as runtime
from startup import Startup, StartupFailure, PHASES, CODES
from test_publish import MemoryStore
from publish import publish
from storage import pack
from comments_store import Comments
from schedule_store import ScheduleStore

SECRET='private-password-canary'
ENV={'ARGUS_COLLECTOR_RUNTIME':'hostless','ARGUS_RUNTIME_MODE':'collect',
     'ARGUS_ARCHIVE_WRITER_OWNER':'hostless','ARGUS_DRIVE_OAUTH':SECRET,
     'ARGUS_DRIVE_FOLDER_ID':'private-drive-id-canary','ARGUS_JSONBIN_KEY':SECRET,
     'ARGUS_JSONBIN_BIN_ID':'private-registry-id-canary',
     'DATABASE_URL':'postgresql://user:'+SECRET+'@private-host/database'}


class TestConnection:
    """SQLite execution adapter, not a simulated PostgreSQL lock guarantee."""
    def __init__(self,db): self.db=db; self.depth=0
    def execute(self,query,params=()):return self.db.execute(query.replace('%s','?'),params)
    def cursor(self):
        cursor=self.db.cursor()
        return SimpleNamespace(executemany=lambda q,args:cursor.executemany(q.replace('%s','?'),args),close=cursor.close)
    @contextmanager
    def transaction(self):
        self.depth+=1;name='startup_'+str(self.depth)
        self.db.execute('SAVEPOINT '+name)
        try:yield
        except BaseException:
            self.db.execute('ROLLBACK TO '+name);raise
        finally:
            self.db.execute('RELEASE '+name);self.depth-=1


class TestComments(Comments):
    def __init__(self,db):super().__init__(db,sqlite=True)
    def transaction(self):return self.db.transaction()


class StartupTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.db=sqlite3.connect(':memory:',isolation_level=None);self.db.row_factory=sqlite3.Row
        self.addCleanup(self.db.close);self.raw=TestConnection(self.db)
        self.journal=SimpleNamespace(db=SimpleNamespace(connection=self.raw),
                                     check_session=Mock(),close=Mock())
        self.store=MemoryStore();self.store.root=ENV['ARGUS_DRIVE_FOLDER_ID']
        self.store.service=SimpleNamespace(close=Mock())
        self.source={'id':'one','platform':'rss','url':'https://fixture.invalid/feed'}
        self.catalog={'sources':[self.source]}
        self.store.json('sources.json',self.catalog)
        self.store.json('collection-cursors.json',{'schema':'argus-collection-cursors-v1',
                         'sources':{},'incremental_sources':{'one':{'url':self.source['url'],'before':123}}})
        post={'canonical_key':'rss:fixture','platform':'rss','post_url':'https://fixture.invalid/article',
              'source_id':'one','source_url':self.source['url'],'text':'preserve archived content',
              'published_at':None,'observed_at':'2026-11-13T17:00:00Z','media':[],
              'public_comments':[{'id':'comment','text':'preserve'}]}
        rows=self.root/'posts.jsonl';rows.write_text(json.dumps(post)+'\n');(self.root/'media').mkdir()
        package=self.root/'package.zip';pack(rows,self.root/'media',package)
        publish(self.store,package,measure=True)
        queue=ScheduleStore(self.raw);queue.sync(self.catalog,{}, {'rss'})
        worker.Pending(self.journal,queue);TestComments(self.raw).close()
        self.db.execute("INSERT INTO runtime_pending VALUES(1,'preserve',?,?)",(b'pending-bytes','{"sources":[]}'))
        self.before_db=list(self.db.iterdump());self.before_drive=copy.deepcopy(self.store.files)
        self.events=[];self.loop=SimpleNamespace(tick=Mock(return_value=False),close=Mock(),stop=Mock())
        self.stack=ExitStack();self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ,ENV,clear=True))
        self.stack.enter_context(patch.object(worker,'emit',side_effect=lambda e,**k:self.events.append({'event':e,**k})))
        self.access=self.stack.enter_context(patch.object(worker,'from_environment',return_value=self.store))
        self.stack.enter_context(patch.object(worker,'fetch_registry',return_value={'collectors':[{'platform':'rss','enabled':True}]}))
        self.stack.enter_context(patch.object(worker,'with_submissions',side_effect=lambda s,c:c))
        self.stack.enter_context(patch.object(worker,'merge_catalog',return_value=self.catalog))
        self.recover=self.stack.enter_context(patch.object(worker,'recover_drive'))
        self.stack.enter_context(patch('comments_store.Comments',TestComments))
        self.open=self.stack.enter_context(patch.object(runtime,'open_journal',return_value=self.journal))
        self.stack.enter_context(patch.object(runtime,'Loop',return_value=self.loop))

    def unchanged(self):
        self.assertEqual(self.store.files,self.before_drive)
        self.assertEqual(list(self.db.iterdump()),self.before_db)
        self.recover.assert_not_called()
        self.assertNotIn('startup_completed',[e['event'] for e in self.events])
        text=json.dumps(self.events)
        for secret in (SECRET,ENV['DATABASE_URL'],ENV['ARGUS_DRIVE_FOLDER_ID'],ENV['ARGUS_JSONBIN_BIN_ID']):
            self.assertNotIn(secret,text)

    def failed(self,phase,code):
        with self.assertRaises(StartupFailure) as caught:runtime.run(worker)
        self.assertEqual((caught.exception.phase,caught.exception.code),(phase,code))
        self.assertEqual(worker.error_category(caught.exception),code)
        failures=[e for e in self.events if e['event']=='startup_phase_failed']
        expected=[(phase,code)]
        if phase=='pipeline_initialization':expected.append(('runtime_state_initialization',code))
        self.assertEqual([(e['phase'],e['error_code']) for e in failures],expected)
        self.unchanged()

    def test_cloud_environment_invalid(self):
        os.environ.pop('ARGUS_COLLECTOR_RUNTIME');self.failed('cloud_environment','cloud_environment_invalid')
        self.access.assert_not_called();self.open.assert_not_called()

    def test_runtime_import_failure_is_logged_before_run(self):
        original=builtins.__import__
        def load(name,*args,**kwargs):
            if name=='continuous_runtime':raise RuntimeError(SECRET)
            return original(name,*args,**kwargs)
        with patch('builtins.__import__',side_effect=load),self.assertRaises(StartupFailure) as caught:
            worker.run()
        self.assertEqual((caught.exception.phase,caught.exception.code),('runtime_import','runtime_import_failed'))
        self.unchanged();self.access.assert_not_called();self.open.assert_not_called()

    def test_missing_each_required_configuration(self):
        for key in ('ARGUS_DRIVE_OAUTH','ARGUS_DRIVE_FOLDER_ID','ARGUS_JSONBIN_KEY','ARGUS_JSONBIN_BIN_ID','DATABASE_URL'):
            with self.subTest(key=key),patch.dict(os.environ,ENV,clear=True):
                self.events.clear();os.environ.pop(key)
                self.failed('configuration','configuration_missing')
        self.access.assert_not_called();self.open.assert_not_called()

    def test_invalid_mode_and_continuous_limits(self):
        for key in ('ARGUS_RUNTIME_MODE','ARGUS_CONTINUOUS_FAST_SOURCES'):
            with self.subTest(key=key),patch.dict(os.environ,{key:SECRET}):
                self.events.clear();self.failed('configuration','configuration_invalid')

    def test_cutover_is_required_before_any_external_access(self):
        os.environ.pop('ARGUS_ARCHIVE_WRITER_OWNER')
        self.failed('archive_writer_cutover','archive_writer_cutover_not_confirmed')
        self.access.assert_not_called();self.open.assert_not_called()

    def test_drive_access_failure_closes_created_client(self):
        with patch.object(self.store,'assert_private',side_effect=RuntimeError(SECRET)):
            self.failed('archive_access','archive_access_failed')
        self.store.service.close.assert_called_once();self.open.assert_not_called()

    def test_registry_runtime_error_is_classified_without_text(self):
        for error in (RuntimeError('JSONBin HTTP 403'),RuntimeError('JSONBin connection failed'),RuntimeError(SECRET)):
            with self.subTest(error_type=type(error).__name__),patch.object(worker,'fetch_registry',side_effect=error):
                self.events.clear();self.failed('registry_load','registry_load_failed')
        self.open.assert_not_called()

    def test_existing_database_writer_is_never_replaced(self):
        self.open.side_effect=RuntimeError('collector_writer_already_running')
        self.failed('database_session','database_writer_lock_occupied')
        self.journal.close.assert_not_called()

    def test_session_pool_change_stops_and_closes_only_own_session(self):
        self.journal.check_session.side_effect=RuntimeError('database_session_changed_use_session_pooling')
        self.failed('database_session','database_session_changed')
        self.journal.close.assert_called_once()

    def test_database_connection_failure(self):
        self.open.side_effect=RuntimeError(ENV['DATABASE_URL'])
        self.failed('database_session','database_session_initialization_failed')

    def test_session_change_after_index_read_still_prevents_startup_writes(self):
        self.journal.check_session.side_effect=[None,RuntimeError('database_session_changed_use_session_pooling')]
        self.failed('journal_verification','database_session_changed')
        self.journal.close.assert_called_once()

    def test_index_failure_releases_session_without_touching_archive(self):
        for error in (RuntimeError(SECRET),OSError('disk full '+SECRET)):
            with self.subTest(error_type=type(error).__name__),patch.object(runtime,'IndexedFeedJournal',side_effect=error):
                self.events.clear();self.failed('archive_index_initialization','archive_index_initialization_failed')
        self.assertEqual(self.journal.close.call_count,2)

    def test_corrupted_real_index_page_preserves_bytes(self):
        self.store.json('live-feed.json',{'id':'fixture-id','sha':'a'*64,'first':1,'last':1})
        self.before_drive=copy.deepcopy(self.store.files)
        self.failed('archive_index_initialization','archive_index_initialization_failed')

    def test_source_configuration_failure_does_not_compact(self):
        with patch.object(worker,'with_submissions',side_effect=RuntimeError(SECRET)):
            self.failed('source_configuration','source_configuration_failed')

    def test_late_registry_failure_rolls_back_scheduler(self):
        original=ScheduleStore.sync
        def fail(schedule,*args,**kwargs):
            original(schedule,*args,**kwargs)
            schedule.execute('UPDATE runtime_schedule SET priority=99')
            raise RuntimeError(SECRET)
        with patch.object(ScheduleStore,'sync',new=fail):
            with self.assertRaises(StartupFailure) as caught:runtime.run(worker)
        self.assertEqual(caught.exception.phase,'source_registry_reconciliation')
        self.unchanged()

    def test_executor_initialization_failure_rolls_back_sql(self):
        with patch.object(runtime,'Loop',side_effect=RuntimeError(SECRET)):
            self.failed('pipeline_initialization','pipeline_initialization_failed')

    def test_success_logs_order_and_recovers_only_after_startup(self):
        self.recover.side_effect=lambda *a,**k:self.events.append({'event':'test_recover_called'})
        runtime.run(worker)
        events=[e['event'] for e in self.events]
        self.assertLess(events.index('startup_completed'),events.index('test_recover_called'))
        self.assertEqual(self.store.files,self.before_drive)
        self.assertEqual(self.db.execute('SELECT package FROM runtime_pending').fetchone()['package'],b'pending-bytes')
        self.journal.close.assert_called_once();self.loop.close.assert_called_once()
        started=[e['phase'] for e in self.events if e['event']=='startup_phase_started']
        completed=[e['phase'] for e in self.events if e['event']=='startup_phase_completed']
        self.assertEqual(set(started),set(PHASES)-{'preflight_archive_read','runtime_import','comments_initialization','comments_synchronization'})
        self.assertEqual(set(started),set(completed))

    def test_preflight_does_not_acquire_writer_or_mutate(self):
        os.environ['ARGUS_RUNTIME_MODE']='preflight';os.environ.pop('ARGUS_ARCHIVE_WRITER_OWNER')
        runtime.run(worker);self.open.assert_not_called();self.recover.assert_not_called()
        self.assertEqual(self.store.files,self.before_drive);self.assertEqual(list(self.db.iterdump()),self.before_db)

    def test_preflight_read_failure(self):
        os.environ['ARGUS_RUNTIME_MODE']='preflight'
        with patch('publish.load_base_snapshot',side_effect=RuntimeError(SECRET)):
            self.failed('preflight_archive_read','archive_read_verification_failed')
        self.open.assert_not_called()

    def test_recovery_failure_is_runtime_failure_and_preserves_pending(self):
        self.recover.side_effect=RuntimeError(SECRET)
        with self.assertRaises(RuntimeError):runtime.run(worker)
        self.assertIn('startup_completed',[e['event'] for e in self.events])
        self.assertEqual([e['phase'] for e in self.events if e['event']=='startup_phase_failed'],['pending_recovery'])
        self.assertEqual(next(e['error_code'] for e in self.events if e['event']=='runtime_recovery_failed'),'pending_recovery_failed')
        self.assertEqual(self.store.files,self.before_drive)
        self.assertEqual(self.db.execute('SELECT package FROM runtime_pending').fetchone()['package'],b'pending-bytes')
        self.assertNotIn(SECRET,json.dumps(self.events))

    def test_logger_all_phases_never_serializes_error_or_dynamic_class(self):
        for phase,code in PHASES.items():
            events=[]
            with self.subTest(phase=phase),self.assertRaises(StartupFailure):
                with Startup(lambda e,**k:events.append({'event':e,**k})).phase(phase):
                    raise type(SECRET,(RuntimeError,),{})(ENV['DATABASE_URL'])
            self.assertEqual(events[-1]['error_code'],code)
            self.assertNotIn(SECRET,json.dumps(events));self.assertNotIn('postgresql://',json.dumps(events))
        self.assertIsNone(worker.safe_sqlstate(SimpleNamespace(sqlstate=SECRET)))
        self.assertEqual(set(PHASES.values())-CODES,set())

    def test_real_entrypoint_never_logs_invalid_mode_value(self):
        env={**os.environ,'ARGUS_RUNTIME_MODE':SECRET}
        proc=subprocess.Popen([sys.executable,str(OPS/'hostless/worker.py')],env=env,
                              stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        try:
            try:out,err=proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill();out,err=proc.communicate(timeout=2)
        finally:
            if proc.poll() is None:proc.kill();proc.wait()
        self.assertNotIn(SECRET,out+err)
        events=[json.loads(line) for line in out.splitlines()]
        self.assertEqual(events[0]['mode'],'invalid')
        self.assertEqual(next(e for e in events if e['event']=='worker_error')['category'],'configuration_invalid')
