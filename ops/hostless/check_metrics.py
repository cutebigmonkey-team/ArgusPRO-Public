"""Compact durable check ledger; unknown historical windows remain null."""
import json
from source_health import effective_status,EMPTY,SKIPPED

WINDOWS={'5m':300,'30m':1800,'1h':3600,'24h':86400}


def initialize(sql,now):
    sql('''CREATE TABLE IF NOT EXISTS runtime_check_events (
        source_id TEXT NOT NULL, token TEXT NOT NULL, platform TEXT NOT NULL,
        checked_at DOUBLE PRECISION NOT NULL, status TEXT NOT NULL,
        completed INTEGER NOT NULL, verified INTEGER NOT NULL, inserted INTEGER,
        duplicates INTEGER, rejected INTEGER, seconds DOUBLE PRECISION,
        PRIMARY KEY(source_id,token))''')
    sql('CREATE INDEX IF NOT EXISTS check_events_time ON runtime_check_events(checked_at)')
    sql('CREATE TABLE IF NOT EXISTS runtime_measurement (id INTEGER PRIMARY KEY,since_at DOUBLE PRECISION NOT NULL)')
    sql('CREATE TABLE IF NOT EXISTS runtime_inserted_posts(id TEXT PRIMARY KEY,platform TEXT NOT NULL,archived_at DOUBLE PRECISION NOT NULL)')
    sql('CREATE INDEX IF NOT EXISTS inserted_posts_time ON runtime_inserted_posts(archived_at)')
    sql('INSERT INTO runtime_measurement VALUES(1,?) ON CONFLICT(id) DO NOTHING',(now,))


def record(sql,row,token,checked):
    status=effective_status(row)
    if status in SKIPPED:return
    delivery=row.get('delivery') or {}
    verified=delivery.get('saved_ids')
    inserted=delivery.get('inserted_ids')
    duplicate=delivery.get('previously_saved_ids')
    rejected=len(row['rejections']) if isinstance(row.get('rejections'),list) else None
    sql('''INSERT INTO runtime_check_events VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(source_id,token)
        DO UPDATE SET status=excluded.status,completed=excluded.completed,verified=excluded.verified,
        inserted=excluded.inserted,duplicates=excluded.duplicates,rejected=excluded.rejected,seconds=excluded.seconds''',
        (row['source_id'],token,row['platform'],checked,status,int(status in EMPTY|{'ok'}),
         int(bool(verified)),inserted,duplicate,rejected,row.get('seconds')))


def summarize(sql,now):
    since=sql('SELECT since_at FROM runtime_measurement WHERE id=1').fetchone()['since_at']
    # Bound both retention and storage. Once capped, older windows are explicitly incomplete.
    threshold=sql('SELECT checked_at FROM runtime_check_events ORDER BY checked_at DESC LIMIT 1 OFFSET 99999').fetchone()
    post_threshold=sql('SELECT archived_at FROM runtime_inserted_posts ORDER BY archived_at DESC LIMIT 1 OFFSET 99999').fetchone()
    floor=max(now-86400,threshold['checked_at'] if threshold else since,
              post_threshold['archived_at'] if post_threshold else since)
    sql('DELETE FROM runtime_check_events WHERE checked_at<?',(floor,))
    sql('DELETE FROM runtime_inserted_posts WHERE archived_at<?',(floor,))
    if floor>since:
        sql('UPDATE runtime_measurement SET since_at=? WHERE id=1',(floor,));since=floor
    result={}
    for label,seconds in WINDOWS.items():
        complete=since<=now-seconds
        rows=sql('''SELECT platform,COUNT(*) AS attempted,SUM(completed) AS completed,
            SUM(verified) AS content_verified,SUM(inserted) AS inserted,SUM(duplicates) AS duplicates,
            SUM(rejected) AS rejected,COUNT(inserted) AS inserted_measured,
            COUNT(duplicates) AS duplicates_measured,COUNT(rejected) AS rejected_measured,
            SUM(CASE WHEN status='timeout' THEN 1 ELSE 0 END) AS timeouts,
            SUM(CASE WHEN status IN ('http_429','rate_limited','gateway_rate_limited') THEN 1 ELSE 0 END) AS rate_limits,
            SUM(CASE WHEN status IN ('robots_denied','login_required','http_401','http_403','challenge_required','unsupported') THEN 1 ELSE 0 END) AS access_restrictions,
            SUM(CASE WHEN status IN ('extraction_failed','unverified_markup','parsing_error') THEN 1 ELSE 0 END) AS parser_errors
            FROM runtime_check_events WHERE checked_at>? AND checked_at<=? GROUP BY platform''',(now-seconds,now)).fetchall()
        inserted={row['platform']:row['n'] for row in sql('''SELECT platform,COUNT(*) AS n FROM runtime_inserted_posts
            WHERE archived_at>? AND archived_at<=? GROUP BY platform''',(now-seconds,now)).fetchall()}
        platforms={}
        for raw in rows:
            row=dict(raw);platform=row.pop('platform')
            for field in ('inserted','duplicates','rejected'):
                measured=row.pop(field+'_measured')
                if measured!=row['attempted']:row[field]=None
            platforms[platform]={'observed':row,'complete_window':complete,
                'unique_durable_insertions_observed':inserted.get(platform,0),
                'api_visible':None,'client_rendered':None,'missing_publications':None}
        result[label]={'since':since,'complete_window':complete,'platforms':platforms,
                       'scope':'durably staged checks; insertion counts require archive acknowledgement; incomplete windows are not totals'}
    return result
