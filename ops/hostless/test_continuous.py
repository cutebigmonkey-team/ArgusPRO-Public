import copy
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

OPS=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(OPS/x) for x in ('hostless','collect','drive','local-storage')]
from continuous import Loop,Limits,Child,FencedStore
from schedule_store import ScheduleStore,MAX_RESULT,MAX_SPOOL,iso
from worker import Pending,assemble
from indexed_journal import IndexedFeedJournal
from test_publish import MemoryStore
from publish import load_snapshot
from check_metrics import summarize


class Session:
    def __init__(self,db):self.db=db;self.owner=threading.get_ident()
    def execute(self,sql,args=()):
        if threading.get_ident()!=self.owner:raise AssertionError('delivery touched supervisor SQL')
        return self.db.execute(sql.replace('%s','?'),args)
    def transaction(self):return self.db


class Job:
    def __init__(self,sources,*,ready=True):
        self.sources=sources;self.ready=ready;self.staged=False;self.closed=False
        self.proc=SimpleNamespace(returncode=0)
    def poll(self,schedule):
        if not self.ready:return False
        if not self.staged:
            for source in self.sources:
                key=source['platform']+':'+source['id'];now=time.time()
                post={'canonical_key':key,'platform':source['platform'],'post_url':source['url']+'/article',
                    'text':'real normalization fixture','published_at':None,'observed_at':iso(now),
                    'source_id':source['id'],'source_url':source['url'],'media':[]}
                row={'source_id':source['id'],'url':source['url'],'platform':source['platform'],
                    'status':'ok','started_at':iso(now-1),'checked_at':iso(now),'seconds':1,
                    'canonical_keys':[key],'new_keys':[],'attempts':[],'rejections':[]}
                schedule.stage({'token':source['_lease_token'],'posts':[post],'record':row,'state_update':None},now)
            self.staged=True
        return True
    def stop(self):self.ready=True
    def close(self):self.closed=True


class ContinuousTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'state.db'
        self.db=sqlite3.connect(self.path);self.db.row_factory=sqlite3.Row
        self.addCleanup(self.db.close)
        self.schedule=ScheduleStore(self.db,sqlite=True)
        self.catalog={'sources':[{'id':str(i),'url':'https://fixture.example/'+str(i),
            'platform':'rss' if i%2==0 else 'facebook','origin':'general'} for i in range(12)]}
        self.schedule.sync(self.catalog,{}, {'rss','facebook'})
        self.store=MemoryStore();self.store.json('sources.json',self.catalog)
        self.feed=IndexedFeedJournal(self.store);self.addCleanup(lambda:self.feed.close())
        session=Session(self.db)
        self.journal=SimpleNamespace(db=SimpleNamespace(connection=session),check_session=lambda:None)
        self.pending=Pending(self.journal,self.schedule,self.feed)
        self.jobs=[];self.loops=[];self.addCleanup(self.close_loops)
    def close_loops(self):
        for loop in self.loops:loop.close()
    def stage(self,size):
        with tempfile.TemporaryDirectory() as root:
            batch=assemble(self.schedule,Path(root),max_bytes=size)
            if batch:self.pending.stage(*batch)
    def loop(self,deliver=None):
        def start(sources):
            job=Job(sources);self.jobs.append(job);return job
        loop=Loop(schedule=self.schedule,pending=self.pending,start=start,
            deliver=deliver or (lambda item:self.pending.publish(self.store,item)),
            assemble=self.stage,guard=lambda:None,emit=lambda *args,**kwargs:None,
            limits=Limits(maintenance_seconds=3600))
        self.loops.append(loop);return loop
    def finish(self,loop):
        loop.stop();deadline=time.monotonic()+5
        while loop.tick():
            if time.monotonic()>deadline:self.fail('bounded shutdown did not finish')
            time.sleep(.005)

    def test_collects_and_durably_stages_while_delivery_is_blocked(self):
        started=threading.Event();release=threading.Event()
        def deliver(item):
            started.set()
            if not release.wait(3):raise TimeoutError('fixture')
            self.pending.publish(self.store,item)
        loop=self.loop(deliver)
        try:
            loop.tick();initial={s['id'] for job in self.jobs for s in job.sources}
            loop.tick();self.assertTrue(started.wait(1))
            loop.tick()
            self.assertGreater(len(self.jobs),2)
            self.assertTrue(initial < {s['id'] for job in self.jobs for s in job.sources})
            self.assertGreater(len(self.schedule.pending()),len(initial))
            self.assertIsNotNone(self.pending.prepare())
            self.assertEqual(self.schedule.execute('SELECT MAX(last_completed_at) AS n FROM runtime_schedule').fetchone()['n'],0)
        finally:release.set();self.finish(loop)
        self.assertGreater(len(load_snapshot(self.store)['items']),0)

    def test_failed_drive_delivery_retains_pending_without_checkpoint(self):
        loop=self.loop(lambda item:(_ for _ in ()).throw(TimeoutError('Drive timeout')))
        loop.tick();loop.tick();deadline=time.monotonic()+2
        while not loop.future.done() and time.monotonic()<deadline:time.sleep(.005)
        loop.stop();loop.tick()
        self.assertIsNotNone(self.pending.prepare())
        self.assertGreater(len(self.schedule.pending()),0)
        self.assertIsNone(self.store.read_json('collection-cursors.json'))
        self.assertGreater(loop.retry_at,0)

    def test_historical_pending_survives_restart_and_replay_ack_is_atomic(self):
        source=self.schedule.claim(time.time(),limit=1)[0]
        job=Job([source]);job.poll(self.schedule);self.stage(2*1024**2)
        item=self.pending.prepare();self.pending.publish(self.store,item)
        # Simulate process death after Drive HEAD and cursor readback, before SQL ack.
        self.assertEqual(len(self.schedule.pending()),1)
        self.feed.close();self.feed=IndexedFeedJournal(self.store)
        self.pending.feed=self.feed
        self.pending.recover(self.store)
        self.assertEqual(self.schedule.pending(),[])
        self.assertEqual(len(load_snapshot(self.store)['items']),1)
        self.assertIsNone(self.pending.prepare())

    def test_lost_cursor_head_ack_keeps_sql_pending_and_recovers_once(self):
        source=self.schedule.claim(time.time(),limit=1)[0];Job([source]).poll(self.schedule)
        self.stage(1024**2);item=self.pending.prepare();write=self.store.json;lost=[False]
        def lose(name,value,mutable=False):
            answer=write(name,value,mutable)
            if name=='live-cursors.json' and not lost[0]:lost[0]=True;raise TimeoutError('lost cursor ack')
            return answer
        with patch.object(self.store,'json',side_effect=lose):
            with self.assertRaises(TimeoutError):self.pending.publish(self.store,item)
        self.assertIsNotNone(self.pending.prepare())
        self.assertEqual(len(self.schedule.pending()),1)
        self.pending.recover(self.store)
        self.assertEqual(len(load_snapshot(self.store)['items']),1)
        self.assertEqual(self.schedule.pending(),[])

    def test_source_child_receives_only_its_cursor_rows(self):
        state={'schema':'fixture','sources':{},'incremental_polling':{
            str(i):{'url':'https://fixture.example/'+str(i),'padding':'x'*1000} for i in range(1000)}}
        source={**self.catalog['sources'][0],'_lease_token':'fixture'}
        with patch('continuous.subprocess.Popen',return_value=Mock()) as spawn:
            child=Child([source],state,{},lambda _: {},OPS)
            try:
                loaded=json.loads((child.root/'cursors.json').read_text())
                self.assertEqual(set(loaded['incremental_polling']),{source['id']})
                self.assertLess((child.root/'cursors.json').stat().st_size,4096)
                child.proc.poll.return_value=0
            finally:child.close()

    def test_backpressure_reserves_results_and_preserves_collected_payloads(self):
        chosen=self.schedule.claim(time.time(),limit=100)
        self.assertLessEqual(len(chosen)*MAX_RESULT,MAX_SPOOL)
        self.assertEqual(self.schedule.claim(time.time(),limit=1),[])
        Job(chosen).poll(self.schedule)
        self.assertEqual(len(self.schedule.pending()),6)
        original=self.schedule.pending()
        self.schedule.execute('UPDATE runtime_results SET bytes=?',(MAX_SPOOL//6,));self.db.commit()
        self.assertEqual(self.schedule.claim(time.time(),limit=1),[])
        self.assertEqual(self.schedule.pending(max_bytes=MAX_SPOOL),original)

    def test_finishing_one_lane_cannot_release_another_live_lease(self):
        first=self.schedule.claim(time.time(),limit=1,classes={'fast'})
        second=self.schedule.claim(time.time(),limit=1,classes={'browser'})
        self.schedule.release_sources(first)
        row=self.schedule.execute('SELECT lease_token FROM runtime_schedule WHERE id=?',(second[0]['id'],)).fetchone()
        self.assertEqual(row['lease_token'],second[0]['_lease_token'])

    def test_small_batches_rotate_every_platform_with_large_sibling_backlogs(self):
        platforms=('facebook','instagram','rss','youtube','telegram','threads','vk','ok','x','tiktok','rutube','mailru','whatsapp')
        self.schedule.sync({'sources':[{'id':p+str(i),'url':'https://fixture.example/'+p+str(i),'platform':p}
            for p in platforms for i in range(5)]},{},set(platforms))
        seen=set();now=time.time()
        for turn in range(len(platforms)):
            chosen=self.schedule.claim(now+turn*6,limit=1)
            self.assertEqual(len(chosen),1)
            seen.add(chosen[0]['platform']);self.schedule.release_sources(chosen)
        self.assertEqual(seen,set(platforms))

    def test_slow_feed_cannot_take_either_reserved_telegram_slot(self):
        sources=[{'id':name,'url':'https://fixture.example/'+name,'platform':platform}
                 for name,platform in [('tg-one','telegram'),('tg-two','telegram'),('slow','rss')]]
        self.schedule.sync({'sources':sources},{},{'rss','telegram'})
        loop=self.loop()
        def start(rows):
            job=Job(rows,ready=False);self.jobs.append(job);return job
        loop.start=start;now=time.time()
        with patch('continuous.time.time',return_value=now):loop.tick()
        with patch('continuous.time.time',return_value=now+2):loop.tick()
        self.assertEqual({job.sources[0]['id'] for job in loop.jobs.values()},{'tg-one','tg-two','slow'})

    def test_wrong_package_ack_never_deletes_source_results(self):
        source=self.schedule.claim(time.time(),limit=1)[0];Job([source]).poll(self.schedule)
        self.stage(1024**2);item=self.pending.prepare()
        self.db.commit()  # emulate the production autocommit durable pending boundary
        self.pending.publish(self.store,item)
        item['digest']='wrong'
        with self.assertRaisesRegex(ValueError,'pending_ack_conflict'):self.pending.acknowledge(item)
        self.assertEqual(len(self.schedule.pending()),1)
        self.assertIsNotNone(self.pending.prepare())

    def test_comment_sql_failure_does_not_stop_verified_post_delivery(self):
        from contextlib import nullcontext
        from comment_maintenance import CommentMaintenance
        comments=CommentMaintenance(SimpleNamespace(transaction=nullcontext),self.feed,Mock(),lambda:None)
        loop=self.loop();loop.maintenance=lambda:comments.step(self.catalog['sources']);loop.next_maintenance=0
        with patch('comments_store.Comments',side_effect=RuntimeError('comment SQL unavailable')):
            deadline=time.monotonic()+5
            while self.feed.index.count()==0:
                loop.tick()
                if time.monotonic()>deadline:self.fail('comment failure blocked post delivery')
                time.sleep(.005)
            self.finish(loop)
        self.assertEqual(comments.status,'degraded')
        self.assertGreater(self.feed.index.count(),0)

    def test_memory_pressure_stops_admission_but_not_delivery(self):
        source=self.schedule.claim(time.time(),limit=1)[0];Job([source]).poll(self.schedule)
        loop=self.loop();loop.can_admit=lambda:False
        loop.tick();self.assertIsNotNone(loop.future);self.assertEqual(loop.jobs,{})
        self.finish(loop)
        self.assertEqual(len(load_snapshot(self.store)['items']),1)

    def test_new_window_ledger_does_not_fabricate_24h_totals_or_rendering(self):
        source=self.schedule.claim(time.time(),limit=1)[0];Job([source]).poll(self.schedule)
        self.stage(1024**2);self.pending.recover(self.store)
        metrics=summarize(self.schedule.execute,time.time())
        self.assertFalse(metrics['24h']['complete_window'])
        row=metrics['5m']['platforms'][source['platform']]
        self.assertEqual(row['observed']['inserted'],1)
        self.assertIsNone(row['api_visible']);self.assertIsNone(row['client_rendered'])

    def test_writer_fence_blocks_put_and_head_after_session_loss(self):
        guard=Mock(side_effect=RuntimeError('lost writer session'))
        fenced=FencedStore(self.store,guard,threading.RLock())
        with self.assertRaises(RuntimeError):fenced.put('page.json',b'{}')
        with self.assertRaises(RuntimeError):fenced.json('live-feed.json',{},mutable=True)
        self.assertIsNone(self.store.read_json('live-feed.json'))

    def test_child_stop_kills_process_group_and_waits(self):
        child=Child.__new__(Child);child.proc=Mock(pid=12345)
        child.proc.poll.return_value=None
        with patch('continuous.os.killpg') as kill:
            child.stop()
        kill.assert_called_once();child.proc.wait.assert_called_once()

    def test_oversized_source_result_is_explicit_failure_without_blocking_siblings(self):
        import hashlib
        chosen=self.schedule.claim(time.time(),limit=2)
        Job([chosen[1]]).poll(self.schedule)
        child=Child.__new__(Child);child.proc=Mock();child.proc.poll.return_value=0
        child.root=Path(self.temp.name)/'child';(child.root/'result/spool').mkdir(parents=True)
        name=hashlib.sha256(chosen[0]['id'].encode()).hexdigest()
        child.owners={name:chosen[0]};child.emit=Mock()
        (child.root/'result/spool'/(name+'.json')).write_bytes(b'x'*(MAX_RESULT+1))
        self.assertTrue(child.poll(self.schedule))
        self.assertEqual(len(self.schedule.pending()),1)
        row=self.schedule.execute('SELECT * FROM runtime_schedule WHERE id=?',(chosen[0]['id'],)).fetchone()
        self.assertEqual(row['last_completed_at'],0)
        self.assertEqual(json.loads(row['health'])['collector_status'],'source_result_too_large')
        self.assertEqual(self.schedule.metrics(time.time())['dead_letters'],1)
        self.assertIsNone(child.emit.call_args.kwargs['rejected_publications'])


if __name__=='__main__':unittest.main()
