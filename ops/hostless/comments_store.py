"""Durable comment jobs and verified, paginated private Drive read model.

Uses the supervisor's existing PostgreSQL session lock. SQLite is test-only.
Comment and cursor updates are one transaction; publication retries independently.
"""
import hashlib
from itertools import islice
import json
import os
import random
import signal
import subprocess
import sys
import time
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from drive_store import encoded
from comment_schedule import delay, retry_seconds

STATUSES={'success','partial','rate_limited','auth_required','forbidden','unsupported','failed'}
PLATFORMS=('threads','telegram','youtube','facebook','instagram','tiktok','rss','wordpress','reddit','x','vk','ok','max')


def iso(now):return datetime.fromtimestamp(now,timezone.utc).isoformat()


def terminate(proc):
    if os.name=='posix':
        try:os.killpg(proc.pid,signal.SIGKILL)
        except ProcessLookupError:pass
    else:proc.kill()


def timestamp(value):
    try:
        if isinstance(value,(int,float)):return iso(value)
        if not value:return None
        parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
        return parsed.astimezone(timezone.utc).isoformat() if parsed.tzinfo else None
    except (ValueError,AttributeError,OverflowError,OSError):return None


def key(platform,post_id,external):
    return hashlib.sha256(encoded([platform,post_id,str(external)])).hexdigest()


def normalize(post,row,now):
    external=str(row.get('id') or '')
    text=row.get('text')
    if not external or not isinstance(text,str) or not text.strip():return None
    parent=row.get('parent_id')
    return dict(id=key(post['platform'],post['id'],external),external_id=external[:300],post_id=post['id'],
        platform=post['platform'],source_id=post.get('source_id',''),source_name=post.get('source_name',''),
        post_url=post.get('url',''),post_text=(post.get('text') or post.get('title') or '')[:600],
        origin=post.get('origin','unknown'),parent_id=key(post['platform'],post['id'],parent) if parent else None,
        author=str(row.get('author') or '')[:200],text=text[:16000],url=str(row.get('url') or '')[:2048],
        published_at=timestamp(row.get('published_at')),updated_at=timestamp(row.get('updated_at')),
        published_at_precision='approximate' if row.get('published_at_precision')=='approximate' else 'source',
        collected_at=iso(now),likes=row.get('likes') if type(row.get('likes')) is int else None,
        sentiment='unknown',collection_status='partial')


