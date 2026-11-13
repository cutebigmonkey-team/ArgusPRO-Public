import hashlib
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import Mock,MagicMock,patch

sys.path.insert(0,str(Path(__file__).resolve().parent))
import worker
from runtime_policy import require_cloud


class WorkerSafety(unittest.TestCase):
    def test_comments_progress_while_news_child_is_running(self):
        proc=Mock(returncode=0)
        proc.poll.side_effect=[None,None,0]
        progress=Mock()
        with tempfile.TemporaryDirectory() as directory,patch.object(worker.subprocess,'Popen',return_value=proc),patch.object(worker.time,'sleep'),patch.object(worker.time,'monotonic',return_value=10):
            worker.collect({'sources':[]},{},{},Path(directory),Mock(),progress=progress)
        progress.assert_called_once()

    def test_comment_adapter_failure_isolated_but_lost_writer_stops_work(self):
        comments,journal=Mock(),Mock()
        comments.tick.side_effect=ValueError('adapter')
        with patch.object(worker,'emit') as emit:
            worker.service_comments(comments,journal)
            self.assertEqual(emit.call_args.args[0],'comments_cycle_failed')
        journal.check_session.side_effect=RuntimeError('session lost')
        comments.reset_mock()
        with self.assertRaises(RuntimeError):worker.service_comments(comments,journal)
        comments.tick.assert_not_called()

    def test_supervisor_concurrency_is_accepted_by_real_collector_cli(self):
        result=subprocess.run([sys.executable,str(worker.OPS/'collect/run.py'),
            '--sources','unused','--concurrency','6','--stream-results',
            '--batch-seconds','105','--source-timeout','65','--help'],capture_output=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stderr.decode(errors='replace'))

    def test_collector_does_not_inherit_writer_credentials(self):
        with patch.dict(os.environ,{'DATABASE_URL':'private','ARGUS_DRIVE_OAUTH':'private',
                'ARGUS_JSONBIN_KEY':'private','TYPESAFE_API_KEY':'private','PATH':'bin','ARGUS_X_BEARER_TOKEN':'api'},clear=True):
            env=worker.collector_environment('cloud-test')
        self.assertEqual(set(env)&{'DATABASE_URL','ARGUS_DRIVE_OAUTH','ARGUS_JSONBIN_KEY'},set())
        self.assertNotIn('ARGUS_X_BEARER_TOKEN',env)
        self.assertEqual(env['ARGUS_ANONYMOUS_ONLY'],'1')
        self.assertNotIn('GITHUB_ACTIONS',env)
        self.assertNotIn('TYPESAFE_API_KEY',env)

    def test_configured_readers_reach_child_without_enabling_other_adapters(self):
        settings={'ARGUS_SOCIAL_COLLECTION':'1','ARGUS_SOURCE_ADAPTERS':'0',
                  'ARGUS_FACEBOOK_GRAPH':'invalid','ARGUS_FACEBOOK_BROWSER_ONLY':'true',
                  'ARGUS_DRIVE_FOLDER_ID':'private','ARGUS_SOURCE_ACTOR_TOKEN':'private'}
        with patch.dict(os.environ,settings,clear=True):
            env=worker.collector_environment('test')
        self.assertEqual(env['ARGUS_SOCIAL_COLLECTION'],'1')
        self.assertEqual(env['ARGUS_SOURCE_ADAPTERS'],'0')
        self.assertEqual(env['ARGUS_FACEBOOK_BROWSER_ONLY'],'true')
        for name in ('ARGUS_FACEBOOK_GRAPH','ARGUS_ENHANCED_COLLECTION',
                     'ARGUS_DRIVE_FOLDER_ID','ARGUS_SOURCE_ACTOR_TOKEN'):
            self.assertNotIn(name,env)

    def test_every_supported_public_reader_switch_preserves_explicit_on_and_off(self):
        names=('ARGUS_SOURCE_ADAPTERS','ARGUS_FACEBOOK_GRAPH','ARGUS_REVIEWED_FACEBOOK_CLI',
               'ARGUS_FACEBOOK_X','ARGUS_PUBLIC_SOCIAL_ADAPTERS','ARGUS_SOCIAL_COLLECTION',
               'ARGUS_EMBED_COLLECTION','ARGUS_REMAINING_COLLECTION','ARGUS_CANDIDATE_FEEDS',
               'ARGUS_ENHANCED_COLLECTION')
        for value in ('0','1'):
            with self.subTest(value=value),patch.dict(os.environ,{k:value for k in names},clear=True):
                env=worker.collector_environment('flags')
                self.assertEqual({k:env[k] for k in names},{k:value for k in names})
        for value in ('true','false'):
            with patch.dict(os.environ,{'ARGUS_FACEBOOK_BROWSER_ONLY':value},clear=True):
                self.assertEqual(worker.collector_environment('flags')['ARGUS_FACEBOOK_BROWSER_ONLY'],value)
        with patch.dict(os.environ,{},clear=True):
            self.assertFalse(set(names)&set(worker.collector_environment('flags')))

    def test_existing_disabled_source_overrides_shipped_enabled_source(self):
        source={'id':'1','url':'https://example.com/feed','enabled':True}
        merged=worker.merge_catalog({'sources':[source]}, {'sources':[{**source,'enabled':False}]})
        self.assertEqual(merged['sources'],[{**source,'enabled':False}])

    def test_existing_url_is_not_added_again_under_an_old_id(self):
        source={'id':'old','url':'https://example.com/feed'}
        merged=worker.merge_catalog({'sources':[source]}, {'sources':[{**source,'id':'new'}]})
        self.assertEqual(len(merged['sources']),1)

    def pending(self):
        journal=MagicMock()
        pending=worker.Pending(journal)
        blob=b'test-package'
        journal.db.connection.execute.return_value.fetchone.return_value={
            'package':blob,'digest':hashlib.sha256(blob).hexdigest(),
            'report':json.dumps({'run_id':'one','sources':[]})}
        journal.db.connection.execute.return_value.rowcount=1
        journal.db.connection.execute.reset_mock()
        return pending,journal

    def test_failed_archive_ack_keeps_pending_package(self):
        pending,journal=self.pending()
        with patch.object(worker,'drain',side_effect=RuntimeError('archive unavailable')):
            with self.assertRaises(RuntimeError):pending.recover(Mock())
        self.assertFalse(any('DELETE' in call.args[0] for call in journal.db.connection.execute.call_args_list))

    def test_pending_removed_only_after_successful_archive_ack(self):
        pending,journal=self.pending()
        with patch.object(worker,'drain') as drain, patch.object(worker,'emit') as emit:
            self.assertTrue(pending.recover(Mock()))
        names=[call.args[0] for call in emit.call_args_list]
        self.assertLess(names.index('drive_delivery_verified'),names.index('pending_acknowledged'))
        self.assertLess(names.index('pending_acknowledged'),names.index('temporary_package_cleaned'))
        drain.assert_called_once()
        self.assertIn('DELETE',journal.db.connection.execute.call_args.args[0])

    def test_lost_database_session_stops_archive_write(self):
        pending,journal=self.pending()
        journal.check_session.side_effect=RuntimeError('session lost')
        with patch.object(worker,'drain') as drain:
            with self.assertRaises(RuntimeError):pending.recover(Mock())
        drain.assert_not_called()

    def test_corrupt_pending_never_reaches_archive(self):
        pending,journal=self.pending()
        journal.db.connection.execute.return_value.fetchone.return_value['package']=b'corrupt'
        with patch.object(worker,'drain') as drain:
            with self.assertRaisesRegex(ValueError,'pending_checksum'):pending.recover(Mock())
        drain.assert_not_called()

    def test_idle_worker_does_not_eagerly_import_enhanced_engines(self):
        script="import sys,worker; assert not {'crawlee','crawl4ai','scrapy'} & set(sys.modules)"
        result=subprocess.run([sys.executable,'-c',script],capture_output=True,text=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_runtime_must_be_explicit_and_linux_for_hostless(self):
        with patch.dict(os.environ,{},clear=True):
            with self.assertRaises(RuntimeError):require_cloud()
        with patch.dict(os.environ,{'ARGUS_COLLECTOR_RUNTIME':'hostless'},clear=True):
            with patch('sys.platform','linux'):require_cloud()
            with patch('sys.platform','win32'):
                with self.assertRaises(RuntimeError):require_cloud()


if __name__=='__main__':unittest.main()
