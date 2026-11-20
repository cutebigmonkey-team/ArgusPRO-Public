"""Read-only fault injection against real indexed journal/package implementations."""
import copy
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from envelope import canonical
from feed_journal import HEAD
from journal_port import ExistingJournalPort
from partition import decode, digest, manifest
from publish import load_snapshot
from test_journal_port import Journal
from test_phase2 import WHEN, parts
import test_phase2 as fixtures
from trusted_consumer import TrustedHostlessConsumer, verify_readback


class ArchiveReadbackFaultTests(unittest.TestCase):
    def setUp(self):
        self.journal=Journal();self.addCleanup(self.journal.close)
        self.port=ExistingJournalPort(self.journal,lambda:True,enabled=True)
        self.packet=parts(2)[0];self.body=canonical(self.packet)
        self.bid=self.packet['group_id']+':0'

    def stage(self):
        self.port.import_candidate(self.body,batch_id=self.bid)
        return 'live-'+self.journal.head['sha']+'.json'

    def assert_blocked(self):
        with self.assertRaisesRegex(ValueError,'archive_readback_mismatch'):self.port.readback(self.bid)
        with self.assertRaisesRegex(ValueError,'archive_readback_mismatch'):self.port.import_candidate(self.body,batch_id=self.bid)
        self.assertEqual(self.journal.store.read_bytes(self.port._name(self.bid),2_000_000),self.body)

    def test_missing_page_blocks_readback_and_failed_retry(self):
        name=self.stage();original=self.journal.store.files.pop(name)
        self.assert_blocked()
        self.journal.store.files[name]=original
        self.assertEqual(verify_readback(self.body,self.port.readback(self.bid)),hashlib.sha256(self.body).hexdigest())
        self.port.import_candidate(self.body,batch_id=self.bid)
        self.assertEqual(len(load_snapshot(self.journal.store)['items']),2)

    def test_corrupt_page_blocks_readback_and_failed_retry(self):
        name=self.stage();original=self.journal.store.files[name]
        self.journal.store.files[name]=b'corrupt page'
        self.assert_blocked()
        self.journal.store.files[name]=original
        self.assertEqual(len(self.port.readback(self.bid)['posts']),2)

    def test_stale_input_receipt_cannot_attest_missing_canonical_records(self):
        self.journal.store.put(self.port._name(self.bid),self.body)
        self.assert_blocked()
        self.assertEqual(self.journal.deliveries,0)

    def test_partial_import_never_creates_receipt_or_acknowledges(self):
        deliver=self.journal.deliver
        def partial(package,report):
            from storage import pack,validate_package
            with tempfile.TemporaryDirectory() as directory:
                root=Path(directory)
                with validate_package(package,root) as (_,rows,_):first=rows[:1]
                (root/'posts.jsonl').write_bytes(canonical(first[0])+b'\n')
                (root/'media').mkdir();pack(root/'posts.jsonl',root/'media',root/'part.zip',created_at=WHEN.isoformat())
                restricted=copy.deepcopy(report);restricted['sources'][0].update(posts=1,canonical_keys=[first[0]['canonical_key']])
                return deliver(root/'part.zip',restricted)
        self.journal.deliver=partial
        queue=Mock();queue.verification_siblings.return_value=[];queue.claim_part.return_value={'run_id':self.bid,'body':self.body,
            'sha256':hashlib.sha256(self.body).hexdigest(),'lease_token':'fixture'}
        with self.assertRaises(ValueError):
            TrustedHostlessConsumer(queue,self.port,lambda:True,enabled=True).consume_once(now=WHEN)
        queue.acknowledge_part.assert_not_called();queue.retry_part.assert_called_once()
        self.assertIsNone(self.journal.store.read_bytes(self.port._name(self.bid),2_000_000))
        self.journal.deliver=deliver
        self.port.import_candidate(self.body,batch_id=self.bid)
        self.assertEqual(len(load_snapshot(self.journal.store)['items']),2)

    def test_compacted_base_bytes_are_verified_without_trusting_cached_index(self):
        self.stage();self.journal.compact(force=True)
        name=self.journal.base['snapshot_name'];original=self.journal.store.files.pop(name)
        self.assert_blocked()
        self.journal.store.files[name]=b'corrupt base'
        self.assert_blocked()
        self.journal.store.files[name]=original
        self.assertEqual(len(self.port.readback(self.bid)['posts']),2)

    def test_restart_reconstructs_independent_proof_and_preserves_cursor_files(self):
        name=self.stage();self.journal.compact(force=True)
        before=(self.journal.store.read_json('live-cursors.json'),self.journal.store.read_json('collection-cursors.json'))
        from indexed_journal import IndexedFeedJournal
        restored=IndexedFeedJournal(self.journal.store);self.addCleanup(restored.close)
        port=ExistingJournalPort(restored,lambda:True,enabled=True)
        self.assertEqual(len(port.readback(self.bid)['posts']),2)
        self.assertEqual(before,(self.journal.store.read_json('live-cursors.json'),self.journal.store.read_json('collection-cursors.json')))

    def test_previously_acknowledged_compacted_sibling_blocks_current_ack_until_repaired(self):
        packets=parts(648)
        first=packets[0];first_body=canonical(first);first_id=first['group_id']+':0'
        self.port.import_candidate(first_body,batch_id=first_id)
        self.journal.compact(force=True)
        base=self.journal.base['snapshot_name']
        for packet in packets[1:]:
            self.port.import_candidate(canonical(packet),batch_id=packet['group_id']+':'+str(packet['index']))
        current=packets[-1];body=canonical(current);bid=current['group_id']+':2'
        queue=Mock();queue.claim_part.return_value={'run_id':bid,'body':body,
            'sha256':hashlib.sha256(body).hexdigest(),'lease_token':'fixture'}
        queue.verification_siblings.return_value=[{'run_id':first_id,'body':first_body,
            'sha256':hashlib.sha256(first_body).hexdigest()}]
        original=self.journal.store.files.pop(base)
        consumer=TrustedHostlessConsumer(queue,self.port,lambda:True,enabled=True)
        with self.assertRaisesRegex(ValueError,'archive_readback_mismatch'):consumer.consume_once(now=WHEN)
        queue.acknowledge_part.assert_not_called();queue.retry_part.assert_called_once()
        self.journal.store.files[base]=original
        consumer.consume_once(now=WHEN)
        queue.acknowledge_part.assert_called_once()
        self.assertEqual(len(load_snapshot(self.journal.store)['items']),648)

    def test_tail_readback_does_not_reload_full_archive(self):
        self.stage()
        from unittest.mock import patch
        with patch('publish.load_base_snapshot',side_effect=AssertionError('unnecessary full base scan')):
            self.assertEqual(len(self.port.readback(self.bid)['posts']),2)

    def test_checksum_valid_but_wrong_identity_page_cannot_attest_original_record(self):
        # An input marker + validly checksummed unrelated page still isn't proof.
        import json
        from drive_store import encoded
        name=self.stage();page=json.loads(self.journal.store.files[name])
        page['items'][0]['id']='rss:unrelated'
        raw=encoded(page);sha=hashlib.sha256(raw).hexdigest()
        self.journal.store.files['live-'+sha+'.json']=raw
        self.journal.head={**self.journal.head,'sha':sha}
        self.journal.store.json(HEAD,self.journal.head,mutable=True)
        self.assert_blocked()


class ArchiveContractAdmissionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):fixtures.GatewayTests.setUpClass()

    def test_non_list_media_is_rejected_before_postgres_admission(self):
        fixture=fixtures.GatewayTests()
        for media in (None, {}, '', 0, False):
            with self.subTest(media_type=type(media).__name__):
                packet=parts(1)[0];packet['posts'][0]['media']=media
                packet['parts'][0]=digest(packet['posts']);packet['group_id']=digest(manifest(packet))
                gateway=fixture.gateway()
                with self.assertRaisesRegex(ValueError,'archive_post_contract_invalid'):
                    gateway.admit(canonical(packet),authorization='Bearer '+fixture.token(),now=WHEN)
                fixture.store.submit_part.assert_not_called()

    def test_accepted_posts_pass_real_package_validator_without_mutating_wire_bytes(self):
        from storage import pack,validate_package
        for media in ('missing', []):
            packet=parts(1)[0]
            if media!= 'missing':packet['posts'][0]['media']=media
            packet['posts'][0]['published_at']=None
            packet['parts'][0]=digest(packet['posts']);packet['group_id']=digest(manifest(packet))
            body=canonical(packet);decode(body,now=WHEN)
            with tempfile.TemporaryDirectory() as directory:
                root=Path(directory);(root/'media').mkdir()
                (root/'posts.jsonl').write_bytes(canonical(packet['posts'][0])+b'\n')
                pack(root/'posts.jsonl',root/'media',root/'package.zip',created_at=WHEN.isoformat())
                with validate_package(root/'package.zip',root) as (_,rows,_):
                    self.assertIsNone(rows[0]['published_at']);self.assertEqual(rows[0]['media'],[])
            self.assertEqual(canonical(packet),body)
