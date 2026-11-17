"""Offline E2E tests against existing signed envelope and SQLite queue."""
import copy
import hashlib
import hmac
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from durable_queue import DurableQueue
from envelope import canonical
from offline_delivery import candidate_from_collector, drain_verified_once

KEY = b'K' * 32
NOW = datetime.now(timezone.utc)
STAMP = NOW.isoformat()
T = int(NOW.timestamp())


class FakeIndependentArchive:
    """Disposable fixture only. NOT a substitute for real Drive archive readback."""
    def __init__(self):
        self.items = {}
        self.fail_next_readback = False
        self.import_calls = 0

    def import_candidate(self, payload, *, sha256):
        self.import_calls += 1
        key = payload['run_id']
        value = {'run_id': key, 'sha256': sha256,
                 'canonical_keys': sorted(p['canonical_key'] for p in payload['posts'])}
        if key in self.items and self.items[key] != value:
            raise ValueError('archive_run_id_conflict')
        self.items[key] = value

    def readback_identity(self, run_id):
        if self.fail_next_readback:
            self.fail_next_readback = False
            raise OSError('simulated_readback_outage')
        return copy.deepcopy(self.items[run_id])


class E2E(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.out = self.directory / 'collector'
        self.out.mkdir()
        self.archive = FakeIndependentArchive()
        self.queue = DurableQueue(self.directory / 'queue.sqlite')
        self.write_fixture()

    def tearDown(self):
        self.queue.close()
        self.temp.cleanup()

    def write_fixture(self, *, post_key='telegram:demo/42', source_id='tg_demo'):
        self.report = {
            'format': 'argus-collection-report-v1',
            'shard_index': 0,
            'shard_count': 4,
            'sources': [{'source_id': 'tg_demo', 'platform': 'telegram',
                         'status': 'ok', 'posts': 1,
                         'canonical_keys': ['telegram:demo/42']}],
        }
        self.post = {'canonical_key': post_key, 'source_id': source_id,
                     'platform': 'telegram', 'post_url': 'https://t.me/demo/42',
                     'text': 'Synthetic fixture, never actual source content',
                     'published_at': STAMP}
        (self.out / 'report.json').write_text(json.dumps(self.report))
        (self.out / 'posts.jsonl').write_text(json.dumps(self.post) + '\n')

    def batch(self):
        return candidate_from_collector(self.out, workflow_run_id=44058882718,
                                        attempt=1, lane='fast', shard=0,
                                        created_at=STAMP)

    def submit(self):
        body = canonical(self.batch())
        return self.queue.submit(body, sha256=hashlib.sha256(body).hexdigest(),
                                signature=hmac.new(KEY, body, hashlib.sha256).hexdigest(),
                                key=KEY, now=NOW)

    def test_e2e_import_readback_ack(self):
        self.assertEqual(self.submit(), 'queued')
        self.assertEqual(drain_verified_once(self.queue, self.archive, now=T),
                         '44058882718:1:fast:0')
        self.assertEqual(self.queue.metrics()['unacknowledged'], 0)
        self.assertIsNone(drain_verified_once(self.queue, self.archive, now=T))
        self.assertEqual(self.archive.import_calls, 1)

    def test_readback_outage_does_not_ack_and_retry_is_idempotent(self):
        self.submit()
        self.archive.fail_next_readback = True
        with self.assertRaisesRegex(OSError, 'simulated_readback_outage'):
            drain_verified_once(self.queue, self.archive, now=T)
        self.assertEqual(self.queue.metrics()['unacknowledged'], 1)
        drain_verified_once(self.queue, self.archive, now=T)
        self.assertEqual(self.archive.import_calls, 2)
        self.assertEqual(self.queue.metrics()['unacknowledged'], 0)
        self.assertEqual(len(self.archive.items), 1)

    def test_queue_survives_restart_before_delivery(self):
        self.submit()
        self.queue.close()
        self.queue = DurableQueue(self.directory / 'queue.sqlite')
        self.assertTrue(drain_verified_once(self.queue, self.archive, now=T))

    def test_missing_post_fails_closed(self):
        (self.out / 'posts.jsonl').write_text('')
        with self.assertRaisesRegex(ValueError, 'collector_posts_mismatch'):
            self.batch()

    def test_unexpected_post_fails_closed(self):
        self.write_fixture(post_key='telegram:other/1')
        with self.assertRaisesRegex(ValueError, 'collector_posts_mismatch'):
            self.batch()

    def test_source_mismatch_fails_closed(self):
        self.write_fixture(source_id='wrong_source')
        with self.assertRaisesRegex(ValueError, 'post_source_platform_mismatch'):
            self.batch()

    def test_bad_shard_fails_closed(self):
        self.report['shard_index'] = 2
        (self.out / 'report.json').write_text(json.dumps(self.report))
        with self.assertRaisesRegex(ValueError, 'collector_shard_mismatch'):
            self.batch()

    def test_invalid_archive_receipt_never_acknowledged(self):
        self.submit()
        self.archive.readback_identity = lambda run_id: {'run_id': run_id}
        with self.assertRaisesRegex(ValueError, 'independent_archive_readback_mismatch'):
            drain_verified_once(self.queue, self.archive, now=T)
        self.assertEqual(self.queue.metrics()['unacknowledged'], 1)

    def test_oversized_batch_rejected_not_truncated(self):
        rows = [dict(self.post, canonical_key=f'telegram:demo/{i}') for i in range(251)]
        (self.out / 'posts.jsonl').write_text(''.join(json.dumps(x) + '\n' for x in rows))
        with self.assertRaisesRegex(ValueError, 'batch_requires_partitioning'):
            self.batch()


if __name__ == '__main__':
    unittest.main()
