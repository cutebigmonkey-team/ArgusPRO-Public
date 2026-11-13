"""Bounded, overlapping source subprocesses and one archive delivery lane.

The coordinator is the sole SQL owner. The delivery executor never shares SQL
transactions. One delivery/maintenance task and at most five collector processes
can exist; there is no unbounded executor submission queue.
"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import random
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from schedule_store import MAX_RESULT,MAX_SPOOL
from retry_after import retry_after


@dataclass(frozen=True)
class Limits:
    fast_sources:int=1
    telegram_sources:int=2
    other_sources:int=1
    delivery_bytes:int=2*1024**2
    maintenance_seconds:int=60
    shutdown_seconds:int=30

    @classmethod
    def environment(cls):
        values={}
        for field,(low,high) in {'fast_sources':(1,2),'telegram_sources':(1,2),'other_sources':(1,2),
                'delivery_bytes':(64*1024,8*1024**2),'maintenance_seconds':(30,3600),
                'shutdown_seconds':(5,120)}.items():
            raw=os.getenv('ARGUS_CONTINUOUS_'+field.upper())
            if raw is not None:
                value=int(raw)
                if not low<=value<=high:raise ValueError('invalid_continuous_limit_'+field)
                values[field]=value
        return cls(**values)


class Child:
    def __init__(self,sources,state,registry,environment,ops,*,clock=time.monotonic,emit=lambda *a,**k:None):
        self.sources=sources;self.clock=clock
        self.emit=emit
        self.directory=tempfile.TemporaryDirectory(prefix='argus-source-lane-')
        self.root=Path(self.directory.name);self.started=clock()
        ids={source['id'] for source in sources}
        from cursor_journal import CONTAINERS
        state={key:{sid:row for sid,row in value.items() if sid in ids} if key in CONTAINERS else value
               for key,value in state.items()}
        for name,value in [('sources',{'sources':sources,'live_lane':'all','live_order':[s['id'] for s in sources]}),
                           ('cursors',state),('registry',registry)]:
            (self.root/(name+'.json')).write_text(json.dumps(value))
        self.owners={hashlib.sha256(s['id'].encode()).hexdigest():s for s in sources}
        command=[sys.executable,str(ops/'collect/run.py'),'--incremental','--public-page',
            '--telegram-history','--stream-results','--sources',str(self.root/'sources.json'),
            '--cursors',str(self.root/'cursors.json'),'--registry',str(self.root/'registry.json'),
            '--output',str(self.root/'result'),'--sources-per-platform','0','--posts','30',
            '--window-hours','0','--concurrency',str(min(6,len(sources))),
            '--batch-seconds','105','--source-timeout','65']
        child_environment=environment('hostless-'+uuid.uuid4().hex)
        if child_environment.get('ARGUS_SOURCE_ADAPTERS')=='1':
            # Passive before-launch check; sources retain their native fallback.
            for tool,platform in [('pravaha','x'),('threads-cli','threads')]:
                if any(s['platform']==platform for s in sources):
                    from collector_preflight import unavailable_reason
                    reason=unavailable_reason(tool,child_environment)
                    if reason:emit('collector_adapter_unavailable',tool=tool,reason=reason,fallback_retained=True)
        try:
            self.proc=subprocess.Popen(command,env=child_environment,
                cwd=ops.parent,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
        except BaseException:self.directory.cleanup();raise

    def poll(self,schedule):
        for path in sorted((self.root/'result/spool').glob('*.json')):
            source=self.owners.get(path.stem)
            if source is None:raise ValueError('unexpected_spool_file')
            try:
                if path.stat().st_size>MAX_RESULT:raise ValueError('source_result_too_large')
                if schedule.spool_bytes()+path.stat().st_size>MAX_SPOOL:
                    schedule.renew_sources([source],time.time()+600)
                    return False
                payload=json.loads(path.read_bytes())
                if payload['record']['source_id']!=source['id']:raise ValueError('spool_source_mismatch')
                schedule.stage(payload,time.time())
            except (ValueError,KeyError,TypeError) as error:
                if str(error)=='result_spool_full':
                    schedule.renew_sources([source],time.time()+600)
                    return False
                category='source_result_too_large' if str(error)=='source_result_too_large' else 'invalid_source_result'
                schedule.quarantine(source,category,time.time())
                self.emit('source_result_quarantined',platform=source['platform'],category=category,
                          checkpoint_advanced=False,rejected_publications=None)
            path.unlink()  # Durable result or explicit failure ledger; no successful cursor on rejection.
        if self.proc.poll() is None and self.clock()-self.started>210:self.stop()
        return self.proc.poll() is not None

    def stop(self):
        if self.proc.poll() is None:
            try:os.killpg(self.proc.pid,signal.SIGKILL)
            except ProcessLookupError:pass
            self.proc.wait()
    def close(self):self.stop();self.directory.cleanup()


class Loop:
    def __init__(self,*,schedule,pending,start,deliver,assemble,guard,emit,
                 maintenance=lambda:None,on_delivery=lambda:None,limits=Limits(),
                 clock=time.monotonic,sql_lock=None,can_admit=lambda:True,on_cleanup=lambda:None):
        self.schedule=schedule;self.pending=pending;self.start=start;self.deliver=deliver
        self.assemble=assemble;self.guard=guard;self.emit=emit;self.maintenance=maintenance
        self.on_delivery=on_delivery;self.limits=limits;self.clock=clock
        self.lock=sql_lock or threading.RLock()
        self.executor=ThreadPoolExecutor(max_workers=1,thread_name_prefix='argus-drive')
        self.jobs={};self.future=None;self.item=None;self.kind=None
        self.failures=0;self.retry_at=0;self.next_maintenance=clock()+limits.maintenance_seconds
        self.stopping=False;self.stopped_at=None
        self.can_admit=can_admit
        self.on_cleanup=on_cleanup

    def stop(self):
        if not self.stopping:self.stopping=True;self.stopped_at=self.clock()

    def tick(self):
        with self.lock:return self._tick()

    def _tick(self):
        self.guard();now=self.clock()
        for lane,job in list(self.jobs.items()):
            if self.stopping and now-self.stopped_at>=self.limits.shutdown_seconds:job.stop()
            if job.poll(self.schedule):
                # Final atomic spool files may have appeared immediately before exit.
                job.poll(self.schedule)
                self.schedule.release_sources(job.sources)
                code=job.proc.returncode;job.close();del self.jobs[lane]
                if code:self.emit('collector_batch_interrupted',exit_code=code,lane=lane)
        if self.future is not None and self.future.done():
            delivered=False
            try:
                self.future.result()
            except Exception as error:
                self.failures+=1
                delay=max(retry_after(error),min(300,2**min(self.failures,8)))
                self.retry_at=now+delay+random.uniform(0,1)
                self.emit('delivery_retry' if self.kind=='delivery' else 'maintenance_retry',
                          error_type=type(error).__name__,retry_seconds=self.retry_at-now)
            else:
                if self.kind=='delivery':
                    self.pending.acknowledge(self.item)
                    self.on_delivery()
                    delivered=True
                self.failures=0;self.retry_at=0
            self.future=None;self.item=None;self.kind=None
            if delivered:self.on_cleanup()
        if self.future is None and now>=self.retry_at:
            self.item=self.pending.prepare()
            maintenance_due=self.item is None and not self.stopping and now>=self.next_maintenance
            if self.item is None and not self.stopping and not maintenance_due:
                self.assemble(self.limits.delivery_bytes)
                self.item=self.pending.prepare()
            if self.item is not None:
                self.kind='delivery';self.future=self.executor.submit(self.deliver,self.item)
            elif maintenance_due:
                self.kind='maintenance';self.next_maintenance=now+self.limits.maintenance_seconds
                self.future=self.executor.submit(self.maintenance)
        legacy_recovery=self.item is not None and len(self.item.get('blob',b''))>8*1024**2
        if not self.stopping and not legacy_recovery:
            # Fast lane always has a reserved process slot. A slow browser can
            # neither consume that slot nor wait on the same source semaphore.
            lanes=[('telegram-'+str(i),{'fast'},1,{'telegram'},()) for i in range(self.limits.telegram_sources)]
            lanes.extend(('fast-'+str(i),{'fast'},1,None,{'telegram'}) for i in range(self.limits.fast_sources))
            lanes.append(('other',{'regular','browser'},self.limits.other_sources,None,()))
            for lane,classes,limit,platforms,excluded in lanes:
                if lane in self.jobs:continue
                if not self.can_admit():break
                sources=self.schedule.claim(time.time(),limit=limit,classes=classes,
                                            platforms=platforms,exclude_platforms=excluded,pace=True)
                if sources:
                    try:self.jobs[lane]=self.start(sources)
                    except BaseException:
                        self.schedule.release_sources(sources);raise
                    self.emit('collection_started',lane=lane,selected=len(sources),
                              platforms=sorted({s['platform'] for s in sources}))
        return not self.stopping or bool(self.jobs) or self.future is not None

    def close(self):
        try:
            for job in self.jobs.values():
                try:
                    job.stop();job.poll(self.schedule);self.schedule.release_sources(job.sources)
                except Exception as error:
                    self.emit('collector_cleanup_failed',error_type=type(error).__name__,checkpoint_advanced=False)
                finally:job.close()
        finally:
            # Do not release writer ownership while an upload may still commit.
            self.executor.shutdown(wait=True,cancel_futures=True)


class FencedStore:
    def __init__(self,store,guard,lock):self.store=store;self.guard=guard;self.lock=lock
    def __getattr__(self,name):return getattr(self.store,name)
    def put(self,*args,**kwargs):
        with self.lock:self.guard()
        return self.store.put(*args,**kwargs)
    def json(self,*args,**kwargs):
        with self.lock:self.guard()
        return self.store.json(*args,**kwargs)
