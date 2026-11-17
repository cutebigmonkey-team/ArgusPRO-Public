"""Private PostgreSQL-backed ARGUS candidate inbox.

Does NOT run migrations, expose HTTP, use the archive, or run on GitHub runners.
Connections must be supplied by a trusted server using a dedicated inbox role.
"""
import hashlib
import secrets
from datetime import datetime, timezone, timedelta
from envelope import verify

SCHEMA = 'argus_ingest'

class PostgresQueue:
    def __init__(self, connection):
        self.conn = connection

    def submit(self, body, *, sha256, signature, key, now=None):
        payload = verify(body, sha256=sha256, signature=signature, key=key, now=now)
        moment = datetime.now(timezone.utc) if now is None else now
        run_id = payload['run_id']
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute('''INSERT INTO argus_ingest.batches
                    (run_id,sha256,body,state,received_at)
                    VALUES (%s,%s,%s,'pending',%s)
                    ON CONFLICT (run_id) DO NOTHING''',
                    (run_id,sha256,body,moment))
                inserted = cur.rowcount == 1
                if inserted:
                    return 'queued'
                cur.execute('SELECT sha256,body,state FROM argus_ingest.batches WHERE run_id=%s FOR UPDATE',(run_id,))
                prior = cur.fetchone()
                if prior is None or prior[0] != sha256 or bytes(prior[1]) != body:
                    raise ValueError('run_id_digest_conflict')
                return 'already_acknowledged' if prior[2] == 'acknowledged' else 'already_queued'

    def claim(self, *, lease_seconds=120, now=None):
        if type(lease_seconds) is not int or not 5 <= lease_seconds <= 3600:
            raise ValueError('invalid_lease_seconds')
        moment = datetime.now(timezone.utc) if now is None else now
        token = secrets.token_hex(32)
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute('''WITH candidate AS (
                    SELECT run_id FROM argus_ingest.batches
                    WHERE state='pending' OR (state='leased' AND lease_until <= %s)
                    ORDER BY received_at,run_id FOR UPDATE SKIP LOCKED LIMIT 1
                ) UPDATE argus_ingest.batches AS b
                  SET state='leased', lease_token=%s,lease_until=%s,
                      attempts=attempts+1
                  FROM candidate WHERE b.run_id=candidate.run_id
                  RETURNING b.run_id,b.sha256,b.body,b.lease_until''',
                  (moment,token,moment+timedelta(seconds=lease_seconds)))
                row = cur.fetchone()
                return None if row is None else {
                    'run_id':row[0], 'sha256':row[1], 'body':bytes(row[2]),
                    'lease_token':token, 'lease_until':row[3]}

    def acknowledge(self, run_id, lease_token, *, archive_verified=False, now=None):
        # The caller MUST be the existing fenced writer after independent archive readback.
        # An assertion is not proof: do not expose this method via HTTP.
        if archive_verified is not True:
            raise ValueError('archive_readback_required')
        moment = datetime.now(timezone.utc) if now is None else now
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute('''UPDATE argus_ingest.batches SET state='acknowledged',
                    lease_token=NULL,lease_until=NULL,acknowledged_at=%s
                    WHERE run_id=%s AND state='leased' AND lease_token=%s
                    AND lease_until>%s''',(moment,run_id,lease_token,moment))
                if cur.rowcount != 1:
                    raise ValueError('invalid_or_expired_lease')

    def release(self, run_id, lease_token, *, now=None):
        moment = datetime.now(timezone.utc) if now is None else now
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute('''UPDATE argus_ingest.batches SET state='pending',
                    lease_token=NULL, lease_until=NULL
                    WHERE run_id=%s AND state='leased' AND lease_token=%s
                    AND lease_until>%s''',(run_id,lease_token,moment))
                if cur.rowcount != 1:
                    raise ValueError('invalid_or_expired_lease')

    def metrics(self, *, now=None):
        moment = datetime.now(timezone.utc) if now is None else now
        with self.conn.cursor() as cur:
            cur.execute('SELECT state,count(*),coalesce(sum(octet_length(body)),0) FROM argus_ingest.batches GROUP BY state')
            statuses = {state:{'count':n,'bytes':size} for state,n,size in cur.fetchall()}
            cur.execute("SELECT count(*) FROM argus_ingest.batches WHERE state='leased' AND lease_until <= %s",(moment,))
            expired = cur.fetchone()[0]
        return {'states':statuses,'expired_leases':expired,
                'unacknowledged':sum(v['count'] for k,v in statuses.items() if k!='acknowledged')}
