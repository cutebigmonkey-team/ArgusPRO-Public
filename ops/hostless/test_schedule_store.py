import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path[:0]=[str(Path(__file__).resolve().parent),str(Path(__file__).resolve().parents[1]/'collect')]
from schedule_store import ScheduleStore,iso


def source(i=0,platform='rss'):
    return {'id':str(i),'url':f'https://source{i}.example/feed','platform':platform}


def payload(s,when=1000,status='ok'):
    return {'token':s['_lease_token'],'posts':[],'state_update':None,
            'record':{'source_id':s['id'],'url':s['url'],'platform':s['platform'],
                      'status':status,'started_at':iso(when-1),'checked_at':iso(when),
                      'seconds':1,'canonical_keys':[],'new_keys':[],'attempts':[]}}


def acknowledge(store,items):
    report={'sources':[p['record'] for p in items],
            'lease_tokens':{p['record']['source_id']:p['token'] for p in items}}
    with store.transaction():store.acknowledge(report)


class DurableSchedule(unittest.TestCase):
    def test_repaired_timeout_retry_still_obeys_shared_circuit_and_pending_result(self):
        requested={**source('z','telegram'),'collection_requested_at':iso(1000),
            'repaired_timeout_retry':{'failed_check_at':iso(900),'requested_at':iso(1000)}}
        health={'url':requested['url'],'last_checked_at':iso(900),
                'next_check_at':iso(5000),'collector_status':'timeout'}
        self.queue.sync({'sources':[requested]},{'incremental_polling':{'z':health}},{'telegram'})
        for i in range(3):self.queue.observe_circuit(requested,{'status':'timeout'},999+i)
        self.db.commit()
        self.assertEqual(self.queue.claim(1002),[])
        chosen=self.queue.claim(1200,limit=1)[0]
        self.queue.stage(payload(chosen,1201),1201)
        self.assertEqual(self.queue.claim(10000),[])

    def test_repaired_timeout_retry_is_exact_and_consumed_even_on_failure(self):
        requested={**source('z','telegram'),'collection_requested_at':iso(1000),
            'repaired_timeout_retry':{'failed_check_at':iso(900),'requested_at':iso(1000)}}
        health={'url':requested['url'],'last_checked_at':iso(900),
                'next_check_at':iso(5000),'collector_status':'timeout'}
        catalog={'sources':[requested]}
        self.queue.sync(catalog,{'incremental_polling':{'z':health}},{'telegram'})
        chosen=self.queue.claim(1001,limit=1)
        self.assertEqual([s['id'] for s in chosen],['z'])
        item=payload(chosen[0],1002,status='timeout')
        self.queue.stage(item,1002);acknowledge(self.queue,[item])
        self.assertEqual(self.queue.claim(1003),[])

    def test_repair_retry_never_overrides_server_stop_or_another_failure(self):
        for status,extra in [('http_429',{}),('login_required',{}),
                             ('timeout',{'host_cooldown_until':iso(5000)}),
                             ('timeout',{'last_checked_at':iso(950)})]:
            with self.subTest(status=status,extra=extra):
                self.queue.execute('DELETE FROM runtime_schedule');self.db.commit()
                requested={**source('z','telegram'),'repaired_timeout_retry':
                    {'failed_check_at':iso(900),'requested_at':iso(1000)}}
                health={'url':requested['url'],'last_checked_at':iso(900),
                        'next_check_at':iso(5000),'collector_status':status,**extra}
                self.queue.sync({'sources':[requested]},{'incremental_polling':{'z':health}},{'telegram'})
                self.assertEqual(self.queue.claim(1001),[])

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.path=Path(self.temp.name)/'queue.sqlite'
        self.db=self.open()
        self.queue=ScheduleStore(self.db,sqlite=True)
        self.queue.sync({'sources':[source(i) for i in range(20)]},{},{'rss'})

    def open(self):
        db=sqlite3.connect(self.path);db.row_factory=sqlite3.Row
        return db

    def tearDown(self):
        self.db.close();self.temp.cleanup()

    def test_priority_has_bounded_advantage_over_overdue_sources(self):
        self.queue.execute('DELETE FROM runtime_schedule');self.db.commit()
        high={**source('high','rss'),'priority':10}
        low={**source('low','rss'),'priority':-10}
        self.queue.sync({'sources':[high,low]}, {}, {'rss'})
        self.queue.execute('UPDATE runtime_schedule SET next_check_at=1000,last_reserved_at=1000 WHERE id=?',('low',))
        self.queue.execute('UPDATE runtime_schedule SET next_check_at=1700,last_reserved_at=1700 WHERE id=?',('high',))
        self.db.commit()
        self.assertEqual(self.queue.claim(1701,limit=1)[0]['id'],'low')

    def test_old_platform_gets_turn_despite_fresh_high_priority_platform(self):
        self.queue.execute('DELETE FROM runtime_schedule');self.db.commit()
        self.queue.sync({'sources':[{**source('rss','rss'),'priority':10},
                                    {**source('tg','telegram'),'priority':0}]}, {}, {'rss','telegram'})
        self.queue.execute('UPDATE runtime_schedule SET next_check_at=1000,last_reserved_at=1000 WHERE id=?',('tg',))
        self.queue.execute('UPDATE runtime_schedule SET next_check_at=1400,last_reserved_at=1400 WHERE id=?',('rss',))
        self.db.commit()
        self.assertEqual(self.queue.claim(1401,limit=1)[0]['platform'],'telegram')

    def test_two_connections_cannot_claim_same_source(self):
        first=self.queue.claim(1000,limit=10)
        other=self.open()
        try:
            second=ScheduleStore(other,sqlite=True).claim(1000,limit=20)
            self.assertFalse({s['id'] for s in first}&{s['id'] for s in second})
            self.assertEqual(len(first)+len(second),6)  # six worst-case 8 MiB reservations
        finally:other.close()

    def test_requested_retry_precedes_unvisited_backlog_then_is_consumed(self):
        requested={**source('z','telegram'),'collection_requested_at':iso(2000)}
        catalog={'sources':[source(i,'telegram') for i in range(20)]+[requested]}
        state={'incremental_polling':{'z':{'url':requested['url'],
            'last_checked_at':iso(1000),'next_check_at':iso(1900),'collector_status':'timeout'}}}
        self.queue.sync(catalog,state,{'telegram'})
        self.queue.execute('UPDATE runtime_schedule SET last_reserved_at=1000 WHERE id=?',('z',))
        self.db.commit()
        chosen=self.queue.claim(2001,limit=1)
        self.assertEqual([s['id'] for s in chosen],['z'])
        item=payload(chosen[0],2002)
        self.queue.stage(item,2002);acknowledge(self.queue,[item])
        self.queue.sync(catalog,{}, {'telegram'})
        self.assertNotEqual(self.queue.claim(2500,limit=1)[0]['id'],'z')

    def test_request_does_not_override_cooldown_circuit_or_pending_result(self):
        requested={**source('z','telegram'),'collection_requested_at':iso(1000)}
        state={'incremental_polling':{'z':{'url':requested['url'],
            'last_checked_at':iso(900),'next_check_at':iso(2000)}}}
        self.queue.sync({'sources':[requested]},state,{'telegram'})
        self.assertEqual(self.queue.claim(1500),[])
        for i in range(3):self.queue.observe_circuit(requested,{'status':'timeout'},1999+i)
        self.db.commit()
        self.assertEqual(self.queue.claim(2002),[])
        chosen=self.queue.claim(2200,limit=1)[0]
        self.queue.stage(payload(chosen,2201),2201)
        self.assertEqual(self.queue.claim(5000),[])

    def test_invalid_and_future_requests_do_not_block_normal_rotation(self):
        sources=[source('a'),{**source('z'),'collection_requested_at':'invalid'},
                 {**source('y'),'collection_requested_at':iso(5000)}]
        self.queue.sync({'sources':sources},{},{'rss'})
        self.assertEqual(self.queue.claim(1000,limit=1)[0]['id'],'a')

    def test_shared_retry_after_survives_restart_and_does_not_stop_other_origins(self):
        sources=[source(100,'instagram'),source(101,'instagram'),source(102,'rss')]
        self.queue.sync({'sources':sources},{},{'instagram','rss'})
        chosen=self.queue.claim(1000,limit=1)[0]
        self.assertEqual(chosen['platform'],'instagram')
        item=payload(chosen,status='http_429');item['record']['retry_after']=iso(1600)
        self.queue.stage(item,1001);acknowledge(self.queue,[item])
        other=self.open()
        try:
            queue=ScheduleStore(other,sqlite=True)
            self.assertEqual([s['platform'] for s in queue.claim(1200)],['rss'])
            self.assertTrue(any(s['platform']=='instagram' for s in queue.claim(1601)))
        finally:other.close()

    def test_three_transient_failures_open_circuit_then_one_probe(self):
        self.queue.sync({'sources':[source(i,'youtube') for i in range(20)]},{},{'youtube'})
        for i in range(3):
            with self.queue.transaction():self.queue.observe_circuit(source(i,'youtube'),{'status':'timeout'},1000+i)
        self.assertEqual(self.queue.claim(1003),[])
        self.assertEqual(len(self.queue.claim(1100)),1)

    def test_bad_normalization_never_poison_other_durable_results(self):
        good,bad=self.queue.claim(1000,limit=2)
        first=payload(good);self.queue.stage(first,1001)
        second=payload(bad);second['record']['canonical_keys']=['missing']
        with self.assertRaisesRegex(ValueError,'manifest_mismatch'):self.queue.stage(second,1001)
        self.queue.quarantine(bad,'invalid_result',1001)
        self.assertEqual(len(self.queue.pending()),1)
        self.assertEqual(self.queue.metrics(1002)['dead_letters'],1)
        acknowledge(self.queue,[first])
        self.assertEqual(self.queue.metrics(1002)['pending_results'],0)

    def test_completed_results_survive_restart_and_lease_expiry(self):
        s=self.queue.claim(1000,limit=1,lease_seconds=2)[0]
        p=payload(s);self.queue.stage(p,1001)
        self.db.close();self.db=self.open();self.queue=ScheduleStore(self.db,sqlite=True)
        self.queue.release_abandoned();self.db.commit()
        self.assertEqual(self.queue.pending(),[p])
        self.assertNotIn(s['id'],{s['id'] for s in self.queue.claim(2000,limit=32)})
        acknowledge(self.queue,[p]);self.assertEqual(self.queue.pending(),[])
        seen=set()
        for _ in range(20):
            chosen=self.queue.claim(2000,limit=1)
            seen.update(row['id'] for row in chosen)
            self.queue.release_sources(chosen)
        self.assertIn(s['id'],seen)

    def test_late_result_cannot_replace_new_lease(self):
        old=self.queue.claim(1000,limit=1,lease_seconds=1)[0]
        self.queue.claim(1002,limit=32)
        with self.assertRaisesRegex(ValueError,'expired_source_lease'):self.queue.stage(payload(old),1003)

    def test_failed_delivery_does_not_advance_health_or_duplicate_result(self):
        s=self.queue.claim(1000,limit=1)[0];p=payload(s)
        self.queue.stage(p,1001);self.queue.stage(p,1002)
        self.assertEqual(len(self.queue.pending()),1)
        row=self.queue.execute('SELECT * FROM runtime_schedule WHERE id=?',(s['id'],)).fetchone()
        self.assertEqual(row['last_completed_at'],0)
        self.assertEqual(json.loads(row['health']),{})
        with self.assertRaises(RuntimeError):
            with self.queue.transaction():
                self.queue.acknowledge({'sources':[p['record']],'lease_tokens':{s['id']:p['token']}})
                raise RuntimeError('database commit interrupted')
        self.assertEqual(len(self.queue.pending()),1)
        acknowledge(self.queue,[p]);self.assertEqual(len(self.queue.pending()),0)

    def test_retry_after_not_shortened_and_disabled_not_admitted(self):
        s=self.queue.claim(1000,limit=1)[0];p=payload(s,status='http_429')
        p['record']['retry_after']=iso(90000)
        self.queue.stage(p,1001);acknowledge(self.queue,[p])
        self.assertGreaterEqual(self.queue.execute('SELECT next_check_at FROM runtime_schedule WHERE id=?',(s['id'],)).fetchone()[0],90000)
        self.queue.sync({'sources':[{**source(99),'enabled':False}]},{},{'rss'})
        self.assertEqual(self.queue.claim(100000),[])

    def test_never_checked_sources_not_starved_by_frequent_successes(self):
        seen=set()
        for tick in range(4):
            selected=self.queue.claim(1000+tick*301,limit=5)
            seen.update(s['id'] for s in selected)
            items=[payload(s,1000+tick*301) for s in selected]
            for p in items:self.queue.stage(p,1000+tick*301)
            acknowledge(self.queue,items)
        self.assertEqual(len(seen),20)

    def test_serial_platform_does_not_fill_batch_with_unstartable_jobs(self):
        catalog={'sources':[source('a'+str(i),'instagram') for i in range(30)]+[
            {**source('z'+str(i),'rss'),'url':f'https://host{i}.example/feed'} for i in range(20)]}
        self.queue.sync(catalog,{}, {'instagram','rss'})
        selected=self.queue.claim(1000,limit=32,classes={'fast'})
        self.assertLessEqual(sum(s['platform']=='instagram' for s in selected),7)  # 100s / (10s estimate + actual 3s pacing)
        self.assertGreater(sum(s['platform']=='rss' for s in selected),1)

    def test_changed_url_cannot_inherit_pending_result_health(self):
        s=self.queue.claim(1000,limit=1)[0];p=payload(s)
        self.queue.stage(p,1001)
        changed={**s,'url':'https://new.example/feed'}
        self.queue.sync({'sources':[changed]},{},{'rss'})
        acknowledge(self.queue,[p])
        row=self.queue.execute('SELECT * FROM runtime_schedule WHERE id=?',(s['id'],)).fetchone()
        self.assertEqual(json.loads(row['health']),{})
        self.assertEqual(row['next_check_at'],0)

    def test_recent_publisher_keeps_a_live_turn_without_starving_backlog(self):
        live=source('z','instagram')
        catalog={'sources':[source('a'+str(i),'instagram') for i in range(10)]+[live,source('rss','rss')]}
        health={'url':live['url'],'last_checked_at':iso(900),'last_success_at':iso(900),
                'last_post_published_at':iso(890),'collector_status':'ok','next_check_at':iso(1000)}
        self.queue.sync(catalog,{'incremental_polling':{'z':health}},{'instagram','rss'})
        self.queue.execute('UPDATE runtime_schedule SET last_reserved_at=900 WHERE id=?',('z',))
        self.db.commit()
        selected=self.queue.claim(1000,limit=4)
        self.assertIn('z',{s['id'] for s in selected})
        self.assertIn('rss',{s['id'] for s in selected})
        self.assertEqual(sum(s['id'].startswith('a') for s in selected),2)

    def test_overdue_source_gets_turn_when_live_peer_exhausts_platform_budget(self):
        live=source('z','instagram');old=source('old','instagram')
        health={'url':live['url'],'last_checked_at':iso(1900),'last_success_at':iso(1900),
                'last_post_published_at':iso(1900),'collector_status':'ok','next_check_at':iso(1990)}
        self.queue.sync({'sources':[old,live]}, {'incremental_polling':{'z':health}}, {'instagram'})
        self.queue.execute('UPDATE runtime_schedule SET latency=60')
        self.queue.execute('UPDATE runtime_schedule SET last_reserved_at=1900 WHERE id=?',('z',))
        self.db.commit()
        self.assertIn('old',{s['id'] for s in self.queue.claim(2000,limit=64)})

    def test_aged_turn_does_not_starve_live_news_or_discard_history_after_restart(self):
        live=source('z','instagram');old=source('old','instagram')
        health={'url':live['url'],'last_checked_at':iso(1900),'last_success_at':iso(1900),
                'last_post_published_at':iso(1900),'collector_status':'ok','next_check_at':iso(1990)}
        self.queue.sync({'sources':[old,live]}, {'incremental_polling':{'z':health,
                        'old':{'url':old['url'],'remaining_ranges':7,'next_check_at':iso(1000)}}}, {'instagram'})
        self.queue.execute('UPDATE runtime_schedule SET latency=60')
        self.queue.execute('UPDATE runtime_schedule SET last_reserved_at=1900 WHERE id=?',('z',))
        self.db.commit()
        first=self.queue.claim(2000,limit=64)
        self.assertEqual([s['id'] for s in first],['old'])
        # A restart releases the old lease under the existing writer lock;
        # the reservation timestamp, pending data and source health survive.
        self.queue.release_abandoned();self.db.commit()
        restarted=ScheduleStore(self.db,sqlite=True)
        self.assertEqual([s['id'] for s in restarted.claim(2001,limit=64)],['z'])
        row=self.queue.execute('SELECT health FROM runtime_schedule WHERE id=?',('z',)).fetchone()
        self.assertEqual(json.loads(row['health'])['last_post_published_at'],iso(1900))

        old_row=self.queue.execute('SELECT health FROM runtime_schedule WHERE id=?',('old',)).fetchone()
        self.assertEqual(json.loads(old_row['health'])['remaining_ranges'],7)

    def test_live_preference_never_bypasses_retry_after(self):
        live=source('z','instagram')
        health={'url':live['url'],'last_checked_at':iso(900),'last_success_at':iso(900),
                'last_post_published_at':iso(890),'collector_status':'ok','next_check_at':iso(1600)}
        self.queue.sync({'sources':[source('a','instagram'),live]},
                        {'incremental_polling':{'z':health}},{'instagram'})
        self.assertEqual([s['id'] for s in self.queue.claim(1000,limit=4)],['a'])

    def test_single_slot_keeps_oldest_source_rotation(self):
        live=source('z','instagram')
        health={'url':live['url'],'last_checked_at':iso(900),'last_success_at':iso(900),
                'last_post_published_at':iso(890),'collector_status':'ok','next_check_at':iso(1000)}
        self.queue.sync({'sources':[source('a','instagram'),live]},
                        {'incremental_polling':{'z':health}},{'instagram'})
        self.assertEqual([s['id'] for s in self.queue.claim(1000,limit=1)],['a'])

    def test_registry_reconciliation_prefetches_bounded_chunks_and_preserves_leases(self):
        sources=[source(str(i),'rss') for i in range(1000)]
        self.queue.sync({'sources':sources},{},{'rss'})
        leased=self.queue.claim(1000,limit=1)[0]
        statements=[];self.db.set_trace_callback(statements.append)
        self.queue.sync({'sources':sources},{},{'rss'})
        self.db.set_trace_callback(None)
        selects=[q for q in statements if q.startswith('SELECT')]
        self.assertEqual(len(selects),16)  # two prefetches for each 128-source chunk
        row=self.queue.execute('SELECT lease_token FROM runtime_schedule WHERE id=?',(leased['id'],)).fetchone()
        self.assertEqual(row['lease_token'],leased['_lease_token'])


if __name__=='__main__':unittest.main()
