"""Private cloud worker using the existing archive format and acknowledgement path.

Drive holds acknowledged cursors. PostgreSQL holds one pending package and a
session lock. No archive credential enters a collector subprocess or image.
"""
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
import uuid
import threading

OPS=Path(__file__).resolve().parents[1]
for directory in ('collect','drive','local-storage'):
    sys.path.insert(0,str(OPS/directory))

from planning import plan
from memory import release as release_memory
from memory import measurement
from drive_store import encoded, from_environment
from publish import load_snapshot
from publish_incremental import drain, deliver, PENDING
from registry import fetch_registry
from site_sources import with_submissions
from startup import Startup,known_code,error_type as safe_error_type,sqlstate as safe_sqlstate

MAX_BATCH_BYTES=32*1024**2
STARTED_AT=datetime.now(timezone.utc).isoformat()
STOP_REQUESTED=threading.Event()


def release_sha():
    path=OPS.parent/'release-sha'
    value=os.getenv('ARGUS_DEPLOYMENT_SHA') or (path.read_text().strip() if path.exists() else '')
    return value if len(value)==40 and all(c in '0123456789abcdef' for c in value) else None


def emit(event, **fields):
    print(json.dumps({'event':event,'at':datetime.now(timezone.utc).isoformat(),**fields}),flush=True)


def memory_phase(phase):
    emit('memory_phase',phase=phase,**measurement())


def collection_fingerprint():
    digest=hashlib.sha256()
    for path in sorted((OPS/'collect').glob('*.py')):
        if path.name.startswith('test_'):continue
        digest.update(path.name.encode()+b'\0'+path.read_bytes())
    return digest.hexdigest()


def runtime_fingerprint():
    digest=hashlib.sha256()
    for directory in ('hostless','drive','collect','local-storage'):
        for path in sorted((OPS/directory).glob('*.py')):
            if path.name.startswith(('test_','benchmark_')):continue
            digest.update(str(path.relative_to(OPS)).encode()+b'\0'+path.read_bytes())
    return digest.hexdigest()


def before_archive_readback():
    emit('memory_reclaimed',phase='before_readback',**release_memory())


def error_category(error):
    """Return only constant classifications; driver text can contain secrets."""
    known=known_code(error)
    if known:return known
    message=str(error).lower()
    if isinstance(error,ValueError) and message in {'source_result_too_large','invalid_source_posts',
        'spool_source_mismatch','source_post_manifest_mismatch','expired_source_lease','result_spool_full','pending_ack_conflict'}:return message
    for category,markers in (
        ('tls_certificate',('certificate verify failed','root certificate','certificate validation')),
        ('tls_hostname',('does not match host name','hostname mismatch')),
        ('tls_unavailable',('does not support ssl','ssl is not enabled')),
        ('authentication',('password authentication failed','no password supplied','authentication failed')),
        ('dns',('could not translate host name','name or service not known','name resolution')),
        ('refused',('connection refused',)),
        ('timeout',('timeout expired','timed out')),
        ('database_missing',('database does not exist','does not exist')),
        ('connection_closed',('server closed the connection','connection reset','eof detected')),
    ):
        if any(marker in message for marker in markers):return category
    return 'unclassified'


def collector_environment(run_id):
    allowed={'PATH','HOME','LANG','LD_LIBRARY_PATH','PLAYWRIGHT_BROWSERS_PATH','SSL_CERT_FILE'}
    env={k:v for k,v in os.environ.items() if k in allowed}
    # Forward only explicitly configured, noncredential reader switches. Do not
    # enable adapters implicitly or grant child processes archive access.
    flags={'ARGUS_SOURCE_ADAPTERS','ARGUS_FACEBOOK_GRAPH','ARGUS_REVIEWED_FACEBOOK_CLI',
           'ARGUS_FACEBOOK_X','ARGUS_PUBLIC_SOCIAL_ADAPTERS','ARGUS_SOCIAL_COLLECTION',
           'ARGUS_EMBED_COLLECTION','ARGUS_REMAINING_COLLECTION','ARGUS_CANDIDATE_FEEDS',
           'ARGUS_ENHANCED_COLLECTION'}
    for name in flags:
        if os.environ.get(name) in ('0','1'):env[name]=os.environ[name]
    if os.environ.get('ARGUS_FACEBOOK_BROWSER_ONLY') in ('true','false'):
        env['ARGUS_FACEBOOK_BROWSER_ONLY']=os.environ['ARGUS_FACEBOOK_BROWSER_ONLY']
    env.update(ARGUS_COLLECTOR_RUNTIME='hostless',ARGUS_ANONYMOUS_ONLY='1',ARGUS_RUN_ID=run_id,
               PYTHONUNBUFFERED='1',PYTHONDONTWRITEBYTECODE='1')
    return env


