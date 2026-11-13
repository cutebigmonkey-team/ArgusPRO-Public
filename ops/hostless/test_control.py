import json
import sqlite3
import unittest
from unittest.mock import Mock,patch
from test_schedule_store import source,payload,acknowledge
from schedule_store import ScheduleStore
from control import Control,validate_patch,apply_catalog

class ControlTests(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(':memory:');self.db.row_factory=sqlite3.Row
        self.schedule=ScheduleStore(self.db,sqlite=True);self.control=Control(self.schedule)
        self.catalog={'sources':[source(1),source(2)]}
    def tearDown(self):self.db.close()
    def command(self,patch,actor='owner',revision=0,id='one',action='update'):
        value={'format':'argus-control-command-v1','revision':revision,'action':action}
        if patch is not None:value['patch']=patch
        self.control.process({'id':id,'lastModifyingUser':{'permissionId':actor},'description':json.dumps(value)},
            {'owner'},{'1','2'},Mock())
    def test_nonowner_cannot_change_global_configuration(self):
        self.command({'sources':{'1':{'enabled':False}}},actor='reader')
        self.assertEqual(self.control.current(),(0,{}))
        self.assertEqual(self.db.execute('SELECT status FROM runtime_control_audit').fetchone()[0],'rejected')
    def test_unknown_actor_fails_closed(self):
        self.command({'ai':{'enabled':True}},actor='')
        self.assertEqual(self.control.current(),(0,{}))
    def test_invalid_and_secret_fields_never_enter_audit_or_settings(self):
        self.command({'ai':{'api_key':'not-a-real-key'}})
        self.assertEqual(self.control.current(),(0,{}))
        self.assertNotIn('not-a-real-key',str([tuple(r) for r in self.db.execute('SELECT * FROM runtime_control_audit')]))
    def test_revision_conflict_and_replay(self):
        self.command({'sources':{'1':{'enabled':False}}})
        self.command({'sources':{'1':{'enabled':True}}},id='two')
        self.command({'sources':{'1':{'enabled':True}}},revision=1)
        self.assertEqual(self.control.current(),(1,{'sources':{'1':{'enabled':False}}}))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM runtime_control_audit').fetchone()[0],2)
    def test_config_is_loaded_from_existing_database_after_controller_restart(self):
        self.command({'ai':{'concurrency':1}})
        self.assertEqual(Control(self.schedule).current(),(1,{'ai':{'concurrency':1}}))
    def test_disable_really_removes_source_from_scheduler_claim(self):
        self.command({'sources':{'1':{'enabled':False}}})
        self.schedule.sync(apply_catalog(self.catalog,self.control.current()[1]),{},{'rss'})
        self.assertEqual([r['id'] for r in self.schedule.claim(1000)],['2'])
        self.assertNotIn('enabled',self.catalog['sources'][0])
    def test_interval_and_backoff_change_real_next_poll(self):
        self.command({'sources':{'1':{'interval':1200,'retry_delay':7200}}})
        self.schedule.sync(apply_catalog(self.catalog,self.control.current()[1]),{},{'rss'})
        item=next(s for s in self.schedule.claim(1000) if s['id']=='1')
        p=payload(item);self.schedule.stage(p,1001);acknowledge(self.schedule,[p])
        self.assertEqual(self.db.execute("SELECT next_check_at FROM runtime_schedule WHERE id='1'").fetchone()[0],2200)
        item=next(s for s in self.schedule.claim(2201) if s['id']=='1');p=payload(item,when=2300,status='timeout')
        self.schedule.stage(p,2301);acknowledge(self.schedule,[p])
        self.assertGreaterEqual(self.db.execute("SELECT next_check_at FROM runtime_schedule WHERE id='1'").fetchone()[0],9500)
    def test_retry_limit_pauses_repeated_failure(self):
        self.command({'sources':{'1':{'retry_limit':0}}})
        catalog=apply_catalog(self.catalog,self.control.current()[1]);self.schedule.sync(catalog,{},{'rss'})
        item=next(s for s in self.schedule.claim(1000) if s['id']=='1')
        p=payload(item,status='timeout');self.schedule.stage(p,1001);acknowledge(self.schedule,[p]);self.schedule.sync(catalog,{},{'rss'})
        self.assertEqual(self.db.execute("SELECT enabled FROM runtime_schedule WHERE id='1'").fetchone()[0],0)
    def test_validator_rejects_unknown_source_invalid_ranges_and_boolean_numbers(self):
        for p in [{'sources':{'missing':{'enabled':True}}},{'sources':{'1':{'interval':True}}},{'sources':{'1':{'interval':59}}},
                  {'ai':{'concurrency':3}},{'ai':{'model':'arbitrary'}},{'role':'owner'},{'sources':{'1':{'enabled':1}}}]:
            with self.subTest(p=p),self.assertRaises(ValueError):validate_patch(p,{'1'})
    def test_actual_probe_failure_has_failed_receipt(self):
        f={'id':'probe','lastModifyingUser':{'permissionId':'owner'},'description':json.dumps({'format':'argus-control-command-v1','action':'test_ai','revision':0})}
        probe=Mock(side_effect=RuntimeError('private upstream error'))
        self.control.process(f,{'owner'},{'1'},probe);probe.assert_called_once()
        self.assertEqual(self.db.execute('SELECT status,detail FROM runtime_control_audit').fetchone()[:],('failed','provider_unavailable'))
    def test_model_requires_verified_provider_inventory(self):
        self.command({'ai':{'model':'jev-1.13.0'}})
        self.assertEqual(self.control.current(),(0,{}))
        f={'id':'probe','lastModifyingUser':{'permissionId':'owner'},'description':json.dumps({'format':'argus-control-command-v1','action':'test_ai','revision':0})}
        self.control.process(f,{'owner'},{'1'},lambda:['jev-1.13.0'])
        self.command({'ai':{'model':'jev-1.13.0'}},id='model')
        self.assertEqual(self.control.current()[1]['ai']['model'],'jev-1.13.0')

    def test_source_priority_changes_actual_admission_order(self):
        self.command({'sources':{'2':{'priority':10}}})
        self.schedule.sync(apply_catalog(self.catalog,self.control.current()[1]),{},{'rss'})
        self.assertEqual(self.schedule.claim(1000,limit=1)[0]['id'],'2')

    def test_provider_names_and_aliases_are_preserved(self):
        client=Mock();client.return_value.call.return_value={'models':[{'id':'opaque','name':'jev-latest'},{'name':'jev-preview'},{'name':'jev-1.14.0'},{'name':'unknown'}]}
        with patch('typesafe_schedule.worker_class',return_value=(None,client)):
            self.assertEqual(Control.probe(),['jev-latest','jev-preview','jev-1.14.0'])

    def test_new_receipt_requires_publication_but_replay_does_not(self):
        self.control.unpublished=False
        self.command({'ai':{'interval':300}})
        self.assertTrue(self.control.unpublished)
        self.control.unpublished=False
        self.command({'ai':{'interval':300}})
        self.assertFalse(self.control.unpublished)

if __name__=='__main__':unittest.main()
