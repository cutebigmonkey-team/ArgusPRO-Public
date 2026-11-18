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
                    WHERE (state='pending' OR (state='leased' AND lease_until <= %s))
                      AND run_id ~ '^[0-9]{1,20}:[0-9]{1,2}:(fast|extended):[0-3]$'
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
        import re
        if not re.fullmatch(r'[0-9]{1,20}:[0-9]{1,2}:(fast|extended):[0-3]',run_id):raise ValueError('invalid_v1_queue_identity')
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
        import re
        if not re.fullmatch(r'[0-9]{1,20}:[0-9]{1,2}:(fast|extended):[0-3]',run_id):
            raise ValueError('invalid_v1_queue_identity')
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

    def _phase2_connection(self):
        # A nested savepoint is not a durable queue commit. Never return queued
        # from an outer transaction owned by the archive writer/caller.
        if self.conn is None or self.conn.autocommit is not True or self.conn.info.transaction_status != 0:
            raise ValueError('dedicated_autocommit_connection_required')

    def submit_part(self, body, *, jti_hash, expires_at, now=None, max_pending_bytes=64_000_000, max_source_bytes=8_000_000):
        """Private gateway boundary only; JWT verification precedes this method."""
        self._phase2_connection()
        import json
        from partition import decode, canonical, manifest
        part=decode(body,now=now);moment=datetime.now(timezone.utc) if now is None else now
        group=part['group_id'];bid=f"{group}:{part['index']}";sha=hashlib.sha256(body).hexdigest()
        producer=part['producer'];source=part['source'];checkpoint=part['checkpoint']
        slot=f"{producer['run_id']}:{producer['attempt']}:{producer['lane']}:{producer['shard']}:{source['source_id']}"
        import re
        if not isinstance(jti_hash,str) or not re.fullmatch('[0-9a-f]{64}',jti_hash):raise ValueError('invalid_replay_identity')
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                # Serialize quota accounting only, not collector execution/archive work.
                cur.execute('SELECT pg_advisory_xact_lock(%s)',(420021752,))
                cur.execute('SELECT batch_id,body_sha FROM argus_ingest.oidc_replays WHERE jti_hash=%s FOR UPDATE',(jti_hash,))
                replay=cur.fetchone()
                if replay and replay!=(bid,sha):raise ValueError('oidc_replay_conflict')
                cur.execute('SELECT group_id,manifest FROM argus_ingest.checkpoint_groups WHERE slot=%s FOR UPDATE',(slot,))
                old=cur.fetchone();expected=canonical(manifest(part))
                if old and (old[0]!=group or bytes(old[1])!=expected):raise ValueError('checkpoint_group_conflict')
                cur.execute('SELECT sha256,body,state FROM argus_ingest.batches WHERE run_id=%s FOR UPDATE',(bid,))
                prior=cur.fetchone()
                if prior and (prior[0]!=sha or bytes(prior[1])!=body):raise ValueError('run_id_digest_conflict')
                if not prior:
                    cur.execute('SELECT revision FROM argus_ingest.source_cursors WHERE source_id=%s',(source['source_id'],))
                    cursor_row=cur.fetchone()
                    if checkpoint['base_revision']!=(cursor_row[0] if cursor_row else 0):raise ValueError('stale_checkpoint_revision')
                    cur.execute("SELECT coalesce(sum(octet_length(body)),0) FROM argus_ingest.batches")
                    if cur.fetchone()[0]+len(body)>max_pending_bytes:raise ValueError('ingestion_backpressure')
                    cur.execute('''SELECT coalesce(sum(octet_length(b.body)),0) FROM argus_ingest.batches b
                        JOIN argus_ingest.checkpoint_groups g ON b.group_id=g.group_id
                        WHERE g.source_id=%s AND b.state<>'acknowledged' ''',(source['source_id'],))
                    if cur.fetchone()[0]+len(body)>max_source_bytes:raise ValueError('ingestion_backpressure')
                if not replay:
                    cur.execute('SELECT count(*) FROM argus_ingest.oidc_replays')
                    if cur.fetchone()[0]>=100000:raise ValueError('ingestion_backpressure')
                    cur.execute('INSERT INTO argus_ingest.oidc_replays VALUES (%s,%s,%s,%s)',(jti_hash,bid,sha,expires_at))
                if prior:return 'already_acknowledged' if prior[2]=='acknowledged' else 'already_queued'
                if not old:
                    cur.execute('''INSERT INTO argus_ingest.checkpoint_groups
                        (group_id,slot,manifest,source_id,base_revision,proposal,part_count)
                        VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s)''',
                        (group,slot,expected,source['source_id'],checkpoint['base_revision'],json.dumps(checkpoint['proposal']),len(part['parts'])))
                cur.execute('''INSERT INTO argus_ingest.batches
                    (run_id,sha256,body,state,received_at,available_at,group_id,part_index)
                    VALUES (%s,%s,%s,'pending',%s,%s,%s,%s)''',(bid,sha,body,moment,moment,group,part['index']))
                return 'queued'

    def claim_part(self, *, lease_seconds=120, max_attempts=5, now=None):
        if type(lease_seconds) is not int or not 5<=lease_seconds<=3600 or type(max_attempts) is not int or not 1<=max_attempts<=20:
            raise ValueError('invalid_lease_policy')
        self._phase2_connection()
        moment=datetime.now(timezone.utc) if now is None else now;token=secrets.token_hex(32)
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute('''UPDATE argus_ingest.batches SET state='dead',lease_token=NULL,lease_until=NULL,last_error='attempts_exhausted'
                    WHERE group_id IS NOT NULL AND attempts>=%s AND
                    (state='pending' OR (state='leased' AND lease_until<=%s))''',(max_attempts,moment))
                cur.execute('''WITH candidate AS (SELECT run_id FROM argus_ingest.batches
                    WHERE group_id IS NOT NULL AND attempts<%s AND
                      ((state='pending' AND available_at<=%s) OR (state='leased' AND lease_until<=%s))
                    ORDER BY available_at,received_at,run_id FOR UPDATE SKIP LOCKED LIMIT 1)
                    UPDATE argus_ingest.batches b SET state='leased',lease_token=%s,lease_until=%s,attempts=attempts+1
                    FROM candidate WHERE b.run_id=candidate.run_id
                    RETURNING b.run_id,b.sha256,b.body,b.attempts''',
                    (max_attempts,moment,moment,token,moment+timedelta(seconds=lease_seconds)))
                row=cur.fetchone()
                return None if row is None else dict(run_id=row[0],sha256=row[1],body=bytes(row[2]),attempts=row[3],lease_token=token)

    def check_part_lease(self, run_id, token, *, now=None):
        self._phase2_connection()
        moment=datetime.now(timezone.utc) if now is None else now
        with self.conn.cursor() as cur:
            cur.execute("SELECT 1 FROM argus_ingest.batches WHERE run_id=%s AND state='leased' AND lease_token=%s AND lease_until>%s",(run_id,token,moment))
            if cur.fetchone() is None:raise ValueError('invalid_or_expired_lease')

    def retry_part(self, run_id, token, *, error_code, max_attempts=5, now=None):
        if type(max_attempts) is not int or not 1<=max_attempts<=20:raise ValueError('invalid_retry_policy')
        self._phase2_connection()
        if error_code not in ('archive_unavailable','archive_readback_mismatch','writer_unavailable','consumer_failed'):
            raise ValueError('invalid_retry_code')
        moment=datetime.now(timezone.utc) if now is None else now
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute('''SELECT attempts FROM argus_ingest.batches WHERE run_id=%s
                    AND state='leased' AND lease_token=%s AND lease_until>%s FOR UPDATE''',(run_id,token,moment))
                row=cur.fetchone()
                if row is None:raise ValueError('invalid_or_expired_lease')
                delay=min(1800,5*2**min(row[0],8))+secrets.randbelow(6)
                cur.execute('''UPDATE argus_ingest.batches SET state=%s,available_at=%s,last_error=%s,
                    lease_token=NULL,lease_until=NULL WHERE run_id=%s''',
                    ('dead' if row[0]>=max_attempts else 'pending',moment+timedelta(seconds=delay),error_code,run_id))

    def acknowledge_part(self, run_id, token, *, verified_body_sha, now=None):
        """Trusted consumer only. Caller obtains digest from actual archive readback."""
        self._phase2_connection()
        import json
        moment=datetime.now(timezone.utc) if now is None else now
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute('''SELECT group_id,sha256 FROM argus_ingest.batches WHERE run_id=%s
                    AND state='leased' AND lease_token=%s AND lease_until>%s''',(run_id,token,moment))
                batch=cur.fetchone()
                if batch is None:raise ValueError('invalid_or_expired_lease')
                if batch[1]!=verified_body_sha:raise ValueError('archive_readback_required')
                cur.execute('SELECT source_id,base_revision,proposal,part_count,manifest,sequence FROM argus_ingest.checkpoint_groups WHERE group_id=%s FOR UPDATE',(batch[0],))
                source,revision,proposal,count,group_manifest,sequence=cur.fetchone()
                if now is None:moment=datetime.now(timezone.utc)
                cur.execute('''SELECT 1 FROM argus_ingest.batches WHERE run_id=%s
                    AND state='leased' AND lease_token=%s AND lease_until>%s FOR UPDATE''',(run_id,token,moment))
                if cur.fetchone() is None:raise ValueError('invalid_or_expired_lease')
                cur.execute("UPDATE argus_ingest.batches SET state='acknowledged',lease_token=NULL,lease_until=NULL,acknowledged_at=%s WHERE run_id=%s",(moment,run_id))
                cur.execute("SELECT count(*) FROM argus_ingest.batches WHERE group_id=%s AND state='acknowledged'",(batch[0],))
                if cur.fetchone()[0]!=count:return 'part_verified'
                # Reconstruct identities incrementally before any cursor CAS.
                # A manifest cannot claim more records than actually delivered.
                from partition import decode
                from envelope import _iso
                declared=json.loads(bytes(group_manifest));seen=set();total=0
                with self.conn.cursor(name='ingest_manifest_'+secrets.token_hex(8)) as records:
                    records.itersize=1
                    records.execute('SELECT part_index,body FROM argus_ingest.batches WHERE group_id=%s ORDER BY part_index',(batch[0],))
                    for part_index,raw_body in records:
                        raw_body=bytes(raw_body);raw=json.loads(raw_body)
                        part=decode(raw_body,now=_iso(raw['created_at']))
                        if part['group_id']!=batch[0] or part['index']!=part_index:
                            raise ValueError('checkpoint_manifest_mismatch')
                        for post in part['posts']:
                            if post['canonical_key'] in seen:raise ValueError('checkpoint_identity_collision')
                            seen.add(post['canonical_key']);total+=1
                if total!=declared['record_count']:raise ValueError('checkpoint_record_count_mismatch')
                state='delivered'
                cur.execute("SELECT 1 FROM argus_ingest.checkpoint_groups WHERE source_id=%s AND sequence<%s AND state='pending' LIMIT 1",(source,sequence))
                older_pending=cur.fetchone() is not None
                if proposal is not None and older_pending:state='cursor_conflict'
                if proposal is not None and not older_pending:
                    cur.execute('INSERT INTO argus_ingest.source_cursors(source_id) VALUES (%s) ON CONFLICT DO NOTHING',(source,))
                    cur.execute('''UPDATE argus_ingest.source_cursors SET revision=revision+1,checkpoint=%s::jsonb,verified_group=%s
                        WHERE source_id=%s AND revision=%s''',(json.dumps(proposal),batch[0],source,revision))
                    if cur.rowcount!=1:state='cursor_conflict'
                cur.execute('UPDATE argus_ingest.checkpoint_groups SET state=%s WHERE group_id=%s',(state,batch[0]))
                return state

    def recover_cursor(self, source_id):
        self._phase2_connection()
        with self.conn.cursor() as cur:
            cur.execute('SELECT revision,checkpoint,verified_group FROM argus_ingest.source_cursors WHERE source_id=%s',(source_id,))
            row=cur.fetchone()
            return {'revision':0,'checkpoint':None,'verified_group':None} if row is None else dict(revision=row[0],checkpoint=row[1],verified_group=row[2])
