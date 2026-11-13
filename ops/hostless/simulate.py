"""Virtual-clock queue soak; synthetic services, NEVER real platform traffic.

This exercises the production lease/spool/ack code against disposable SQLite.
It is not a CPU, browser, network or PostgreSQL throughput benchmark.
"""
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import time

sys.path[:0]=[str(Path(__file__).resolve().parent),str(Path(__file__).resolve().parents[1]/'collect')]
from schedule_store import ScheduleStore,iso

PLATFORMS=('facebook','instagram','mailru','ok','rss','rutube','telegram','threads','tiktok','vk','whatsapp','x','youtube')


def simulate(seconds=300,*,faults=False,finite_outage=False,service_seconds=.05,publish_seconds=.2):
    begin=time.monotonic();count=1198;epoch=1800000000.0;now=epoch
    last={};attempts=0;peak_pending=0;max_gap=0;restarts=0;last_limited={};violations=0
    catalog={'sources':[{'id':str(i),'platform':PLATFORMS[i%13],
                        'url':f'https://fixture.invalid/{i}'} for i in range(count)]}
    with tempfile.TemporaryDirectory(prefix='argus-simulation-') as temporary:
        path=Path(temporary)/'queue.sqlite'
        def connect():
            db=sqlite3.connect(path);db.row_factory=sqlite3.Row
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('PRAGMA synchronous=NORMAL')
            return db
        db=connect();queue=ScheduleStore(db,sqlite=True)
        queue.sync(catalog,{},set(PLATFORMS))
        db.execute('CREATE TABLE simulated_archive(id TEXT PRIMARY KEY)');db.commit()
        while now<epoch+seconds:
            if faults and not restarts and now-epoch>=400:
                db.close();db=connect();queue=ScheduleStore(db,sqlite=True)
                queue.release_abandoned();db.commit();restarts+=1
            selected=queue.claim(now,limit=32)
            items=[]
            for s in selected:
                sid=s['id'];status='ok';delay=service_seconds
                if faults and sid not in last_limited and int(sid)%29==0 and (not finite_outage or now-epoch<500):
                    status='http_429'
                elif faults and int(sid)%53==0 and now-epoch<500:status='http_403'
                elif faults and int(sid)%47==0 and now-epoch<500:status='http_503'
                elif faults and int(sid)%37==0 and now-epoch<500:
                    status='timeout';delay=65
                if sid in last_limited and now<last_limited[sid]:violations+=1
                record={'source_id':sid,'url':s['url'],'platform':s['platform'],
                    'started_at':iso(now),'checked_at':iso(now+delay),'seconds':delay,
                    'status':status,'attempts':[],'canonical_keys':[],'new_keys':[]}
                if status=='http_429':
                    record['retry_after']=iso(now+3600);last_limited[sid]=now+3600
                p={'token':s['_lease_token'],'record':record,'posts':[],'state_update':None}
                queue.stage(p,now+delay);queue.stage(p,now+delay)  # duplicate delivery
                items.append(p);attempts+=1
            # Synthetic four-slot service waves. Hung fixtures obey the same
            # 65-second bound; no network is contacted.
            if items:now+=sum(p['record']['seconds'] for p in items)/4
            peak_pending=max(peak_pending,len(queue.pending()))
            storage_down=faults and 390<=now-epoch<460
            if not storage_down:
                pending=queue.pending()
                with queue.transaction():
                    for p in pending:
                        r=p['record'];sid=r['source_id']
                        if r['status']=='ok':
                            checked=float(__import__('datetime').datetime.fromisoformat(r['checked_at']).timestamp())
                            if sid in last:max_gap=max(max_gap,checked-last[sid])
                            last[sid]=checked
                            db.execute('INSERT OR IGNORE INTO simulated_archive VALUES(?)',(sid,))
                    queue.acknowledge({'sources':[p['record'] for p in pending],
                                      'lease_tokens':{p['record']['source_id']:p['token'] for p in pending}})
                if pending:now+=publish_seconds
            now+=.25 if selected else 1
        metrics=queue.metrics(now)
        unique=db.execute('SELECT COUNT(*) FROM simulated_archive').fetchone()[0]
        duplicates=db.execute('SELECT COUNT(*)-COUNT(DISTINCT id) FROM simulated_archive').fetchone()[0]
        assert violations==0 and duplicates==0
        assert metrics['pending_bytes']<=48*1024**2
        if not faults:assert len(last)==count and max_gap<=300
        db.close()
    return {'kind':'virtual_clock_queue_simulation','sources':count,'virtual_seconds':seconds,
        'fixture_service_seconds':service_seconds,'fixture_publish_seconds':publish_seconds,
        'faults':faults,'finite_outage':finite_outage,'successful_sources_5m':sum(now-300<t<=now for t in last.values()),
        'ever_successful':len(last),'max_success_gap_seconds':round(max_gap,2),
        'attempts':attempts,'unique_simulated_archive_ids':unique,'duplicate_archive_ids':duplicates,
        'max_pending_results':peak_pending,'remaining_pending_results':metrics['pending_results'],
        'restart_count':restarts,'retry_after_violations':violations,'wall_seconds':round(time.monotonic()-begin,3)}


if __name__=='__main__':
    results=[simulate(n) for n in (300,1800,7200)]
    results.append(simulate(7200,faults=True))
    results.append(simulate(7200,faults=True,finite_outage=True))
    print(json.dumps(results,indent=2))
