import copy
import hashlib
import io
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

from durable_queue import DurableQueue
from envelope import SCHEMA, canonical
from ingestion_gateway import IngestionGateway, wsgi_application

NOW = datetime.now(timezone.utc)
RUN = '44058882718:1:fast:0'

def payload():
    return {'schema': SCHEMA, 'run_id': RUN, 'lane': 'fast', 'shard': 0,
            'created_at': NOW.isoformat(),
            'sources': [{'source_id': 'tg_demo', 'platform': 'telegram', 'status': 'ok', 'posts': 1}],
            'posts': [{'canonical_key': 'telegram:demo/42', 'source_id': 'tg_demo',
                       'platform': 'telegram', 'text': 'synthetic fixture'}]}


def fake_verified(token, batch):
    if token != 'fixture-github-oidc':
        raise ValueError('invalid_oidc_token')
    return {'run_id': '44058882718', 'run_attempt': '1'}


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.queue = DurableQueue(Path(self.temp.name) / 'queue.sqlite')
        self.auth = Mock(side_effect=fake_verified)
        self.gateway = IngestionGateway(self.queue, server_key=b'Q'*32, authorize=self.auth)

    def tearDown(self):
        self.queue.close()
        self.temp.cleanup()

    def request(self, body, token='fixture-github-oidc', **overrides):
        environ = {'PATH_INFO': '/v1/ingestion/batches', 'REQUEST_METHOD': 'POST',
                   'wsgi.url_scheme': 'https', 'CONTENT_TYPE': 'application/json',
                   'CONTENT_LENGTH': str(len(body)), 'HTTP_AUTHORIZATION': 'Bearer '+token,
                   'wsgi.input': io.BytesIO(body)}
        environ.update(overrides)
        recorded = []
        reply = b''.join(wsgi_application(self.gateway)(environ, lambda status, headers: recorded.append(status)))
        return recorded[0], json.loads(reply)

    def test_accept_and_receive_receipt_not_archive_ack(self):
        code, reply = self.request(canonical(payload()))
        self.assertEqual(code, '202 Accepted')
        self.assertEqual(reply['status'], 'queued')
        self.assertFalse(reply['archive_verified'])
        self.assertEqual(self.queue.metrics()['unacknowledged'], 1)

    def test_duplicate_exact_package_is_idempotent(self):
        body = canonical(payload())
        self.assertEqual(self.request(body)[1]['status'], 'queued')
        self.assertEqual(self.request(body)[1]['status'], 'already_queued')
        self.assertEqual(self.queue.metrics()['unacknowledged'], 1)

    def test_conflicting_same_run_id_does_not_overwrite(self):
        self.request(canonical(payload()))
        changed = payload(); changed['posts'][0]['text'] = 'modified'
        self.assertEqual(self.request(canonical(changed))[0], '401 Unauthorized')
        self.assertEqual(self.queue.metrics()['unacknowledged'], 1)

    def test_invalid_token_rejected_before_queue(self):
        self.assertEqual(self.request(canonical(payload()), 'invalid')[0], '401 Unauthorized')
        self.assertEqual(self.queue.metrics()['unacknowledged'], 0)

    def test_wrong_identity_rejected(self):
        self.auth.side_effect = lambda *_: {'run_id':'999', 'run_attempt':'1'}
        self.assertEqual(self.request(canonical(payload()))[0], '401 Unauthorized')
        self.assertEqual(self.queue.metrics()['unacknowledged'], 0)

    def test_duplicate_json_key_rejected(self):
        body = canonical(payload())[:-1]+b',"schema":"argus-ingest-envelope-v1"}'
        self.assertEqual(self.request(body)[0], '401 Unauthorized')

    def test_noncanonical_json_rejected(self):
        body = json.dumps(payload(), indent=2).encode()
        self.assertEqual(self.request(body)[0], '401 Unauthorized')

    def test_plain_http_rejected(self):
        self.assertEqual(self.request(canonical(payload()), **{'wsgi.url_scheme':'http'})[0], '403 Forbidden')
        self.assertEqual(self.queue.metrics()['unacknowledged'], 0)

    def test_oversized_length_rejected_before_stream_read(self):
        self.assertEqual(self.request(canonical(payload()), CONTENT_LENGTH='2000001')[0], '413 Content Too Large')

    def test_missing_bearer_rejected(self):
        self.assertEqual(self.request(canonical(payload()), HTTP_AUTHORIZATION='')[0], '401 Unauthorized')

    def test_wrong_endpoint_rejected(self):
        self.assertEqual(self.request(canonical(payload()), PATH_INFO='/health')[0], '404 Not Found')

    def test_wrong_media_type_rejected(self):
        self.assertEqual(self.request(canonical(payload()), CONTENT_TYPE='text/plain')[0], '415 Unsupported Media Type')

    def test_bad_method_rejected(self):
        self.assertEqual(self.request(canonical(payload()), REQUEST_METHOD='GET')[0], '405 Method Not Allowed')

    def test_short_body_rejected(self):
        self.assertEqual(self.request(canonical(payload()), CONTENT_LENGTH=str(len(canonical(payload()))+1))[0], '400 Bad Request')

    def test_expires_queue_lease_not_auto_acknowledged(self):
        self.request(canonical(payload()))
        lease = self.queue.claim()
        self.assertIsNotNone(lease)
        self.assertEqual(self.queue.metrics()['unacknowledged'], 1)
        with self.assertRaisesRegex(ValueError,'archive_readback_required'):
            self.queue.acknowledge(lease['run_id'],lease['lease_token'])

if __name__ == '__main__': unittest.main()