class Comments:
    def __init__(self,db,*,sqlite=False,clock=time.time):
        self.db,self.sqlite,self.clock=db,sqlite,clock
        self.synced={};self.published=None;self.last_publish=0;self.page_cache={};self.running=None;self.next_scan=0
        self.sql('''CREATE TABLE IF NOT EXISTS comment_jobs (
            id TEXT PRIMARY KEY, post TEXT NOT NULL, cursor TEXT, status TEXT NOT NULL,
            reason TEXT NOT NULL, due DOUBLE PRECISION NOT NULL, checked DOUBLE PRECISION NOT NULL DEFAULT 0,
            failures INTEGER NOT NULL DEFAULT 0, enabled INTEGER NOT NULL DEFAULT 1)''')
        self.sql('CREATE INDEX IF NOT EXISTS comment_due ON comment_jobs(enabled,due,checked)')
        platform_expr="json_extract(post,'$.platform')" if self.sqlite else "(post::jsonb->>'platform')"
        date_expr="json_extract(post,'$.published_at')" if self.sqlite else "(post::jsonb->>'published_at')"
        # Avoid a full archive scan for every platform on each comment turn.
        self.sql(f'''CREATE INDEX IF NOT EXISTS comment_platform_turn
            ON comment_jobs ({platform_expr},enabled,due,checked,{date_expr} DESC)''')
        self.sql('''CREATE TABLE IF NOT EXISTS collected_comments (
            id TEXT PRIMARY KEY, post_id TEXT NOT NULL, platform TEXT NOT NULL, source_id TEXT NOT NULL,
            published_at TEXT, collected_at TEXT NOT NULL, payload TEXT NOT NULL)''')
        self.sql('CREATE INDEX IF NOT EXISTS comment_post ON collected_comments(post_id)')
        self.sql('CREATE INDEX IF NOT EXISTS comment_platform_date ON collected_comments(platform,published_at)')
        self.sql('CREATE INDEX IF NOT EXISTS comment_source ON collected_comments(source_id,collected_at)')
        self.sql('''CREATE TABLE IF NOT EXISTS comment_versions (
            id TEXT NOT NULL, sha TEXT NOT NULL, observed_at TEXT NOT NULL, payload TEXT NOT NULL,
            PRIMARY KEY(id,sha))''')
        self.sql('''CREATE TABLE IF NOT EXISTS comment_circuits (
            platform TEXT PRIMARY KEY, until_at DOUBLE PRECISION NOT NULL)''')
        self.sql('CREATE TABLE IF NOT EXISTS comment_turns (platform TEXT PRIMARY KEY, reserved DOUBLE PRECISION NOT NULL)')
        self.sql('''CREATE TABLE IF NOT EXISTS comment_activity (
            id TEXT PRIMARY KEY, last_new DOUBLE PRECISION NOT NULL DEFAULT 0,
            quiet_scans INTEGER NOT NULL DEFAULT 0, new_count INTEGER NOT NULL DEFAULT 0)''')
        self.sql('CREATE TABLE IF NOT EXISTS comment_lane_turns (platform TEXT PRIMARY KEY, turns INTEGER NOT NULL)')
        # One-time upgrade of jobs previously excluded before any public request.
        for platform,reason in [('telegram','telegram_user_session_required'),*[(p,'comment_adapter_not_available') for p in ('threads','x','vk','ok','max')]]:
            self.sql("UPDATE comment_jobs SET status='pending',reason='',checked=0,due=? WHERE reason=? AND post LIKE ?",
                (self.clock(),reason,'%"platform":"'+platform+'"%'))
        self.sql("DELETE FROM comment_circuits WHERE platform='telegram' AND NOT EXISTS (SELECT 1 FROM comment_jobs WHERE post LIKE ? AND (status='rate_limited' OR (status='auth_required' AND reason!='telegram_user_session_required')))",
            ('%"platform":"telegram"%',))
        # Repair only circuits caused by an absent public excerpt, never a real
        # login gate or rate limit. A single page cannot establish platform access.
        platform_expr="json_extract(j.post,'$.platform')" if self.sqlite else "j.post::jsonb->>'platform'"
        self.sql(f'''DELETE FROM comment_circuits WHERE platform IN ('facebook','instagram','tiktok')
            AND EXISTS (SELECT 1 FROM comment_jobs j WHERE {platform_expr}=comment_circuits.platform
                AND j.reason='public_comments_not_exposed')
            AND NOT EXISTS (SELECT 1 FROM comment_jobs j WHERE {platform_expr}=comment_circuits.platform
                AND (j.status='rate_limited' OR (j.status='auth_required' AND j.reason!='public_comments_not_exposed')))''')

    def sql(self,query,args=()):return self.db.execute(query if self.sqlite else query.replace('?','%s'),args)
    def transaction(self):return self.db if self.sqlite else self.db.transaction()

    def upsert(self,post,rows,now,status='partial'):
        changed=0
        for raw in rows:
            value=normalize(post,raw,now)
            if not value:continue
            value['collection_status']='collected' if status=='success' else 'partial'
            old=self.sql('SELECT payload FROM collected_comments WHERE id=?',(value['id'],)).fetchone()
            if old:
                previous=json.loads(old['payload']);value['collected_at']=previous['collected_at']
                if all(previous.get(k)==value.get(k) for k in ('text','platform','published_at')) and previous.get('analysis'):
                    value['analysis']=previous['analysis'];value['sentiment']=previous.get('sentiment','unknown')
                if value==previous:continue
            payload=encoded(value).decode();digest=hashlib.sha256(payload.encode()).hexdigest()
            self.sql('''INSERT INTO collected_comments VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                published_at=excluded.published_at,payload=excluded.payload''',
                (value['id'],value['post_id'],value['platform'],value['source_id'],value['published_at'],value['collected_at'],payload))
            self.sql('INSERT INTO comment_versions VALUES(?,?,?,?) ON CONFLICT(id,sha) DO NOTHING',(value['id'],digest,iso(now),payload))
            changed+=1
        return changed

    def sync(self,posts,sources=None):
        now=self.clock();active=None if sources is None else {s['id'] for s in sources if s.get('enabled',True)}
        fingerprints={}
        iterator=iter(posts)
        # Bound both libpq's result buffer and serialized upsert payloads. Keep
        # the transaction and cache acknowledgement at their original boundary.
        with self.transaction():
            while batch := list(islice(iterator,128)):
                ids=[post['id'] for post in batch if post.get('id')]
                existing={}
                if ids:
                    marks=','.join('?' for _ in ids)
                    existing={r['id']:json.loads(r['post']).get('url') for r in
                        self.sql(f'SELECT id,post FROM comment_jobs WHERE id IN ({marks})',ids).fetchall()}
                pending=[];embedded=[]
                for post in batch:
                    if post.get('sample') or not post.get('id') or not post.get('url'):continue
                    enabled=int(active is None or post.get('source_id') in active)
                    fields=('id','platform','source_id','source_name','url','text','title','published_at','origin','public_comments','instagram_comments','platform_post_id','conversation_id')
                    value={k:post[k] for k in fields if k in post}
                    value['text']=str(value.get('text',''))[:600]
                    fingerprint=hashlib.sha256(encoded([value,enabled])).hexdigest()
                    if self.synced.get(post['id'])==fingerprint:continue
                    previous_url=existing.get(post['id'])
                    if previous_url and previous_url!=post['url']:
                        self.sql("UPDATE comment_jobs SET cursor=NULL,due=?,status='pending',failures=0 WHERE id=?",(now,post['id']))
                        self.sql('DELETE FROM comment_activity WHERE id=?',(post['id'],))
                    status,reason='pending',''
                    platform=post['platform']
                    if platform not in PLATFORMS:
                        status,reason='unsupported','comment_adapter_not_available'
                    pending.append((post['id'],encoded(value).decode(),status,reason,now if status=='pending' else now+86400,enabled,0 if status=='pending' else now))
                    for field in ('public_comments','instagram_comments'):
                        if value.get(field):embedded.append((value,value[field]))
                    fingerprints[post['id']]=fingerprint
                query='''INSERT INTO comment_jobs(id,post,status,reason,due,enabled,checked) VALUES(?,?,?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET post=excluded.post,enabled=excluded.enabled'''
                if pending:
                    cursor=self.db.cursor()
                    try:cursor.executemany(query if self.sqlite else query.replace('?','%s'),pending)
                    finally:cursor.close()
                for value,rows in embedded:self.upsert(value,rows,now)
        self.synced.update(fingerprints)
        while len(self.synced)>4096:self.synced.pop(next(iter(self.synced)))

    def due(self,limit=4):
        turns={r['platform']:r['reserved'] for r in self.sql('SELECT * FROM comment_turns').fetchall()}
        platforms=sorted(PLATFORMS,key=lambda p:turns.get(p,0))
        date="json_extract(j.post,'$.published_at')" if self.sqlite else "j.post::jsonb->>'published_at'"
        platform_expr="json_extract(j.post,'$.platform')" if self.sqlite else "j.post::jsonb->>'platform'"
        lanes={r['platform']:r['turns'] for r in self.sql('SELECT * FROM comment_lane_turns').fetchall()}
        found=[]
        for platform in platforms:
            # Reserve turns for new posts, unfinished pages, active discussions,
            # and oldest overdue work. An endless backfill cannot starve live work.
            lane=lanes.get(platform,0)%4
            preference=("j.checked=0", "j.cursor IS NOT NULL", "a.last_new>"+str(self.clock()-3600), "1=1")[lane]
            order=(date+' DESC NULLS LAST,j.due,j.id') if lane==0 else 'j.due,j.checked,j.id'
            rows=self.sql(f'''SELECT j.* FROM comment_jobs j LEFT JOIN comment_activity a ON a.id=j.id
                LEFT JOIN comment_circuits c ON c.platform={platform_expr}
                WHERE j.enabled=1 AND j.due<=? AND {platform_expr}=? AND (c.until_at IS NULL OR c.until_at<=?)
                ORDER BY CASE WHEN {preference} THEN 0 ELSE 1 END,{order} LIMIT ?''',
                (self.clock(),platform,self.clock(),limit)).fetchall()
            if rows:found.append(rows)
        # One turn per platform before additional jobs from the same platform.
        return ([rows[0] for rows in found]+[row for rows in found for row in rows[1:]])[:limit]

    def acknowledge(self,job,answer):
        now=self.clock();post=json.loads(job['post']);status=answer.get('status','failed')
        if status not in STATUSES:raise ValueError('invalid_comment_status')
        published=timestamp(post.get('published_at'))
        age=now-datetime.fromisoformat(published).timestamp() if published else 86400*8
        cursor=answer.get('cursor')
        if cursor and job['cursor'] and cursor==json.loads(job['cursor']) and status in ('success','partial'):
            answer={**answer,'status':'failed','reason':'pagination_stalled'};status='failed'
        failures=job['failures']+1 if status in ('failed','rate_limited') else 0
        with self.transaction():
            current=self.sql('SELECT * FROM comment_jobs WHERE id=?',(job['id'],)).fetchone()
            # Ignore delayed results for a changed URL, cursor, or disabled source.
            if not current or not current['enabled'] or any(current[k]!=job[k] for k in ('post','cursor','checked','status')):return 0
            before=self.sql('SELECT COUNT(*) AS n FROM collected_comments WHERE post_id=?',(post['id'],)).fetchone()['n']
            changed=self.upsert(post,answer.get('rows',[]),now,status)
            after=self.sql('SELECT COUNT(*) AS n FROM collected_comments WHERE post_id=?',(post['id'],)).fetchone()['n']
            previous=self.sql('SELECT * FROM comment_activity WHERE id=?',(post['id'],)).fetchone()
            activity=dict(previous) if previous else dict(last_new=0,quiet_scans=0,new_count=0)
            activity['new_count']=after-before
            if after>before:activity.update(last_new=now,quiet_scans=0)
            elif status in ('success','partial') and not cursor:activity['quiet_scans']+=1
            self.sql('''INSERT INTO comment_activity VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                last_new=excluded.last_new,quiet_scans=excluded.quiet_scans,new_count=excluded.new_count''',
                (post['id'],activity['last_new'],activity['quiet_scans'],activity['new_count']))
            wait=delay(age,activity,now,status,failures,cursor,answer.get('retry_after'))
            if failures:wait+=random.uniform(0,30)
            # Access errors preserve continuation; successful full scans reset it.
            next_cursor=json.dumps(cursor) if cursor else None
            if status not in ('success','partial'):next_cursor=job['cursor']
            self.sql('UPDATE comment_jobs SET cursor=?,status=?,reason=?,due=?,checked=?,failures=? WHERE id=?',
                (next_cursor,status,str(answer.get('reason',''))[:160],now+wait,now,failures,job['id']))
            if status=='rate_limited' or retry_seconds(answer.get('retry_after')) or (status=='auth_required' and answer.get('reason')!='public_comments_not_exposed'):
                self.sql('''INSERT INTO comment_circuits VALUES(?,?) ON CONFLICT(platform) DO UPDATE SET
                    until_at=CASE WHEN comment_circuits.until_at>excluded.until_at THEN comment_circuits.until_at ELSE excluded.until_at END''',
                    (post['platform'],now+wait))
            self.sql('INSERT INTO comment_lane_turns VALUES(?,1) ON CONFLICT(platform) DO UPDATE SET turns=comment_lane_turns.turns+1',(post['platform'],))
        return changed

    def tick(self,emit):
        # A child overlaps the news cycle. Never wait for comment networking here.
        if self.running:
            proc,job,started,output=self.running
            if proc.poll() is None and time.monotonic()-started<45:return
            if proc.poll() is None:
                terminate(proc);proc.communicate();answer=dict(status='failed',reason='adapter_timeout')
            else:
                proc.communicate();output.seek(0);out=output.read(4*1024**2+1)
                try:answer=json.loads(out) if proc.returncode==0 and len(out)<4*1024**2 else dict(status='failed',reason='adapter_process_failed')
                except ValueError:answer=dict(status='failed',reason='invalid_adapter_output')
            if not isinstance(answer,dict) or answer.get('status') not in STATUSES or not isinstance(answer.get('rows',[]),list):
                answer=dict(status='failed',reason='invalid_adapter_output')
            output.close();self.running=None
            changed=self.acknowledge(job,answer)
            emit('comments_checked',checked=1,changed=changed,status=answer.get('status'),reason=answer.get('reason'),platform=json.loads(job['post'])['platform'])
        if self.clock()<self.next_scan:return
        self.next_scan=self.clock()+15
        for job in self.due(16):
            post=json.loads(job['post'])
            circuit=self.sql('SELECT until_at FROM comment_circuits WHERE platform=?',(post['platform'],)).fetchone()
            if circuit and circuit['until_at']>self.clock():
                self.sql('UPDATE comment_jobs SET due=? WHERE id=?',(circuit['until_at'],job['id']))
                continue
            if post['platform'] not in PLATFORMS:
                answer=dict(rows=[],status='unsupported',reason='comment_adapter_not_available')
                self.acknowledge(job,answer)
            else:
                allowed={'PATH','HOME','LANG','SSL_CERT_FILE','ARGUS_YOUTUBE_API_KEY','YOUTUBE_API_KEY',
                    'ARGUS_TELEGRAM_API_ID','ARGUS_TELEGRAM_API_HASH','ARGUS_TELEGRAM_SESSION'}
                from comment_official import ENVIRONMENT
                allowed.update(ENVIRONMENT)
                if os.getenv('ARGUS_ANONYMOUS_ONLY')=='1':
                    allowed={'PATH','HOME','LANG','SSL_CERT_FILE'}
                env={k:v for k,v in os.environ.items() if k in allowed}
                try:
                    output=tempfile.TemporaryFile()
                    proc=subprocess.Popen([sys.executable,str(Path(__file__).resolve().parents[1]/'collect/comment_adapters.py')],
                        stdin=subprocess.PIPE,stdout=output,stderr=subprocess.DEVNULL,env=env,start_new_session=True)
                    proc.stdin.write(encoded({'post':post,'cursor':json.loads(job['cursor']) if job['cursor'] else None}))
                    proc.stdin.close();proc.stdin=None
                    self.running=(proc,job,time.monotonic(),output)
                    self.sql('INSERT INTO comment_turns VALUES(?,?) ON CONFLICT(platform) DO UPDATE SET reserved=excluded.reserved',(post['platform'],self.clock()))
                    emit('comments_started',platform=post['platform'])
                except (ValueError,OSError):
                    if 'output' in locals():output.close()
                    self.acknowledge(job,dict(status='failed',reason='adapter_process_failed'))
                break

    def close(self):
        if self.running:
            proc,_,_,output=self.running
            if proc.poll() is None:terminate(proc)
            proc.communicate();output.close();self.running=None

    def metrics(self):
        counts={r['status']:r['n'] for r in self.sql('SELECT status,COUNT(*) AS n FROM comment_jobs GROUP BY status').fetchall()}
        total=self.sql('SELECT COUNT(*) AS n FROM collected_comments').fetchone()['n']
        recent={str(seconds):self.sql('SELECT COUNT(*) AS n FROM collected_comments WHERE collected_at>=?',(iso(self.clock()-seconds),)).fetchone()['n'] for seconds in (300,3600,86400)}
        platform_expr="json_extract(post,'$.platform')" if self.sqlite else "post::jsonb->>'platform'"
        health={}
        for row in self.sql(f'''SELECT {platform_expr} AS platform,status,COUNT(*) AS n,
                MIN(due) AS earliest,MAX(checked) AS checked FROM comment_jobs WHERE enabled=1
                GROUP BY {platform_expr},status''').fetchall():
            item=health.setdefault(row['platform'],dict(statuses={},last_checked=0,oldest_due=None))
            item['statuses'][row['status']]=row['n']
            item['last_checked']=max(item['last_checked'],row['checked'])
            item['oldest_due']=min(item['oldest_due'] or row['earliest'],row['earliest'])
        for row in self.sql('SELECT * FROM comment_circuits WHERE until_at>?',(self.clock(),)).fetchall():
            health.setdefault(row['platform'],{})['paused_until']=row['until_at']
        overdue=self.sql('SELECT COUNT(*) AS n,MIN(due) AS oldest FROM comment_jobs WHERE enabled=1 AND due<=?',(self.clock(),)).fetchone()
        return dict(comments=total,jobs=sum(counts.values()),statuses=counts,recent=recent,
            platforms=health,overdue=overdue['n'],oldest_overdue_seconds=max(0,self.clock()-(overdue['oldest'] or self.clock())))

    def publish(self,store):
        # This is a read model: PostgreSQL remains the transactional authority.
        now=self.clock()
        if now-self.last_publish<60:return False
        # Chronological pagination shifted every following page on each insert,
        # uploading and reading back the entire discussion archive each cycle.
        # Stable hash partitions confine that work to the affected partition.
        pages=[];sources={}
        def partitions():
            buckets=self.sql('SELECT DISTINCT substr(id,1,1) AS bucket FROM collected_comments ORDER BY bucket').fetchall()
            for bucket in buckets:
                last=''
                while records:=self.sql('''SELECT id,payload FROM collected_comments
                    WHERE substr(id,1,1)=? AND id>? ORDER BY id LIMIT 100''',(bucket['bucket'],last)).fetchall():
                    yield records
                    last=records[-1]['id']
        for records in partitions():
            rows=[json.loads(r['payload']) for r in records];raw=encoded({'format':'argus-comments-page-v1','items':rows})
            sources.update({r['source_id']:r.get('source_name') or r['source_id'] for r in rows if r.get('source_id')})
            digest=hashlib.sha256(raw).hexdigest();name='comments-'+digest+'.json'
            file=self.page_cache.get(digest)
            if file is None:
                file=store.put(name,raw)
                if store.read_bytes(name,len(raw))!=raw:raise ValueError('comments_page_readback')
                self.page_cache[digest]=file
            dates=[r['published_at'] or r['collected_at'] for r in rows]
            pages.append(dict(id=file,sha256=digest,size=len(raw),count=len(rows),platforms=sorted({r['platform'] for r in rows}),
                sources=sorted({r['source_id'] for r in rows}),posts=sorted({r['post_id'] for r in rows}),first=min(dates),last=max(dates)))
        live_hashes={page['sha256'] for page in pages}
        self.page_cache={sha:file for sha,file in self.page_cache.items() if sha in live_hashes}
        platforms=[]
        platform_expr="json_extract(post,'$.platform')" if self.sqlite else "post::jsonb->>'platform'"
        # One newest status per platform, rather than fetching tens of thousands
        # of serialized post bodies merely to discard their duplicate platforms.
        query=f'''SELECT post,status,reason,checked,due FROM (
            SELECT post,status,reason,checked,due,
              ROW_NUMBER() OVER (PARTITION BY {platform_expr} ORDER BY checked DESC,id) AS rank
            FROM comment_jobs WHERE checked>0) ranked WHERE rank=1'''
        for row in self.sql(query).fetchall():
            p=json.loads(row['post'])['platform']
            if p not in {r['platform'] for r in platforms}:platforms.append(dict(platform=p,status=row['status'],reason=row['reason'],checked_at=iso(row['checked']),next_check_at=iso(row['due'])))
        value=dict(format='argus-comments-catalog-v1',pages=pages,metrics=self.metrics(),platforms=platforms,sources=sources)
        revision=hashlib.sha256(encoded(value)).hexdigest()
        if revision==self.published:self.last_publish=now;return False
        value.update(updated_at=iso(now),revision=revision)
        store.json('comments-catalog.json',value,mutable=True)
        if store.read_json('comments-catalog.json')!=value:raise ValueError('comments_catalog_readback')
        self.published=revision;self.last_publish=now
        return True
