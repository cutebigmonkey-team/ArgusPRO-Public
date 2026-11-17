"""Local-only durable inbox prototype for verified ARGUS envelopes.

No network server and no connection to the production archive. An acknowledged
item means the caller *asserts* an independent archive readback succeeded;
this class does not claim to perform archive delivery or validation itself.
"""
import os
import secrets
import sqlite3
import time
from pathlib import Path

from envelope import verify

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
 run_id TEXT PRIMARY KEY,
 sha256 TEXT NOT NULL,
 body BLOB NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('pending','leased','acknowledged')),
 received_at INTEGER NOT NULL,
 attempts INTEGER NOT NULL DEFAULT 0,
 lease_token TEXT,
 lease_until INTEGER,
 acknowledged_at INTEGER,
 CHECK ((state='leased' AND lease_token IS NOT NULL AND lease_until IS NOT NULL)
     OR (state!='leased' AND lease_token IS NULL AND lease_until IS NULL))
);
CREATE INDEX IF NOT EXISTS batches_available ON batches(state,lease_until,received_at);
"""


class DurableQueue:
    def __init__(self, database):
        self.path = Path(database)
        if self.path.exists() and not self.path.is_file():
            raise ValueError('queue_path_is_not_file')
        # This is a local prototype. Caller must choose a trusted, private dir.
        self.db = sqlite3.connect(str(self.path), timeout=10, isolation_level=None)
        try:
            self.db.execute('PRAGMA busy_timeout=10000')
            self.db.execute('PRAGMA journal_mode=WAL')
            self.db.execute('PRAGMA synchronous=FULL')
            self.db.executescript(SCHEMA)
            if self.path.exists():
                os.chmod(self.path, 0o600)
        except Exception:
            self.db.close()
            raise

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def submit(self, body, *, sha256, signature, key, now=None):
        """Verify authenticity before inserting, idempotently, in one transaction."""
        payload = verify(body, sha256=sha256, signature=signature, key=key, now=now)
        run_id = payload['run_id']
        received = int(time.time() if now is None else now.timestamp())
        self.db.execute('BEGIN IMMEDIATE')
        try:
            old = self.db.execute('SELECT sha256,body,state FROM batches WHERE run_id=?', (run_id,)).fetchone()
            if old:
                if old[0] != sha256 or bytes(old[1]) != body:
                    raise ValueError('run_id_digest_conflict')
                result = 'already_acknowledged' if old[2] == 'acknowledged' else 'already_queued'
            else:
                self.db.execute('INSERT INTO batches(run_id,sha256,body,state,received_at) VALUES (?,?,?,?,?)',
                                (run_id,sha256,sqlite3.Binary(body),'pending',received))
                result = 'queued'
            self.db.execute('COMMIT')
            return result
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def claim(self, *, lease_seconds=120, now=None):
        """Lease one pending/expired batch; token fences old consumers."""
        if type(lease_seconds) is not int or not 5 <= lease_seconds <= 3600:
            raise ValueError('invalid_lease_seconds')
        current = int(time.time() if now is None else now)
        token = secrets.token_hex(32)
        self.db.execute('BEGIN IMMEDIATE')
        try:
            row = self.db.execute('''SELECT run_id,sha256,body FROM batches
                WHERE state='pending' OR (state='leased' AND lease_until<=?)
                ORDER BY received_at,run_id LIMIT 1''', (current,)).fetchone()
            if row is None:
                self.db.execute('COMMIT')
                return None
            self.db.execute('''UPDATE batches SET state='leased',lease_token=?,lease_until=?,attempts=attempts+1
                WHERE run_id=?''', (token,current+lease_seconds,row[0]))
            self.db.execute('COMMIT')
            return {'run_id':row[0], 'sha256':row[1], 'body':bytes(row[2]),
                    'lease_token':token, 'lease_until':current+lease_seconds}
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def acknowledge(self, run_id, lease_token, *, archive_verified=False, now=None):
        """Only the current lease can ack; caller must have independently verified archive readback."""
        if archive_verified is not True:
            raise ValueError('archive_readback_required')
        current = int(time.time() if now is None else now)
        result = self.db.execute('''UPDATE batches SET state='acknowledged', lease_token=NULL,
                lease_until=NULL, acknowledged_at=? WHERE run_id=? AND state='leased'
                AND lease_token=? AND lease_until>?''', (current,run_id,lease_token,current))
        if result.rowcount != 1:
            raise ValueError('invalid_or_expired_lease')

    def release(self, run_id, lease_token, *, now=None):
        current = int(time.time() if now is None else now)
        result = self.db.execute('''UPDATE batches SET state='pending',lease_token=NULL,lease_until=NULL
                WHERE run_id=? AND state='leased' AND lease_token=? AND lease_until>?''',
                (run_id,lease_token,current))
        if result.rowcount != 1:
            raise ValueError('invalid_or_expired_lease')

    def metrics(self, *, now=None):
        current = int(time.time() if now is None else now)
        rows = self.db.execute('SELECT state,COUNT(*),COALESCE(SUM(length(body)),0) FROM batches GROUP BY state')
        statuses = {state:{'count':n,'bytes':size} for state,n,size in rows}
        expired = self.db.execute("SELECT COUNT(*) FROM batches WHERE state='leased' AND lease_until<=?",(current,)).fetchone()[0]
        return {'states': statuses, 'expired_leases': expired,
                'unacknowledged':sum(v['count'] for k,v in statuses.items() if k!='acknowledged')}