def merge_catalog(shipped, remote):
    # Existing private edits, including disabled sources, take precedence.
    rows=list(remote.get('sources',[]))
    ids={r['id'] for r in rows}; urls={r['url'] for r in rows}
    rows.extend(r for r in shipped['sources'] if r['id'] not in ids and r['url'] not in urls)
    return {**shipped,**remote,'sources':rows}


class Pending:
    def __init__(self,journal,schedule=None,feed=None):
        self.journal=journal
        self.schedule=schedule
        self.feed=feed
        self.write_failures=0
        self.last_delivery=None
        self.db=journal.db.connection
        self.db.execute('''CREATE TABLE IF NOT EXISTS runtime_pending
            (slot INTEGER PRIMARY KEY CHECK(slot=1), digest TEXT NOT NULL,
             package BYTEA NOT NULL, report TEXT NOT NULL)''')

    def stage(self,package,report):
        self.journal.check_session()
        blob=package.read_bytes()
        if not 0<len(blob)<=MAX_BATCH_BYTES: raise ValueError('batch_size_exceeded')
        report.setdefault('pending_staged_at',datetime.now(timezone.utc).isoformat())
        self.db.execute('INSERT INTO runtime_pending VALUES (1,%s,%s,%s)',
                        (hashlib.sha256(blob).hexdigest(),blob,encoded(report).decode()))

    def recover(self,store):
        item=self.prepare()
        if item is None:return False
        self.publish(store,item)
        return self.acknowledge(item)

    def prepare(self):
        self.journal.check_session()
        row=self.db.execute('SELECT * FROM runtime_pending WHERE slot=1').fetchone()
        if row is None:return None
        memory_phase('pending_loaded')
        blob=bytes(row['package'])
        if hashlib.sha256(blob).hexdigest()!=row['digest']:raise ValueError('pending_checksum')
        report=json.loads(row['report'])
        if 'archive_attempt_at' not in report:
            # Persist deterministic normalization time before any upload. Retries
            # reuse immutable page names instead of accumulating orphan versions.
            report['archive_attempt_at']=datetime.now(timezone.utc).isoformat()
            self.db.execute('UPDATE runtime_pending SET report=%s WHERE slot=1 AND digest=%s',
                            (encoded(report).decode(),row['digest']))
        return {'digest':row['digest'],'blob':blob,'report':report}

    def publish(self,store,item):
        # Delivery lane: no SQL access, no cursor/queue deletion. The supervisor
        # alone owns the PostgreSQL session and acknowledges on Future completion.
        blob=item['blob'];report=item['report']
        emit('archive_write_started',run_id=report['run_id'],selected=len(report['sources']))
        with tempfile.TemporaryDirectory(prefix='argus-recovery-') as temporary:
            package=Path(temporary)/'package.zip';package.write_bytes(blob)
            try:
                memory_phase('pending_publish_before')
                if self.feed:drain(store,package,report,journal=self.feed)
                else:drain(store,package,report,before_readback=before_archive_readback)
                memory_phase('pending_publish_after')
            except Exception:
                self.write_failures+=1
                emit('archive_write_failed',run_id=report['run_id'],failures=self.write_failures)
                raise
        emit('drive_delivery_verified',verified=True,delivery_kind='journal' if self.feed else 'snapshot')
        return report

    def acknowledge(self,item):
        report=item['report']
        # drain returns only after identity and checkpoint read-back succeeded.
        self.journal.check_session()
        with self.db.transaction():
            if self.schedule:
                self.schedule.acknowledge(report)
                from schedule_store import stamp
                for platform,rows in report.get('platform_insertions',{}).items():
                    for sid,archived_at in rows.items():
                        if archived_at:self.schedule.execute('''INSERT INTO runtime_inserted_posts VALUES(?,?,?)
                            ON CONFLICT(id) DO NOTHING''',(sid,platform,stamp(archived_at)))
            deleted=self.db.execute('DELETE FROM runtime_pending WHERE slot=1 AND digest=%s',(item['digest'],))
            if deleted.rowcount!=1:raise ValueError('pending_ack_conflict')
        self.last_delivery=datetime.now(timezone.utc).isoformat()
        emit('pending_acknowledged',scope='sql_pending',verified=True)
        emit('temporary_package_cleaned',scope='sql_pending',verified=True)
        emit('archive_verified',run_id=report['run_id'],selected=len(report['sources']),
             new_posts=report.get('new_to_archive'),
             verified_posts=sum(r.get('archived_posts',0) for r in report['sources']),
             statuses=dict(Counter(r['status'] for r in report['sources'])))
        for platform in sorted({row['platform'] for row in report['sources']}):
            rows=[row for row in report['sources'] if row['platform']==platform]
            emit('platform_verified',platform=platform,selected=len(rows),
                 new_posts=len(report['platform_insertions'].get(platform,{})) if 'platform_insertions' in report else None,
                 verified_posts=sum(row.get('archived_posts',0) for row in rows),
                 statuses=dict(Counter(row['status'] for row in rows)))
        return True


