"""Hostless integration: one fenced writer, bounded collectors, disposable index."""
import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from continuous import Child,FencedStore,Limits,Loop
from indexed_journal import IndexedFeedJournal
from schedule_store import ScheduleStore
from serialized_db import Connection
from control import Control,apply_catalog
from comment_maintenance import CommentMaintenance
from startup import Startup,StartupCheckFailed,error_type,known_code


def open_journal(dsn,schema,*,emit=lambda *args,**kwargs:None,stop=None):
    # Keep the pinned session-lock implementation; never reconnect or steal locks.
    from session_store import open_session
    return open_session(dsn,schema,emit,stop.wait if stop is not None else time.sleep)


def run(w):
    phases=Startup(w.emit)
    with phases.phase('cloud_environment'):
        from runtime_policy import require_cloud
        require_cloud()
    with phases.phase('configuration'):
        mode=os.getenv('ARGUS_RUNTIME_MODE','preflight')
        if mode not in ('preflight','collect'):raise ValueError('invalid_runtime_mode')
        required=('ARGUS_DRIVE_OAUTH','ARGUS_DRIVE_FOLDER_ID','ARGUS_JSONBIN_KEY','ARGUS_JSONBIN_BIN_ID','DATABASE_URL')
        if any(not os.getenv(key) for key in required):raise StartupCheckFailed('configuration_missing')
        limits=Limits.environment()
    if mode=='collect':
        with phases.phase('archive_writer_cutover'):
            if os.getenv('ARGUS_ARCHIVE_WRITER_OWNER')!='hostless':
                raise StartupCheckFailed('archive_writer_cutover_not_confirmed')
    os.environ['ARGUS_ANONYMOUS_ONLY']='1'
    lock=threading.RLock()
    store=None;journal=None;feed=None;loop=None;comments=None
    def guard():
        with lock:journal.check_session()
    try:
        with phases.phase('archive_access'):
            store=w.from_environment();store.assert_private()
        with phases.phase('registry_load'):
            registry=w.fetch_registry(os.environ['ARGUS_JSONBIN_BIN_ID'],os.environ['ARGUS_JSONBIN_KEY'],os.getenv('ARGUS_JSONBIN_KEY_TYPE','access'))
        # Preflight is genuinely read-only: the journal constructor itself does DDL.
        if mode=='preflight':
            with phases.phase('preflight_archive_read'):
                from publish import load_base_snapshot
                count=0
                def consume(row):
                    nonlocal count
                    count+=1
                load_base_snapshot(store,item_consumer=consume)
            w.emit('startup_completed',mode='preflight',database_session='not_acquired')
            w.emit('preflight_ok',mode='preflight',base_posts=count,tls='verify-full');return
        with phases.phase('database_session'):
            schema='argus_drive_'+hashlib.sha256(store.root.encode()).hexdigest()[:24]
            journal=open_journal(os.getenv('ARGUS_DATABASE_SESSION_URL') or os.environ['DATABASE_URL'],schema,
                                 emit=w.emit,stop=w.STOP_REQUESTED)
            guard()
        fenced=FencedStore(store,guard,lock)
        with phases.phase('archive_index_initialization'):
            # Constructor reads verified Drive data into disposable local files only.
            feed=IndexedFeedJournal(fenced,observe=w.memory_phase,restore_cursors=False)
        with phases.phase('cursor_restoration'):
            feed.restore_cursors()
        with phases.phase('journal_verification'):
            guard();feed.verify_head();feed.cursor.verify()
        with phases.phase('source_configuration'):
            from runtime_catalog import load_private_catalog
            merged=load_private_catalog(store,w.with_submissions)
            prior_monitor=store.read_json('monitor.json') or {}
        db=Connection(journal.db.connection,lock)
        # Roll back scheduler/comments initialization if ANY later startup phase
        # fails. No archive compaction, command processing or pending recovery here.
        with phases.phase('runtime_state_initialization'), db.transaction():
            guard()
            with phases.phase('scheduler_initialization'):
                schedule=ScheduleStore(db)
            pending=w.Pending(journal,schedule,feed)
            prior_delivery=prior_monitor.get('runtime',{}).get('last_successful_delivery')
            if prior_delivery:
                try:
                    if w.datetime.fromisoformat(prior_delivery.replace('Z','+00:00'))<=w.datetime.now(w.timezone.utc):
                        pending.last_delivery=prior_delivery
                except (ValueError,TypeError,AttributeError):pass
            comments=CommentMaintenance(db,feed,w.emit,guard)
            control=Control(schedule)
            _,settings=control.current()
            catalog=apply_catalog(merged,settings)
            state=feed.verified_state
            enabled={row['platform'] for row in registry['collectors'] if row['enabled']}
            with phases.phase('source_registry_reconciliation'):
                schedule.sync(catalog,state,enabled)
            feed.catalog=catalog
            last_report={'run_id':'startup','updated_at':w.datetime.now(w.timezone.utc).isoformat(),
                         'sources':[],'new_to_archive':0,'incremental':True}
            shared={'state':state,'catalog':catalog,'registry':registry,'refresh':False}

            def start(sources):
                return Child(sources,shared['state'],shared['registry'],w.collector_environment,w.OPS,emit=w.emit)

            def assemble(max_bytes):
                with tempfile.TemporaryDirectory(prefix='argus-delivery-package-') as temporary:
                    batch=w.assemble(schedule,Path(temporary),max_bytes=max_bytes)
                    if batch:
                        package,report=batch;pending.stage(package,report)
                        w.emit('package_staged',run_id=report['run_id'],selected=len(report['sources']))

            def delivery(item):
                pending.publish(fenced,item)
                return item['report']

            def on_delivery():
                nonlocal last_report
                shared['state']=copy.deepcopy(feed.verified_state or shared['state'])
                last_report=copy.deepcopy(loop.item['report'])
                comments.dirty(row['id'] for row in feed.changed)
                if feed.reconciled:
                    comments.rescan();feed.reconciled=False

            def maintenance():
                nonlocal last_report
                guard()
                if getattr(feed,'invalid',False):
                    # Pending retry owns reconciliation; maintenance must not publish stale state.
                    return
                feed.maintenance(last_report)
                if 'runtime' in shared:
                    feed.monitor['runtime']=copy.deepcopy(shared['runtime'])
                    fenced.json('monitor.json',feed.monitor,mutable=True)
                try:
                    remote=load_private_catalog(fenced,w.with_submissions)
                    fresh_registry=w.fetch_registry(os.environ['ARGUS_JSONBIN_BIN_ID'],os.environ['ARGUS_JSONBIN_KEY'],os.getenv('ARGUS_JSONBIN_KEY_TYPE','access'))
                    merged=remote
                    control.sync(fenced,merged)
                    _,settings=control.current()
                    updated=apply_catalog(merged,settings)
                    if updated!=shared['catalog']:comments.rescan()
                    shared['catalog']=updated;shared['registry']=fresh_registry;feed.catalog=updated
                    shared['refresh']=True
                    control.publish(fenced,schedule,fresh_registry)
                except Exception as error:
                    guard();w.emit('control_sync_failed',error_type=type(error).__name__)
                comments.step(shared['catalog']['sources'])
                if comments.comments is not None and comments.status!='degraded':
                    try:
                        guard();comments.comments.tick(w.emit);comments.comments.publish(fenced)
                    except Exception as error:
                        guard();w.emit('comments_cycle_failed',error_type=error_type(error))
                return True

            with phases.phase('pipeline_initialization'):
                loop=Loop(schedule=schedule,pending=pending,start=start,deliver=delivery,
                          assemble=assemble,guard=guard,emit=w.emit,maintenance=maintenance,
                          on_delivery=on_delivery,limits=limits,sql_lock=lock,
                          can_admit=__import__('memory').can_admit,
                          on_cleanup=lambda:w.emit('memory_reclaimed',phase='after_verified_delivery',**w.release_memory()))
        w.emit('startup_completed',mode='collect',pipeline='continuous')
        w.emit('preflight_ok',mode='collect',tls='verify-full',archive_posts=feed.index.count(),
               source_count=len(catalog['sources']),pipeline='continuous')
        # Recovery is a durable runtime operation, not an initialization check.
        # Failure leaves its existing retry package intact and does not bypass fencing.
        w.emit('runtime_recovery_started')
        try:
            from pending_recovery import recover
            if not recover(w,fenced,journal,feed):
                w.emit('worker_shutdown',pending_preserved=True);return
            schedule.release_abandoned()
            shared['state']=copy.deepcopy(feed.verified_state)
            if shared['state']!=state:
                schedule.sync(catalog,shared['state'],enabled)
            comments.dirty(row['id'] for row in feed.changed)
        except Exception as error:
            w.emit('runtime_recovery_failed',error_code=known_code(error) or 'pending_recovery_failed',
                   error_type=error_type(error))
            raise
        w.emit('runtime_recovery_completed')
        next_metrics=0
        while True:
            if w.STOP_REQUESTED.is_set():loop.stop()
            if next_metrics==0:
                with phases.phase('pipeline_admission'):
                    admitted=loop.tick()
                if not admitted:break
            elif not loop.tick():break
            with lock:
                if shared['refresh']:
                    schedule.sync(shared['catalog'],shared['state'],
                                  {r['platform'] for r in shared['registry']['collectors'] if r['enabled']})
                    shared['refresh']=False
                now=time.monotonic()
                if now>=next_metrics:
                    from check_metrics import summarize
                    package_row=schedule.execute('SELECT report FROM runtime_pending WHERE slot=1').fetchone()
                    package_age=None
                    if package_row:
                        try:
                            staged=w.datetime.fromisoformat(json.loads(package_row['report'])['pending_staged_at'])
                            package_age=max(0,(w.datetime.now(w.timezone.utc)-staged).total_seconds())
                        except (KeyError,ValueError,TypeError):pass
                    metrics={**schedule.metrics(time.time()),**w.measurement(),
                        'pending_delivery_packages':schedule.execute('SELECT COUNT(*) AS n FROM runtime_pending').fetchone()['n'],
                        'pending_delivery_package_age_seconds':package_age,
                        'comments_synchronization_status':comments.status,
                        'at':w.datetime.now(w.timezone.utc).isoformat(),
                        'worker_started_at':w.STARTED_AT,'deployment_sha':w.release_sha(),
                        'last_successful_delivery':pending.last_delivery,
                        'archive_write_failures_this_session':pending.write_failures,
                        'delivery_active':loop.kind=='delivery','collector_processes':len(loop.jobs),
                        'queue_capacity_bytes':48*1024**2,'delivery_batch_target_bytes':limits.delivery_bytes,
                        'windows':summarize(schedule.execute,time.time()),'provider_restarts':None}
                    shared['runtime']=metrics
                    w.emit('continuous_status',**metrics)
                    next_metrics=now+30
            time.sleep(.1 if loop.jobs or loop.future is not None else 1)
        w.emit('worker_shutdown',pending_preserved=True)
    finally:
        resources=[('pipeline',loop),('comments',comments),('index',feed),
                   ('drive_client',getattr(store,'service',None)),('writer_session',journal)]
        for name,resource in resources:
            if resource is None:continue
            close=getattr(resource,'close',None)
            if close is not None:
                try:close()
                except Exception as error:w.emit('resource_cleanup_failed',component=name,error_type=error_type(error))
