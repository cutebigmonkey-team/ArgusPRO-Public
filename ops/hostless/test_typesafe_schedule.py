import os
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from typesafe_schedule import TypeSafeSchedule, worker_class

ENV = {'ARGUS_TYPESAFE_ENABLED': 'true', 'ARGUS_ARCHIVE_WRITER_OWNER': 'hostless',
       'TYPESAFE_API_KEY': 'isolated-not-a-key',
       'ARGUS_TYPESAFE_CONFIG': '{"max_posts":50,"concurrency":2,"daily_requests":2300}'}


class ScheduleTests(unittest.TestCase):
    def test_admin_settings_reach_actual_worker_and_interval(self):
        seen=[]
        class Worker:
            def __init__(self,store,client,config,guard):seen.append(config)
            async def run(self,*args,**kwargs):return {}
        schedule=TypeSafeSchedule(Mock(),clock=lambda:100)
        schedule.settings={'enabled':True,'concurrency':1,'max_posts':2,'interval':900,'model':'jev-1.13.0'}
        with patch.dict(os.environ,ENV,clear=True),patch('typesafe_schedule.worker_class',return_value=(Worker,Mock())):
            schedule.run_due(Mock(),Mock(),Mock())
        self.assertEqual(seen[0]['concurrency'],1)
        self.assertEqual(seen[0]['max_posts'],2)
        self.assertEqual(seen[0]['model'],'jev-1.13.0')
        self.assertEqual(schedule.next_run,1000)

    def test_real_module_load_does_not_import_hostless_worker_as_ai(self):
        Worker, Client = worker_class()
        self.assertEqual(Worker.__module__, 'argus_typesafe_worker')
        self.assertEqual(Client.__module__, 'typesafe')

    def test_missing_secret_and_disabled_never_construct_client(self):
        for changes, event in [({'TYPESAFE_API_KEY': ''}, 'typesafe_unavailable'),
                               ({'ARGUS_TYPESAFE_ENABLED': 'false'}, None)]:
            with patch.dict(os.environ, {**ENV, **changes}, clear=True), patch('typesafe_schedule.worker_class') as load:
                emit=Mock(); TypeSafeSchedule(emit).run_due(Mock(), Mock(), Mock())
                load.assert_not_called()
                if event: self.assertEqual(emit.call_args.args[0], event)
                else: emit.assert_not_called()

    def test_bounded_repeat_uses_verified_snapshot_and_original_limits(self):
        seen=[]
        class Worker:
            def __init__(self, store, client, config, guard):
                seen.append(config);guard()
            async def run(self, snapshot, policy, max_seconds, request_seconds):
                self_snapshot.append(snapshot); self.assert_budget=max_seconds
                seen.append((max_seconds, request_seconds))
                return {'new_completed':1}
        self_snapshot=[]; emit=Mock(); stamp=[0]
        schedule=TypeSafeSchedule(emit, clock=lambda:stamp[0])
        store=Mock();store.read_json.return_value={}
        feed=Mock();feed.head={'last':42};feed.snapshot={'items':[]}
        journal=Mock()
        with patch.dict(os.environ, ENV, clear=True), patch('typesafe_schedule.worker_class',return_value=(Worker,Mock())):
            schedule.run_due(store,feed,journal)
            schedule.run_due(store,feed,journal)
            self.assertEqual(len(self_snapshot),1)
            stamp[0]=301;schedule.run_due(store,feed,journal)
        self.assertEqual(len(self_snapshot),2)
        self.assertIs(self_snapshot[0],feed.snapshot)
        self.assertEqual(seen[0],{'max_posts':4,'concurrency':2,'daily_requests':2300})
        self.assertEqual(seen[1],(60,30))
        self.assertEqual(emit.call_args.args[0],'typesafe_published')

    def test_lower_existing_concurrency_and_batch_limits_are_preserved(self):
        seen=[]
        class Worker:
            def __init__(self,store,client,config,guard):seen.append(config)
            async def run(self,*args,**kwargs):return {}
        env={**ENV,'ARGUS_TYPESAFE_CONFIG':'{"max_posts":3,"concurrency":1,"daily_requests":17}'}
        with patch.dict(os.environ,env,clear=True), patch('typesafe_schedule.worker_class',return_value=(Worker,Mock())):
            TypeSafeSchedule(Mock()).run_due(Mock(),Mock(),Mock())
        self.assertEqual(seen,[{'max_posts':3,'concurrency':1,'daily_requests':17}])

    def test_lost_session_prevents_egress(self):
        journal=Mock();journal.check_session.side_effect=RuntimeError('lost')
        with patch.dict(os.environ, ENV, clear=True), patch('typesafe_schedule.worker_class') as load:
            with self.assertRaises(RuntimeError):TypeSafeSchedule(Mock()).run_due(Mock(),Mock(),journal)
            load.assert_not_called()

    def test_failure_is_logged_without_secret_and_retried_next_slot(self):
        emit=Mock();stamp=[0];schedule=TypeSafeSchedule(emit,clock=lambda:stamp[0])
        with patch.dict(os.environ, ENV, clear=True), patch('typesafe_schedule.worker_class',side_effect=ValueError('secret')) as load:
            schedule.run_due(Mock(),Mock(),Mock())
            self.assertEqual(emit.call_args.kwargs,{'error_type':'ValueError'})
            schedule.run_due(Mock(),Mock(),Mock());self.assertEqual(load.call_count,1)
            stamp[0]=301;schedule.run_due(Mock(),Mock(),Mock());self.assertEqual(load.call_count,2)


if __name__ == '__main__': unittest.main()
