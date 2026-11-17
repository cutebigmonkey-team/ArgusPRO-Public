import copy
import hashlib
import hmac
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from durable_queue import DurableQueue
from envelope import canonical, SCHEMA

KEY=b'B'*32
NOW=datetime(2026,11,17,12,0,tzinfo=timezone.utc)
T=int(NOW.timestamp())


def fixture():
    return {'schema':SCHEMA,'run_id':'44058882718:1:fast:0','lane':'fast','shard':0,
            'created_at':NOW.isoformat(),
            'sources':[{'source_id':'tg_demo','platform':'telegram','status':'ok','posts':1}],
            'posts':[{'canonical_key':'telegram:demo/42','source_id':'tg_demo','platform':'telegram',
                      'post_url':'https://t.me/demo/42','text':'Demo','published_at':NOW.isoformat()}]}


def signed(payload):
    body=canonical(payload)
    return {'body':body,'sha256':hashlib.sha256(body).hexdigest(),
            'signature':hmac.new(KEY,body,hashlib.sha256).hexdigest(),'key':KEY,'now':NOW}


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.path=Path(self.temp.name)/'queue.sqlite3'
        self.queue=DurableQueue(self.path)

    def tearDown(self):
        self.queue.close()
        self.temp.cleanup()

    def test_insert_and_idempotency(self):
        pkt=signed(fixture())
        self.assertEqual(self.queue.submit(**pkt),'queued')
        self.assertEqual(self.queue.submit(**pkt),'already_queued')
        self.assertEqual(self.queue.metrics()['unacknowledged'],1)

    def test_run_id_conflict(self):
        self.queue.submit(**signed(fixture()))
        changed=fixture();changed['posts'][0]['text']='other'
        with self.assertRaisesRegex(ValueError,'run_id_digest_conflict'):
            self.queue.submit(**signed(changed))

    def test_tamper_never_queued(self):
        pkt=signed(fixture());pkt['body']=pkt['body'].replace(b'Demo',b'Hack')
        with self.assertRaisesRegex(ValueError,'digest_mismatch'):
            self.queue.submit(**pkt)
        self.assertEqual(self.queue.metrics()['unacknowledged'],0)

    def test_claim_requires_ack_after_verified_readback(self):
        self.queue.submit(**signed(fixture()))
        lease=self.queue.claim(now=T)
        self.assertIsNotNone(lease)
        with self.assertRaisesRegex(ValueError,'archive_readback_required'):
            self.queue.acknowledge(lease['run_id'],lease['lease_token'],now=T)
        with self.assertRaisesRegex(ValueError,'invalid_or_expired_lease'):
            self.queue.acknowledge(lease['run_id'],'bad',archive_verified=True,now=T)
        self.queue.acknowledge(lease['run_id'],lease['lease_token'],archive_verified=True,now=T)
        self.assertIsNone(self.queue.claim(now=T))
        self.assertEqual(self.queue.submit(**signed(fixture())),'already_acknowledged')

    def test_expired_lease_reclaims_and_fences(self):
        self.queue.submit(**signed(fixture()))
        first=self.queue.claim(lease_seconds=5,now=T)
        self.assertIsNone(self.queue.claim(now=T+1))
        again=self.queue.claim(lease_seconds=5,now=T+6)
        self.assertNotEqual(first['lease_token'],again['lease_token'])
        with self.assertRaisesRegex(ValueError,'invalid_or_expired_lease'):
            self.queue.acknowledge(first['run_id'],first['lease_token'],archive_verified=True,now=T+6)
        self.queue.acknowledge(again['run_id'],again['lease_token'],archive_verified=True,now=T+6)

    def test_release_and_reclaim(self):
        self.queue.submit(**signed(fixture()))
        first=self.queue.claim(now=T)
        self.queue.release(first['run_id'],first['lease_token'],now=T)
        other=self.queue.claim(now=T)
        self.assertNotEqual(other['lease_token'],first['lease_token'])

    def test_reopen_persists_pending(self):
        self.queue.submit(**signed(fixture()))
        self.queue.close()
        self.queue=DurableQueue(self.path)
        lease=self.queue.claim(now=T)
        self.assertEqual(lease['run_id'],fixture()['run_id'])
        self.assertEqual(lease['body'],signed(fixture())['body'])

    def test_invalid_lease_duration(self):
        with self.assertRaisesRegex(ValueError,'invalid_lease_seconds'):
            self.queue.claim(lease_seconds=0,now=T)

if __name__=='__main__':unittest.main()
