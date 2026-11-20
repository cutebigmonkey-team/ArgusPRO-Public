"""Integration tests against an isolated CI PostgreSQL service only.

The test refuses any non-loopback/non-fixture database. Never target ARGUS production.
"""
import hashlib
import hmac
import os
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import unittest

try:
    import psycopg
    from psycopg.conninfo import conninfo_to_dict
except ImportError:
    psycopg = None

from envelope import SCHEMA, canonical
from pg_queue import PostgresQueue

KEY = b'P' * 32


def connection_string():
    dsn = os.environ.get('ARGUS_PG_INTEGRATION_DSN')
    if not dsn:
        raise unittest.SkipTest('No isolated integration database configured')
    if psycopg is None:
        raise RuntimeError('psycopg missing for PostgreSQL integration suite')
    parsed = conninfo_to_dict(dsn)
    if (parsed.get('dbname') != 'argus_ingest_ci'
            or parsed.get('user') != 'argus_ci'
            or parsed.get('host') not in ('localhost', '127.0.0.1')
            or parsed.get('port', '5432') != '5432'):
        raise RuntimeError('REFUSING_NON_ISOLATED_POSTGRES_TARGET')
    return dsn


def batch(run_number=1, *, content='synthetic test post'):
    when = datetime.now(timezone.utc).isoformat()
    return {
        'schema': SCHEMA,
        'run_id': f'{10000000000 + run_number}:1:fast:0',
        'lane': 'fast', 'shard': 0,
        'created_at': when,
        'sources': [{'source_id':'rss_ci','platform':'rss','status':'ok','posts':1}],
        'posts': [{'canonical_key': f'rss:ci/{run_number}', 'source_id':'rss_ci',
                   'platform':'rss', 'text':content, 'published_at':when}],
    }


def signed(payload):
    body = canonical(payload)
    return dict(body=body, sha256=hashlib.sha256(body).hexdigest(),
                signature=hmac.new(KEY,body,hashlib.sha256).hexdigest(),key=KEY)


class PostgreSQLQueueIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dsn = connection_string()
        cls.conn = psycopg.connect(cls.dsn, autocommit=True)
        schema = Path(__file__).with_name('pg_queue_schema.sql')
        cls.conn.execute(schema.read_text(encoding='utf-8'))

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def setUp(self):
        self.conn.execute('TRUNCATE argus_ingest.batches')
        self.queue = PostgresQueue(self.conn)

    def test_verified_insert_and_identical_replay(self):
        packet = signed(batch())
        self.assertEqual(self.queue.submit(**packet), 'queued')
        self.assertEqual(self.queue.submit(**packet), 'already_queued')
        self.assertEqual(self.queue.metrics()['unacknowledged'], 1)

    def test_conflicting_replay_rejected(self):
        self.queue.submit(**signed(batch()))
        with self.assertRaisesRegex(ValueError, 'run_id_digest_conflict'):
            self.queue.submit(**signed(batch(content='changed content')))
        self.assertEqual(self.queue.metrics()['unacknowledged'], 1)

    def test_bad_signature_never_enqueued(self):
        packet = signed(batch())
        packet['signature'] = '0' * 64
        with self.assertRaises(ValueError):
            self.queue.submit(**packet)
        self.assertEqual(self.queue.metrics()['unacknowledged'], 0)

    def test_claim_acknowledge_requires_verified_readback(self):
        self.queue.submit(**signed(batch()))
        lease = self.queue.claim()
        self.assertIsNotNone(lease)
        with self.assertRaisesRegex(ValueError, 'archive_readback_required'):
            self.queue.acknowledge(lease['run_id'], lease['lease_token'])
        self.queue.acknowledge(lease['run_id'], lease['lease_token'], archive_verified=True)
        self.assertIsNone(self.queue.claim())
        self.assertEqual(self.queue.metrics()['unacknowledged'], 0)

    def test_lease_expiry_fences_old_consumer(self):
        self.queue.submit(**signed(batch()))
        t = datetime.now(timezone.utc)
        first = self.queue.claim(lease_seconds=5, now=t)
        self.assertIsNone(self.queue.claim(now=t + timedelta(seconds=1)))
        second = self.queue.claim(lease_seconds=5, now=t + timedelta(seconds=6))
        self.assertNotEqual(first['lease_token'], second['lease_token'])
        with self.assertRaisesRegex(ValueError, 'invalid_or_expired_lease'):
            self.queue.acknowledge(first['run_id'], first['lease_token'], archive_verified=True,
                                   now=t + timedelta(seconds=6))
        self.queue.acknowledge(second['run_id'], second['lease_token'], archive_verified=True,
                               now=t + timedelta(seconds=6))

    def test_release_and_retry(self):
        self.queue.submit(**signed(batch()))
        first = self.queue.claim()
        self.queue.release(first['run_id'],first['lease_token'])
        second = self.queue.claim()
        self.assertNotEqual(first['lease_token'],second['lease_token'])
        self.assertEqual(self.queue.metrics()['unacknowledged'],1)

    def test_persists_after_connection_closed(self):
        self.queue.submit(**signed(batch()))
        with psycopg.connect(self.dsn, autocommit=True) as reconnected:
            second = PostgresQueue(reconnected)
            claimed = second.claim()
            self.assertIsNotNone(claimed)
            self.assertEqual(claimed['run_id'],batch()['run_id'])
            self.assertEqual(second.metrics()['unacknowledged'],1)

    def test_two_consumers_cannot_claim_same_batch(self):
        self.queue.submit(**signed(batch()))
        def worker(_):
            with psycopg.connect(self.dsn, autocommit=True) as db:
                return PostgresQueue(db).claim()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(worker, range(2)))
        self.assertEqual(sum(x is not None for x in results), 1)
        self.assertEqual(self.queue.metrics()['unacknowledged'],1)


if __name__ == '__main__':
    unittest.main()
