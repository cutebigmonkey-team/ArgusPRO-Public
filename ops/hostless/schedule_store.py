"""Durable source leases and bounded completed-result spool.

All SQL values are bound. Production uses the existing locked PostgreSQL
session; SQLite is only a deterministic, disposable test implementation.
Archive acknowledgements, never collection completion, advance checkpoints.
"""
import hashlib
import json
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import urlsplit

from source_health import update, SKIPPED

MAX_RESULT = 8 * 1024**2
MAX_SPOOL = 48 * 1024**2


def scheduling_class(source):
    value=source.get('scheduling_class')
    if value in {'fast','regular','browser'}:return value
    if source['platform'] in {'telegram','youtube','rss','reddit','gdelt','mailru','rutube'}:return 'fast'
    if source['platform'] in {'facebook','threads','vk','ok','whatsapp'}:return 'browser'
    return 'regular'


def stamp(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp() if value else 0.0


def iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def collection_requested(row, now):
    """One operator request, consumed by an acknowledged collection attempt."""
    try:
        value = json.loads(row['source']).get('collection_requested_at')
        if not isinstance(value, str):return False
        requested = stamp(value)
    except (TypeError, ValueError, OverflowError):
        return False
    return row['last_completed_at'] < requested <= now


def request_group(source):
    return urlsplit(source['url']).hostname if source['platform'] in ('rss','mailru') else source['platform']


def repaired_timeout_retry(row, now):
    """An operator may retry one exact timeout after repairing its cause."""
    try:
        source=json.loads(row['source']); health=json.loads(row['health'])
        request=source.get('repaired_timeout_retry',{})
        return (health.get('url')==source['url'] and health.get('collector_status')=='timeout' and
                stamp(health.get('last_checked_at')) > 0 and
                request.get('failed_check_at')==health.get('last_checked_at') and
                row['last_completed_at'] < stamp(request.get('requested_at')) <= now and
                stamp(health.get('host_cooldown_until')) <= now)
    except (TypeError,ValueError,AttributeError,OverflowError):
        return False


class ScheduleStore:
    def __init__(self, connection, *, sqlite=False):
        self.db, self.sqlite = connection, sqlite
        self.execute('''CREATE TABLE IF NOT EXISTS runtime_schedule (
            id TEXT PRIMARY KEY, source TEXT NOT NULL, health TEXT NOT NULL,
            next_check_at DOUBLE PRECISION NOT NULL, last_attempt_at DOUBLE PRECISION NOT NULL DEFAULT 0,
            last_reserved_at DOUBLE PRECISION NOT NULL DEFAULT 0,
            last_completed_at DOUBLE PRECISION NOT NULL DEFAULT 0,
            priority INTEGER NOT NULL DEFAULT 0, enabled INTEGER NOT NULL,
            lease_until DOUBLE PRECISION NOT NULL DEFAULT 0, lease_token TEXT,
            latency DOUBLE PRECISION NOT NULL DEFAULT 10, failures INTEGER NOT NULL DEFAULT 0)''')
        self.execute('''CREATE TABLE IF NOT EXISTS runtime_results (
            source_id TEXT PRIMARY KEY, token TEXT NOT NULL, payload TEXT NOT NULL,
            bytes INTEGER NOT NULL, created_at DOUBLE PRECISION NOT NULL)''')
        self.execute('CREATE INDEX IF NOT EXISTS runtime_due ON runtime_schedule(enabled,next_check_at,lease_until)')
        self.execute('''CREATE TABLE IF NOT EXISTS runtime_dead_letters (
            source_id TEXT PRIMARY KEY, category TEXT NOT NULL, failures INTEGER NOT NULL,
            last_failed_at DOUBLE PRECISION NOT NULL)''')
        self.execute('''CREATE TABLE IF NOT EXISTS runtime_circuits (
            group_key TEXT PRIMARY KEY, until_at DOUBLE PRECISION NOT NULL,
            failures INTEGER NOT NULL, updated_at DOUBLE PRECISION NOT NULL)''')
        from check_metrics import initialize
        initialize(self.execute,time.time())
        self.execute('CREATE TABLE IF NOT EXISTS runtime_reader_pacing (group_key TEXT PRIMARY KEY,finished_at DOUBLE PRECISION NOT NULL,reserved_at DOUBLE PRECISION NOT NULL DEFAULT 0)')

    def execute(self, sql, args=()):
        return self.db.execute(sql if self.sqlite else sql.replace('?', '%s'), args)

    def transaction(self):
        return self.db if self.sqlite else self.db.transaction()

    def sync(self, catalog, state, enabled):
        health = state.get('incremental_polling', {})
        with self.transaction():
            self.execute('UPDATE runtime_schedule SET enabled=0')
            sources=catalog['sources']
            for offset in range(0,len(sources),128):
                batch=sources[offset:offset+128];ids=[s['id'] for s in batch]
                marks=','.join('?' for _ in ids)
                existing={r['id']:r for r in self.execute('SELECT * FROM runtime_schedule WHERE id IN ('+marks+')',ids).fetchall()}
                pending={r['source_id'] for r in self.execute('SELECT source_id FROM runtime_results WHERE source_id IN ('+marks+')',ids).fetchall()}
                inserts=[];updates=[];intervals=[];restored=[]
                for source in batch:
                    old=existing.get(source['id']);current=health.get(source['id'],{})
                    if current.get('url')!=source['url']:current={}
                    active=int(source.get('enabled',True) and source['platform'] in enabled)
                    if old and old['failures']>source.get('control_retry_limit',1000000):active=0
                    priority=max(-10,min(10,int(source.get('priority',0))))
                    if old is None:
                        inserts.append((source['id'],json.dumps(source),json.dumps(current),stamp(current.get('next_check_at')),active,stamp(current.get('last_checked_at')),priority))
                        continue
                    previous=json.loads(old['source']);changed=previous['url']!=source['url']
                    updates.append((json.dumps(source),active,priority,source['id']))
                    if not old['failures'] and not old['lease_token'] and previous.get('control_interval')!=source.get('control_interval'):
                        intervals.append((old['last_completed_at']+source.get('control_interval',300),source['id']))
                    if source['id'] not in pending and not old['lease_token'] and (changed or stamp(current.get('last_checked_at'))>old['last_completed_at']):
                        restored.append((json.dumps(current),stamp(current.get('next_check_at')),stamp(current.get('last_checked_at')),stamp(current.get('last_checked_at')),source['id']))
                operations=[
                    ('INSERT INTO runtime_schedule(id,source,health,next_check_at,enabled,last_completed_at,priority) VALUES(?,?,?,?,?,?,?)',inserts),
                    ('UPDATE runtime_schedule SET source=?,enabled=?,priority=? WHERE id=?',updates),
                    ('UPDATE runtime_schedule SET next_check_at=? WHERE id=?',intervals),
                    ('UPDATE runtime_schedule SET health=?,next_check_at=?,last_completed_at=?,last_attempt_at=? WHERE id=?',restored)]
                cursor=self.db.cursor()
                try:
                    for query,values in operations:
                        if values:cursor.executemany(query if self.sqlite else query.replace('?','%s'),values)
                finally:cursor.close()

    def release_abandoned(self):
        # Called only after acquiring the archive-wide session lock: the prior
        # supervisor cannot still be a writer. Durable results remain reserved.
        self.execute('''UPDATE runtime_schedule SET lease_token=NULL,lease_until=0
            WHERE id NOT IN (SELECT source_id FROM runtime_results)''')

    def release_sources(self,sources):
        # A finished child must never release a sibling lane's live lease.
        with self.transaction():
            for source in sources:
                self.execute('''UPDATE runtime_schedule SET lease_token=NULL,lease_until=0
                    WHERE id=? AND lease_token=? AND id NOT IN (SELECT source_id FROM runtime_results)''',
                    (source['id'],source['_lease_token']))

    def renew_sources(self,sources,until):
        with self.transaction():
            for source in sources:
                self.execute('''UPDATE runtime_schedule SET lease_until=? WHERE id=? AND lease_token=?''',
                             (until,source['id'],source['_lease_token']))

    def claim(self, now, *, limit=32, lease_seconds=600,classes=None,platforms=None,exclude_platforms=(),pace=False):
        # Reserve the worst-case result size BEFORE starting a source. A Drive
        # outage cannot overfill SQL while an admitted child finishes its work.
        reserved=self.execute('''SELECT COUNT(*) AS n FROM runtime_schedule WHERE lease_until>?
            AND id NOT IN (SELECT source_id FROM runtime_results)''',(now,)).fetchone()['n']
        limit=min(limit,max(0,(MAX_SPOOL-self.spool_bytes())//MAX_RESULT-reserved))
        if not limit:return []
        rows = self.execute('''SELECT * FROM runtime_schedule WHERE enabled=1
            AND (next_check_at<=? OR source LIKE ?)
            AND lease_until<=? AND id NOT IN (SELECT source_id FROM runtime_results)
            ORDER BY priority DESC, last_reserved_at, next_check_at, id''',
            (now,'%"repaired_timeout_retry"%',now)).fetchall()
        rows=[row for row in rows if (row['next_check_at']<=now or repaired_timeout_retry(row,now))
              and (classes is None or scheduling_class(json.loads(row['source'])) in classes)
              and (platforms is None or json.loads(row['source'])['platform'] in platforms)
              and json.loads(row['source'])['platform'] not in exclude_platforms]
        # Priority grants a bounded head start, never permanent precedence over
        # an older eligible reservation. Stored Retry-After/circuits still gate
        # eligibility above/below; no deadline is shortened by this ordering.
        rows.sort(key=lambda row: (max(row['last_reserved_at'],row['next_check_at'])
                                   - 30*max(-10,min(10,row['priority'])),row['id']))
        # Requests reorder only eligible work. Leases, retry deadlines, shared
        # circuits and acknowledgement remain authoritative, including retries.
        rows = sorted(rows, key=lambda row: not collection_requested(row, now))
        # Give each platform a live turn before draining its discovery backlog.
        # Prefer an overdue, recently successful publisher for that single turn;
        # the rest of the batch still rotates oldest-first through ALL sources.
        # Eligibility/cooldowns above and circuits below remain authoritative.
        groups = {}
        for row in rows:
            platform=json.loads(row['source'])['platform']
            groups.setdefault(platform,[]).append(row)
        first=[];live_remainder=[]
        for candidates in groups.values():
            live=[]
            for row in candidates:
                health=json.loads(row['health'])
                last_success=stamp(health.get('last_success_at'))
                last_post=max(stamp(health.get('last_post_published_at')),stamp(health.get('last_new_post_at')))
                if (limit>len(groups) and last_success and last_post and
                        0 <= now-last_success <= 86400 and 0 <= now-last_post <= 86400 and
                        health.get('collector_status') in {'ok','no_new_posts','no_public_posts'}):
                    live.append(row)
            requested=[row for row in candidates if collection_requested(row, now)]
            overdue=[row for row in candidates
                     if max(row['last_reserved_at'],row['next_check_at'])<=now-300]
            # When a platform can afford only one reader, alternate an aged
            # source with its fresh publisher. Persistent reservation times
            # survive restart; admitting a live peer must not consume every
            # turn while older eligible work waits forever.
            last_turn=max(row['last_reserved_at'] for row in candidates)
            last_live=max((row['last_reserved_at'] for row in live),default=-1)
            aged=overdue if live and last_live>=last_turn else []
            picked=(requested or aged or live or candidates)[0]
            first.append(picked)
            if live and picked['id']!=live[0]['id']:
                live_remainder.append(live[0])
        platform_reserved={}
        for row in self.execute('SELECT source,last_reserved_at FROM runtime_schedule WHERE enabled=1').fetchall():
            platform=json.loads(row['source'])['platform']
            platform_reserved[platform]=max(platform_reserved.get(platform,0),row['last_reserved_at'])
        first.sort(key=lambda row:(not collection_requested(row,now),
            platform_reserved.get(json.loads(row['source'])['platform'],0)-30*max(-10,min(10,row['priority'])),row['id']))
        reserved={row['id'] for row in first}
        live_ids={row['id'] for row in live_remainder}
        rest=live_remainder+[row for row in rows if row['id'] not in reserved|live_ids]
        chosen=[]; estimated=0.0; browser_count=0; platform_seconds={}
        circuits={r['group_key']:r for r in self.execute('SELECT * FROM runtime_circuits').fetchall()}
        probed=set()
        for row in first+rest:
            source=json.loads(row['source'])
            spacing={'instagram':5,'telegram':1,'youtube':1,'facebook':1,'threads':3,'tiktok':3,'vk':2,'ok':2,'x':3}.get(source['platform'],0)
            last=self.execute('SELECT * FROM runtime_reader_pacing WHERE group_key=?',(source['platform'],)).fetchone() if spacing else None
            if last and ((pace and now<last['reserved_at']+spacing) or (source['platform']=='instagram' and now<last['finished_at']+5)):continue
            group_key=request_group(source)
            circuit=circuits.get(group_key)
            if circuit and (circuit['until_at']>now or (circuit['failures']>=3 and group_key in probed)):continue
            browser=source['platform'] in {'facebook','threads','vk','ok','whatsapp'}
            if browser and browser_count>=3: continue
            cost=max(1,min(60,row['latency']))
            # Platform admission limits apply even with spare global slots.
            # Reserve their pacing cost before filling a batch; otherwise a
            # large Instagram catalogue spends most leases on deferred jobs.
            platform=source['platform']
            pacing={'instagram':3,'telegram':1,'youtube':1,'tiktok':3,'threads':3,
                    'facebook':1,'vk':2,'ok':2,'x':3}.get(platform,0)
            group=platform if pacing else urlsplit(source['url']).hostname
            platform_budget=200 if platform in ('telegram','youtube') else 100
            if platform_seconds.get(group,0)+cost+pacing>platform_budget and group in platform_seconds:continue
            if len(chosen)>=limit or (len(chosen)>=8 and estimated+cost>500): continue
            token=uuid.uuid4().hex
            # Compare-and-set is the reservation. Another claimant can select
            # the same candidate but cannot obtain the same live lease.
            with self.transaction():
                result=self.execute('''UPDATE runtime_schedule SET lease_token=?,lease_until=?,last_reserved_at=?
                    WHERE id=? AND enabled=1 AND lease_until<=? AND next_check_at<=?
                    AND id NOT IN (SELECT source_id FROM runtime_results)''',
                    (token,now+lease_seconds,now,row['id'],now,
                     max(now,row['next_check_at']) if repaired_timeout_retry(row,now) else now))
            if result.rowcount:
                chosen.append({**source,'_lease_token':token})
                if spacing:
                    self.execute('''INSERT INTO runtime_reader_pacing(group_key,finished_at,reserved_at) VALUES(?,0,?)
                        ON CONFLICT(group_key) DO UPDATE SET reserved_at=excluded.reserved_at''',(source['platform'],now))
                estimated+=cost;browser_count+=int(browser)
                platform_seconds[group]=platform_seconds.get(group,0)+cost+pacing
                probed.add(group_key)
        return chosen

    def stage(self, payload, now):
        row=payload['record']; sid=row['source_id']; token=payload['token']
        posts=payload.get('posts')
        if not isinstance(posts,list) or any(not isinstance(p,dict) or any(not isinstance(p.get(k),str)
                for k in ('canonical_key','platform','post_url','text','observed_at')) for p in posts):
            raise ValueError('invalid_source_posts')
        if set(row.get('canonical_keys',[]))!={p['canonical_key'] for p in posts}:
            raise ValueError('source_post_manifest_mismatch')
        raw=json.dumps(payload,ensure_ascii=False,separators=(',',':'),allow_nan=False)
        size=len(raw.encode())
        if size>MAX_RESULT: raise ValueError('source_result_too_large')
        with self.transaction():
            existing=self.execute('SELECT token,payload FROM runtime_results WHERE source_id=?',(sid,)).fetchone()
            if existing:
                if existing['token']==token and existing['payload']==raw:return
                raise ValueError('source_result_conflict')
            source=self.execute('SELECT * FROM runtime_schedule WHERE id=?',(sid,)).fetchone()
            if not source or source['lease_token']!=token or source['lease_until']<=now:raise ValueError('expired_source_lease')
            if json.loads(source['source'])['url']!=row['url']:raise ValueError('source_url_changed')
            if self.spool_bytes()+size>MAX_SPOOL:raise ValueError('result_spool_full')
            self.execute('INSERT INTO runtime_results VALUES(?,?,?,?,?)',(sid,token,raw,size,now))
            from check_metrics import record as record_check
            record_check(self.execute,row,token,stamp(row.get('checked_at')) or now)
            self.observe_circuit(json.loads(source['source']),row,now)
            if row['platform']=='instagram' and row['status'] not in SKIPPED:
                self.execute('''INSERT INTO runtime_reader_pacing(group_key,finished_at) VALUES(?,?) ON CONFLICT(group_key)
                    DO UPDATE SET finished_at=excluded.finished_at''',('instagram',now))
            if row.get('started_at'):
                self.execute('UPDATE runtime_schedule SET last_attempt_at=? WHERE id=?',(stamp(row['started_at']),sid))

    def observe_circuit(self,source,record,now):
        # Quota and transient origin failures apply to sibling sources too.
        # Auth/robots failures remain source-specific and never disable a platform.
        key=request_group(source);status=record['status']
        old=self.execute('SELECT * FROM runtime_circuits WHERE group_key=?',(key,)).fetchone()
        transient=status in {'timeout','network_error','dns_error','connection_reset','http_502','http_503','http_504'}
        limited=status in {'http_429','rate_limited','gateway_rate_limited'}
        if not transient and not limited:
            if status in {'ok','no_new_posts','no_public_posts'} and old:
                # A success cannot shorten an explicitly active Retry-After.
                self.execute('UPDATE runtime_circuits SET failures=0 WHERE group_key=?',(key,))
            return
        failures=1+(old['failures'] if old and now-old['updated_at']<600 else 0)
        until=max(stamp(record.get('retry_after')),now+60 if limited else now+min(300,15*2**min(failures-3,4)) if failures>=3 else now)
        if old:until=max(until,old['until_at'])
        self.execute('''INSERT INTO runtime_circuits VALUES(?,?,?,?) ON CONFLICT(group_key)
            DO UPDATE SET until_at=excluded.until_at,failures=excluded.failures,updated_at=excluded.updated_at''',
            (key,until,failures,now))

    def pending(self,max_bytes=16*1024**2):
        result=[];size=0
        for row in self.execute('SELECT source_id,bytes FROM runtime_results ORDER BY created_at,source_id').fetchall():
            if result and size+row['bytes']>max_bytes:break
            raw=self.execute('SELECT payload FROM runtime_results WHERE source_id=?',(row['source_id'],)).fetchone()
            if raw:result.append(json.loads(raw['payload']));size+=row['bytes']
        return result

    def quarantine(self,source,category,now):
        # A bounded failure ledger (one row/source), never a successful cursor.
        # Retrying from the last archive checkpoint is safe after partial loss.
        with self.transaction():
            from source_health import update
            old=self.execute('SELECT health FROM runtime_schedule WHERE id=?',(source['id'],)).fetchone()
            previous=json.loads(old['health']) if old else {}
            rejected={'source_id':source['id'],'platform':source['platform'],'url':source['url'],
                'status':category,'checked_at':iso(now),'new_keys':[],'canonical_keys':[],
                'attempts':[{'tool':'result_transport','status':category,'posts':0}],'seconds':0,
                'remaining_ranges':previous.get('remaining_ranges')}
            health=update(previous,rejected,target_seconds=300)
            next_check=max(now+3600,stamp(health.get('next_check_at')))
            health['next_check_at']=iso(next_check)
            self.execute('''INSERT INTO runtime_dead_letters VALUES(?,?,1,?) ON CONFLICT(source_id)
                DO UPDATE SET category=excluded.category, failures=runtime_dead_letters.failures+1,
                last_failed_at=excluded.last_failed_at''',(source['id'],category,now))
            self.execute('''UPDATE runtime_schedule SET lease_token=NULL,lease_until=0,next_check_at=?,failures=failures+1,health=?
                WHERE id=? AND lease_token=?''',(next_check,json.dumps(health),source['id'],source['_lease_token']))

    def spool_bytes(self):
        return self.execute('SELECT COALESCE(SUM(bytes),0) AS n FROM runtime_results').fetchone()['n']

    def acknowledge(self, report):
        # Invoked in the same transaction as removing runtime_pending, only
        # after the existing Drive writer has verified posts AND cursors.
        for record in report['sources']:
            sid=record['source_id']; token=report.get('lease_tokens',{}).get(sid)
            if not token:continue  # backwards-compatible recovery of old batches
            old=self.execute('SELECT * FROM runtime_schedule WHERE id=?',(sid,)).fetchone()
            result=self.execute('SELECT token FROM runtime_results WHERE source_id=?',(sid,)).fetchone()
            if not old or not result or result['token']!=token:raise ValueError('ack_lease_mismatch')
            health=json.loads(old['health'])
            changed=json.loads(old['source'])['url']!=record['url']
            if changed:health={}
            elif record['status'] not in SKIPPED and record.get('checked_at'):
                source=json.loads(old['source'])
                health=update(health,record,target_seconds=source.get('control_interval',300),headroom_seconds=0 if 'control_interval' in source else 30)
                # An explicit owner interval is a setting, not the adaptive
                # scheduler's freshness ceiling. Never shorten server cooldowns.
                if not health.get('consecutive_errors') and 'control_interval' in source:
                    health['next_check_at']=iso(max(stamp(record['checked_at'])+source['control_interval'],stamp(record.get('retry_after'))))
                if health.get('consecutive_errors') and 'control_retry_delay' in source:
                    health['next_check_at']=iso(max(stamp(health['next_check_at']),stamp(record['checked_at'])+source['control_retry_delay']))
            completed=0 if changed else stamp(record.get('checked_at')) or old['last_completed_at']
            self.execute('''UPDATE runtime_schedule SET health=?,next_check_at=?,last_completed_at=?,
                lease_until=0,lease_token=NULL,latency=?,failures=? WHERE id=?''',
                (json.dumps(health),stamp(health.get('next_check_at')),completed,
                 .7*old['latency']+.3*max(0,record.get('seconds',0)),health.get('consecutive_errors',0),sid))
            self.execute('DELETE FROM runtime_results WHERE source_id=? AND token=?',(sid,token))
            from check_metrics import record as record_check
            record_check(self.execute,record,token,completed)
            self.execute('DELETE FROM runtime_dead_letters WHERE source_id=?',(sid,))

    def metrics(self, now):
        rows=self.execute('SELECT * FROM runtime_schedule').fetchall()
        pending=self.execute('SELECT COUNT(*) AS n,MIN(created_at) AS oldest FROM runtime_results').fetchone()
        pending_ids={r['source_id'] for r in self.execute('SELECT source_id FROM runtime_results').fetchall()}
        platforms={}
        for row in rows:
            p=platforms.setdefault(json.loads(row['source'])['platform'],dict(registered=0,active=0,due=0,leased=0,pending_results=0))
            p['registered']+=1;p['active']+=row['enabled']
            p['due']+=int(bool(row['enabled']) and row['next_check_at']<=now)
            p['leased']+=int(row['lease_until']>now)
            p['pending_results']+=int(row['id'] in pending_ids)
        return {'registered':len(rows),'active':sum(r['enabled'] for r in rows),
                'platform_queues':platforms,
                'dead_letters':self.execute('SELECT COUNT(*) AS n FROM runtime_dead_letters').fetchone()['n'],
                'pending_results':pending['n'],'pending_bytes':self.spool_bytes(),
                'oldest_pending_seconds':max(0,now-pending['oldest']) if pending['oldest'] else 0,
                'leased':sum(r['lease_until']>now for r in rows),
                'open_circuits':self.execute('SELECT COUNT(*) AS n FROM runtime_circuits WHERE until_at>?',(now,)).fetchone()['n'],
                'oldest_due_seconds':max((max(0,now-r['next_check_at']) for r in rows if r['enabled'] and r['next_check_at']>0),default=0),
                'due':sum(r['enabled'] and r['next_check_at']<=now for r in rows),
                'last_attempt_at':max((r['last_attempt_at'] for r in rows),default=0)}