def recover_drive(store,journal,feed=None):
    import base64
    staged=store.read_json(PENDING) or {}
    if staged.get('state')!='pending':return
    blob=base64.b64decode(staged['package'],validate=True)
    if hashlib.sha256(blob).hexdigest()!=staged['sha256']:raise ValueError('drive_pending_checksum')
    with tempfile.TemporaryDirectory(prefix='argus-drive-recovery-') as temporary:
        path=Path(temporary)/'package.zip';path.write_bytes(blob)
        journal.check_session()
        if feed:drain(store,path,staged['report'],journal=feed)
        else:deliver(store,path,staged['report'],before_readback=before_archive_readback)
    store.json(PENDING,{'state':'acknowledged','sha256':staged['sha256'],
                       'run_id':staged['report']['run_id']},mutable=True)
    emit('previous_drive_batch_recovered')


def service_comments(comments,journal):
    # A lost writer session must stop the worker; an adapter error must not stop news.
    journal.check_session()
    try:comments.tick(emit)
    except Exception as error:
        emit('comments_cycle_failed',error_type=type(error).__name__,sqlstate=getattr(error,'sqlstate',None))


def collect(catalog,state,registry,output,schedule=None,progress=None):
    for name,value in [('sources',catalog),('cursors',state),('registry',registry)]:
        (output/(name+'.json')).write_bytes(encoded(value))
    run_id='hostless-'+uuid.uuid4().hex
    command=[sys.executable,str(OPS/'collect/run.py'),'--incremental','--public-page',
             '--telegram-history','--sources',str(output/'sources.json'),
             '--cursors',str(output/'cursors.json'),'--registry',str(output/'registry.json'),
             '--output',str(output/'result'),'--sources-per-platform','0','--posts','30',
             '--window-hours','0','--concurrency','6']
    if schedule:
        command.extend(['--stream-results','--batch-seconds','105','--source-timeout','65'])
    # Silence child traceback/text; it can contain fetched URLs or private rows.
    proc=subprocess.Popen(command,env=collector_environment(run_id),cwd=OPS.parent,
                          stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
    try:
        if schedule:
            deadline=time.monotonic()+210
            spool_sources={hashlib.sha256(s['id'].encode()).hexdigest():s for s in catalog['sources']}
            def harvest():
                for path in sorted((output/'result'/'spool').glob('*.json')):
                    source=spool_sources.get(path.stem)
                    if not source:raise ValueError('unexpected_spool_file')
                    try:
                        if path.stat().st_size>8*1024**2:raise ValueError('source_result_too_large')
                        payload=json.loads(path.read_bytes())
                        if payload['record']['source_id']!=source['id']:raise ValueError('spool_source_mismatch')
                        schedule.stage(payload,time.time())
                    except (ValueError,KeyError,TypeError):
                        schedule.quarantine(source,'invalid_or_oversized_result',time.time())
                        emit('source_result_quarantined',platform=source['platform'])
                    path.unlink()  # only after durable database INSERT
            next_progress=0;next_memory=0
            while proc.poll() is None:
                harvest()
                if time.monotonic()>=next_memory:
                    memory_phase('collection_active');next_memory=time.monotonic()+15
                if progress and time.monotonic()>=next_progress:
                    progress();next_progress=time.monotonic()+2
                if time.monotonic()>deadline:raise TimeoutError('batch_deadline')
                time.sleep(.25)
            harvest()
            # Completed source results survive a later child failure. Unfinished
            # leases are released only after that process group is gone.
            code=proc.returncode
            memory_phase('collection_exit')
        else:code=proc.wait(timeout=600)
    except BaseException:
        if proc.poll() is None:
            os.killpg(proc.pid,signal.SIGKILL)
            proc.wait()
        raise
    if schedule:
        if code:emit('collector_batch_interrupted',exit_code=code)
        schedule.release_abandoned()
        return None
    if code:raise RuntimeError('collector_exit_'+str(code))
    result=output/'result'
    report=json.loads((result/'report.json').read_bytes())
    report['poll_target_seconds']=300
    return result/'package.zip',report


def assemble(schedule,output,max_bytes=2*1024**2):
    """Amortize full archive publication across bounded durable source results."""
    from storage import pack
    from record_quality import merge_observations
    posts={};rows=[];updates={};tokens={};size=0
    for payload in schedule.pending(max_bytes=max_bytes):
        encoded_size=len(encoded(payload))
        if rows and size+encoded_size>max_bytes:break
        size+=encoded_size
        row=payload['record'];sid=row['source_id']
        rows.append(row);tokens[sid]=payload['token']
        if payload.get('state_update'):updates[sid]=payload['state_update']
        for post in payload['posts']:
            key=post['canonical_key'];posts[key]=merge_observations(posts.get(key),post)
    if not rows:return None
    source=output/'posts.jsonl'
    with source.open('w',encoding='utf-8') as stream:
        for post in posts.values():stream.write(json.dumps(post,ensure_ascii=False)+'\n')
    media=output/'media';media.mkdir()
    package=output/'package.zip';pack(source,media,package)
    report={'format':'argus-collection-report-v1','run_id':'hostless-'+uuid.uuid4().hex,
            'updated_at':datetime.now(timezone.utc).isoformat(),'sources':rows,
            'state_updates':updates,'incremental':True,'live_lane':'all','poll_target_seconds':300,'poll_headroom_seconds':30,
            'lease_tokens':tokens,'started_sources':[r['source_id'] for r in rows if r.get('started_at')]}
    return package,report


def legacy_run():
    from runtime_policy import require_cloud
    require_cloud()
    mode=os.getenv('ARGUS_RUNTIME_MODE','preflight')
    if mode not in ('preflight','collect'):raise ValueError('invalid_runtime_mode')
    required=['ARGUS_DRIVE_OAUTH','ARGUS_DRIVE_FOLDER_ID','ARGUS_JSONBIN_KEY',
              'ARGUS_JSONBIN_BIN_ID','DATABASE_URL']
    missing=[key for key in required if not os.getenv(key)]
    if missing:
        emit('configuration_missing',names=missing)
        return
    emit('archive_access_check')
    store=from_environment();store.assert_private()
    emit('archive_catalog_loading')
    pointer=store.read_json('catalog.json')
    if not pointer:raise ValueError('existing_archive_required')
    emit('archive_snapshot_loading',expected_posts=pointer.get('posts'))
    memory_phase('archive_load_before')
    snapshot=load_snapshot(store,pointer)
    memory_phase('archive_load_after')
    emit('archive_read_verified',posts=len(snapshot['items']),updated_at=snapshot.get('updated_at'))
    del snapshot
    emit('memory_reclaimed',phase='startup',**release_memory())
    from argus_collectors.postgres import PostgresJournal
    schema='argus_drive_'+hashlib.sha256(store.root.encode()).hexdigest()[:24]
    emit('state_connecting')
    memory_phase('database_connect_before')
    journal=PostgresJournal(os.environ['DATABASE_URL'],schema=schema)
    memory_phase('database_connect_after')
    try:
        from schedule_store import ScheduleStore
        schedule=ScheduleStore(journal.db.connection)
        pending=Pending(journal,schedule)
        emit('registry_loading')
        registry=fetch_registry(os.environ['ARGUS_JSONBIN_BIN_ID'],os.environ['ARGUS_JSONBIN_KEY'],
                                os.getenv('ARGUS_JSONBIN_KEY_TYPE','access'))
        emit('preflight_ok',tls='verify-full',database_session='locked',mode=mode)
        if mode=='preflight':return
        if os.getenv('ARGUS_ARCHIVE_WRITER_OWNER')!='hostless':
            raise RuntimeError('archive_writer_cutover_not_confirmed')
        # One worker replica; old GitHub writers must be paused before this flag.
        memory_phase('pending_recovery_before')
        pending.recover(store)
        memory_phase('pending_recovery_after')
        recover_drive(store,journal)
        memory_phase('drive_recovery_after')
        emit('memory_reclaimed',phase='after_pending_recovery',**release_memory())
        from feed_journal import FeedJournal
        feed=FeedJournal(store,observe=memory_phase)
        pending.feed=feed
        from comments_store import Comments
        memory_phase('comments_init_before')
        comments=Comments(journal.db.connection)
        memory_phase('comments_init_after')
        from comments_ai import CommentAnalysis
        comment_analysis=CommentAnalysis()
        schedule.release_abandoned()
        refreshed=time.monotonic()
        from utilization import Utilization
        utilization=Utilization();utilization.sample()
        from typesafe_schedule import TypeSafeSchedule
        typesafe_schedule=TypeSafeSchedule(emit)
        from control import Control, apply_catalog
        control=Control(schedule)
        from runtime_catalog import load_private_catalog
        while True:
            cycle_started=time.monotonic()
            transfer_before=dict(store.telemetry)
            journal.check_session()
            if pending.recover(store):continue
            if time.monotonic()-refreshed>=21600:
                registry=fetch_registry(os.environ['ARGUS_JSONBIN_BIN_ID'],os.environ['ARGUS_JSONBIN_KEY'],
                                        os.getenv('ARGUS_JSONBIN_KEY_TYPE','access'))
                refreshed=time.monotonic()
            catalog=load_private_catalog(store,with_submissions)
            try:
                _, control_config=control.sync(store,catalog)
            except Exception as error:
                journal.check_session()
                emit('control_sync_failed',error_type=type(error).__name__)
                _, control_config=control.current()
            catalog=apply_catalog(catalog,control_config)
            typesafe_schedule.settings=control_config.get('ai',{})
            state=feed.verified_state if feed.verified_state is not None else store.read_json('collection-cursors.json') or {'schema':'argus-collection-cursors-v1','sources':{}}
            feed.catalog=catalog
            # Start and reap comment work while the news subprocess is active,
            # rather than retaining completed results through long AI/Drive work.
            memory_phase('comments_sync_before')
            try:comments.sync(feed.snapshot['items'],catalog['sources'])
            except Exception as error:emit('comments_sync_failed',error_type=type(error).__name__)
            memory_phase('comments_sync_after')
            emit('memory_reclaimed',phase='after_comments_sync',**release_memory())
            service_comments(comments,journal)
            enabled={r['platform'] for r in registry['collectors'] if r['enabled']}
            schedule.sync(catalog,state,enabled)
            # Publish committed owner commands before a potentially long collection
            # cycle. Retry a failed publication without reapplying the command.
            if control.unpublished:
                try:control.publish(store,schedule,registry)
                except Exception as error:
                    journal.check_session()
                    emit('control_publish_failed',error_type=type(error).__name__)
            sources=schedule.claim(time.time(),limit=64)
            chosen={**catalog,'sources':sources,'live_lane':'all','live_order':[s['id'] for s in sources]}
            emit('schedule_status',target_seconds=300,**schedule.metrics(time.time()))
            if chosen['sources']:
                emit('memory_reclaimed',phase='before_collection',**release_memory())
                emit('collection_started',lane='all',selected=len(chosen['sources']),
                     platforms=sorted({source['platform'] for source in chosen['sources']}))
                with tempfile.TemporaryDirectory(prefix='argus-collect-') as temporary:
                    try:collect(chosen,state,registry,Path(temporary),schedule,progress=lambda:service_comments(comments,journal))
                    except TimeoutError:
                        emit('collector_batch_deadline',pending=schedule.metrics(time.time())['pending_results'])
                        schedule.release_abandoned()
            else:emit('no_due_sources',lane='all')
            with tempfile.TemporaryDirectory(prefix='argus-merge-') as temporary:
                memory_phase('assemble_before')
                batch=assemble(schedule,Path(temporary))
                memory_phase('assemble_after')
                if batch:
                    package,report=batch
                    pending.stage(package,report)
                    emit('package_staged',run_id=report['run_id'],selected=len(report['sources']))
            if batch:pending.recover(store)
            # Enqueue immediately after verified post publication. Expensive
            # compaction/analytics must not delay the first discussion request.
            try:
                comments.sync(feed.snapshot['items'],catalog['sources'])
            except Exception as error:
                emit('comments_sync_failed',error_type=type(error).__name__)
            service_comments(comments,journal)
            if feed.compact():emit('archive_compacted',posts=len(feed.snapshot['items']))
            try:
                if feed.publish_analytics():emit('analytics_published',posts=len(feed.analytics.rows),sequence=(feed.head or {}).get('last',0))
            except Exception as error:
                # The next cycle retries this derived product. Never lose a
                # successfully archived publication because statistics failed.
                emit('analytics_publish_failed',error_type=type(error).__name__)
            try:
                journal.check_session()
                comments.tick(emit)
                comment_analysis.run_due(comments,store,journal,emit)
                journal.check_session()
                if comments.publish(store):emit('comments_published',**comments.metrics())
            except Exception as error:
                emit('comments_cycle_failed',error_type=type(error).__name__)
            typesafe_schedule.run_due(store,feed,journal)
            emit('memory_reclaimed',phase='after_archive',**release_memory())
            # Small independent telemetry object, no archive scan or credentials.
            monitor=feed.monitor if feed.monitor is not None else store.read_json('monitor.json') or {}
            monitor['runtime']={'at':datetime.now(timezone.utc).isoformat(),**schedule.metrics(time.time()),
                                'comments':comments.metrics(),
                                'source_concurrency':6,'browser_contexts':1,'batch_limit':64,
                                'platform_concurrency':{'telegram':2,'youtube':2,'browser':1},
                                'archive_posts':len(feed.snapshot['items']),'feed_sequence':feed.head['last'] if feed.head else 0,
                                'archive_write_seconds':feed.last_write_seconds,'delivery_mode':'verified_drive_journal',
                                'last_successful_delivery':pending.last_delivery,'archive_write_failures_this_session':pending.write_failures,
                                'worker_started_at':STARTED_AT,'deployment_sha':release_sha(),'drive_sync':'verified',
                                'collection_sha256':collection_fingerprint(),
                                'drive_transfer':{k:round(v-transfer_before[k],3) for k,v in store.telemetry.items()},
                                **utilization.sample(),'cycle_seconds':round(time.monotonic()-cycle_started,3)}
            store.json('monitor.json',monitor,mutable=True)
            try:control.publish(store,schedule,registry)
            except Exception as error:
                journal.check_session()
                emit('control_publish_failed',error_type=type(error).__name__)
            emit('cycle_completed',**monitor['runtime'])
            time.sleep(2 if sources else 15)
    finally:
        if 'comments' in locals():comments.close()
        journal.close()


def run():
    with Startup(emit).phase('runtime_import'):
        from continuous_runtime import run as continuous_run
    return continuous_run(sys.modules[__name__])


if __name__=='__main__':
    for sig in (signal.SIGTERM,signal.SIGINT):
        signal.signal(sig,lambda *_:STOP_REQUESTED.set())
    socket.setdefaulttimeout(30)
    mode=os.getenv('ARGUS_RUNTIME_MODE','preflight')
    emit('worker_started',mode=mode if mode in ('preflight','collect') else 'invalid',
         runtime_sha256=runtime_fingerprint(),
         collection_sha256=collection_fingerprint(),
         code_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
         scheduler_sha256=hashlib.sha256(Path(__file__).with_name('schedule_store.py').read_bytes()).hexdigest())
    while not STOP_REQUESTED.is_set():
        try:
            run()
            if os.getenv('ARGUS_RUNTIME_MODE','preflight')=='preflight':
                while not STOP_REQUESTED.wait(60):pass
            STOP_REQUESTED.wait(60)
        except Exception as error:
            emit('worker_error',error_type=safe_error_type(error),category=error_category(error),
                 sqlstate=safe_sqlstate(error))
            STOP_REQUESTED.wait(60)
