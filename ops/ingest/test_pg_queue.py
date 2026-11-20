"""Contract smoke tests. Full concurrency needs separate isolated PostgreSQL integration CI."""
import unittest
from pathlib import Path
from pg_queue import PostgresQueue

class PgQueueContractTests(unittest.TestCase):
    def test_invalid_lease_seconds_fail_closed(self):
        queue = PostgresQueue(None)
        for seconds in (0, 4, 3601, '120', True):
            with self.subTest(seconds=seconds), self.assertRaisesRegex(ValueError,'invalid_lease_seconds'):
                queue.claim(lease_seconds=seconds)

    def test_ack_without_verified_readback_fails_closed(self):
        queue = PostgresQueue(None)
        with self.assertRaisesRegex(ValueError,'archive_readback_required'):
            queue.acknowledge('run','token')

    def test_legacy_methods_cannot_mutate_phase2_batches(self):
        queue = PostgresQueue(None)
        identity = 'a' * 64 + ':0'
        with self.assertRaisesRegex(ValueError, 'invalid_v1_queue_identity'):
            queue.release(identity, 'token')
        with self.assertRaisesRegex(ValueError, 'invalid_v1_queue_identity'):
            queue.acknowledge(identity, 'token', archive_verified=True)

    def test_schema_has_unique_identity_and_body_limit(self):
        ddl = Path(__file__).with_name('pg_queue_schema.sql').read_text()
        self.assertIn('run_id text PRIMARY KEY',ddl)
        self.assertIn('octet_length(body) <= 2000000',ddl)
        self.assertIn("state IN ('pending','leased','acknowledged')",ddl)

    def test_queries_use_skip_locked_and_fencing(self):
        source = Path(__file__).with_name('pg_queue.py').read_text()
        self.assertIn('FOR UPDATE SKIP LOCKED',source)
        self.assertIn('lease_token=%s',source)
        self.assertIn('ON CONFLICT (run_id) DO NOTHING',source)

if __name__ == '__main__': unittest.main()
