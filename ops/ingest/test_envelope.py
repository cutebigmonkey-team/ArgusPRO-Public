import copy
import hashlib
import hmac
import json
import unittest
from datetime import datetime, timezone
from envelope import SCHEMA, canonical, sign, verify

KEY = b'A' * 32
NOW = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)

def fixture():
    return {'schema':SCHEMA,'run_id':'44058882718:1:fast:0','lane':'fast','shard':0,
            'created_at':'2026-11-17T12:00:00+00:00',
            'sources':[{'source_id':'tg_demo','platform':'telegram','status':'ok','posts':1}],
            'posts':[{'canonical_key':'telegram:demo/42','source_id':'tg_demo','platform':'telegram',
                      'post_url':'https://t.me/demo/42','text':'Demo','published_at':'2026-11-17T11:59:00+00:00'}]}

def signed(payload):
    data=canonical(payload)
    return data,hashlib.sha256(data).hexdigest(),hmac.new(KEY,data,hashlib.sha256).hexdigest()

class EnvelopeTests(unittest.TestCase):
    def test_signed_envelope_roundtrip(self):
        body,sha,sig=signed(fixture())
        self.assertEqual(verify(body,sha256=sha,signature=sig,key=KEY,now=NOW)['run_id'],fixture()['run_id'])
    def test_payload_tamper_rejected(self):
        body,sha,sig=signed(fixture());body=body.replace(b'Demo',b'Fake')
        with self.assertRaisesRegex(ValueError,'digest_mismatch'):
            verify(body,sha256=sha,signature=sig,key=KEY,now=NOW)
    def test_forged_signature_rejected(self):
        body,sha,sig=signed(fixture())
        with self.assertRaisesRegex(ValueError,'authentication_failed'):
            verify(body,sha256=sha,signature='0'*64,key=KEY,now=NOW)
    def test_duplicate_json_key_rejected(self):
        body,sha,sig=signed(fixture())
        noncanonical=body[:-1]+b',"schema":"'+SCHEMA.encode()+b'"}'
        sha=hashlib.sha256(noncanonical).hexdigest();sig=hmac.new(KEY,noncanonical,hashlib.sha256).hexdigest()
        with self.assertRaisesRegex(ValueError,'duplicate_json_key'):
            verify(noncanonical,sha256=sha,signature=sig,key=KEY,now=NOW)
    def test_stale_timestamp_rejected(self):
        row=fixture();row['created_at']='2026-11-14T12:00:00+00:00'
        data,sha,sig=signed(row)
        with self.assertRaisesRegex(ValueError,'stale_or_future_envelope'):
            verify(data,sha256=sha,signature=sig,key=KEY,now=NOW)
    def test_unknown_source_rejected(self):
        row=fixture();row['posts'][0]['source_id']='other'
        data,sha,sig=signed(row)
        with self.assertRaisesRegex(ValueError,'unknown_source_or_platform'):
            verify(data,sha256=sha,signature=sig,key=KEY,now=NOW)
    def test_duplicate_canonical_key_rejected(self):
        row=fixture();row['posts'].append(copy.deepcopy(row['posts'][0]))
        data,sha,sig=signed(row)
        with self.assertRaisesRegex(ValueError,'invalid_canonical_key'):
            verify(data,sha256=sha,signature=sig,key=KEY,now=NOW)
    def test_unsigned_not_valid(self):
        row=fixture();body,sha,sig=signed(row)
        with self.assertRaisesRegex(ValueError,'weak_signing_key'):
            verify(body,sha256=sha,signature=sig,key=b'unsafe',now=NOW)

if __name__ == '__main__':unittest.main()
