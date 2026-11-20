"""Real PostgreSQL gates. Refuse any target except PR #3's loopback fixture."""
import hashlib
import json
import unittest
from datetime import timedelta
from pathlib import Path

from test_pg_queue_integration import connection_string, psycopg
from pg_queue import PostgresQueue
from envelope import canonical
from partition import digest
import test_phase2 as fixtures
from trusted_consumer import TrustedHostlessConsumer


class Phase2PostgreSQLTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dsn=connection_string()  # strict disposable role/database/host guard
        cls.conn=psycopg.connect(cls.dsn,autocommit=True)
        cls.conn.execute('DROP SCHEMA IF EXISTS argus_ingest CASCADE')
        for name in ('pg_queue_schema.sql','pg_phase2_schema.sql'):
            cls.conn.execute(Path(__file__).with_name(name).read_text())

    @classmethod
    def tearDownClass(cls):cls.conn.close()

    def setUp(self):
        self.conn.execute('TRUNCATE argus_ingest.batches,argus_ingest.source_cursors,argus_ingest.checkpoint_groups,argus_ingest.oidc_replays CASCADE')
        self.queue=PostgresQueue(self.conn)

    def submit(self,p,nonce=None,**kwargs):
        body=canonical(p)
        return self.queue.submit_part(body,jti_hash=hashlib.sha256((nonce or digest(p)).encode()).hexdigest(),
            expires_at=fixtures.WHEN+timedelta(minutes=5),now=fixtures.WHEN,**kwargs)

    def lease(self,**kwargs):return self.queue.claim_part(now=fixtures.WHEN,**kwargs)

    def ack(self,lease,**kwargs):
        return self.queue.acknowledge_part(lease['run_id'],lease['lease_token'],verified_body_sha=lease['sha256'],now=kwargs.pop('now',fixtures.WHEN),**kwargs)

    def test_out_of_order_648_all_parts_required_and_exactly_one_cursor_commit(self):
        p=fixtures.parts()
        for packet in reversed(p):self.submit(packet)
        first=self.lease();self.ack(first)
        self.assertEqual(self.queue.recover_cursor(fixtures.SID)['revision'],0)
        second=self.lease();self.ack(second)
        self.assertEqual(self.queue.recover_cursor(fixtures.SID)['revision'],0)
        self.ack(self.lease())
        self.assertEqual(self.queue.recover_cursor(fixtures.SID)['revision'],1)
        self.assertEqual(self.submit(p[0],'new-jti'),'already_acknowledged')
        self.assertEqual(self.queue.recover_cursor(fixtures.SID)['revision'],1)

    def test_jti_conflict_and_changed_producer_slot_are_transactional(self):
        p=fixtures.parts();self.submit(p[0],'same-token')
        with self.assertRaisesRegex(ValueError,'oidc_replay_conflict'):self.submit(p[1],'same-token')
        different=fixtures.parts(1)
        with self.assertRaisesRegex(ValueError,'checkpoint_group_conflict'):self.submit(different[0],'other-token')
        self.assertEqual(self.conn.execute('SELECT count(*) FROM argus_ingest.batches').fetchone()[0],1)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM argus_ingest.oidc_replays').fetchone()[0],1)

    def test_restart_and_expired_lease_cannot_advance(self):
        self.submit(fixtures.parts(1)[0]);old=self.lease(lease_seconds=5)
        with psycopg.connect(self.dsn,autocommit=True) as connection:
            restored=PostgresQueue(connection)
            self.assertEqual(restored.recover_cursor(fixtures.SID)['revision'],0)
            newer=restored.claim_part(lease_seconds=5,now=fixtures.WHEN+timedelta(seconds=6))
            with self.assertRaisesRegex(ValueError,'invalid_or_expired_lease'):self.ack(old,now=fixtures.WHEN+timedelta(seconds=6))
            restored.acknowledge_part(newer['run_id'],newer['lease_token'],verified_body_sha=newer['sha256'],now=fixtures.WHEN+timedelta(seconds=6))
            self.assertEqual(restored.recover_cursor(fixtures.SID)['revision'],1)

    def test_newer_complete_group_cannot_skip_older_pending_records(self):
        old=fixtures.parts(1)[0];self.submit(old)
        newer=fixtures.parts(2)[0];newer['producer']['run_id']+=1
        from partition import manifest
        newer['group_id']=digest(manifest(newer));self.submit(newer)
        claimed=[self.lease(),self.lease()]
        lease_a=next(l for l in claimed if l['run_id'].startswith(old['group_id']))
        lease_b=next(l for l in claimed if l['run_id'].startswith(newer['group_id']))
        self.assertEqual(self.ack(lease_b),'cursor_conflict')
        self.assertEqual(self.queue.recover_cursor(fixtures.SID)['revision'],0)
        self.ack(lease_a)
        self.assertEqual(self.queue.recover_cursor(fixtures.SID)['revision'],1)

    def test_exhausted_lease_dead_letter_keeps_payload_and_cursors(self):
        self.submit(fixtures.parts(1)[0]);self.lease(lease_seconds=5,max_attempts=1)
        self.assertIsNone(self.queue.claim_part(max_attempts=1,now=fixtures.WHEN+timedelta(seconds=6)))
        row=self.conn.execute('SELECT state,octet_length(body) FROM argus_ingest.batches').fetchone()
        self.assertEqual(row[0],'dead');self.assertGreater(row[1],0)
        self.assertEqual(self.queue.recover_cursor(fixtures.SID)['revision'],0)

    def test_backoff_does_not_block_unrelated_source(self):
        self.submit(fixtures.parts(1)[0]);lease=self.lease()
        self.queue.retry_part(lease['run_id'],lease['lease_token'],error_code='archive_unavailable',now=fixtures.WHEN)
        self.assertIsNone(self.lease())
        from partition import partition_source
        sid='rss_other';source={**fixtures.SOURCE,'source_id':sid};rows=fixtures.posts(1);rows[0]['source_id']=sid
        p=partition_source(fixtures.PRODUCER,source,rows,{'base_revision':0,'proposal':{'head_id':'1'}},fixtures.WHEN.isoformat(),now=fixtures.WHEN)[0]
        self.submit(p);self.ack(self.lease())
        self.assertEqual(self.queue.recover_cursor(sid)['revision'],1)
        self.assertEqual(self.queue.recover_cursor(fixtures.SID)['revision'],0)

    def test_partial_archive_readback_and_crash_after_upload_are_retryable(self):
        packet=fixtures.parts(2)[0];self.submit(packet);stored={}
        class Port:
            partial=True
            def import_candidate(self,body,*,batch_id):stored[batch_id]=json.loads(body)['posts']
            def readback(self,batch_id):
                rows=stored[batch_id]
                return {'batch_id':batch_id,'sha256':hashlib.sha256(canonical(packet)).hexdigest(),
                        'posts':rows[:1] if self.partial else rows}
        port=Port();consumer=TrustedHostlessConsumer(self.queue,port,lambda:True,enabled=True)
        with self.assertRaises(ValueError):consumer.consume_once(now=fixtures.WHEN)
        self.assertEqual(self.queue.recover_cursor(fixtures.SID)['revision'],0)
        port.partial=False
        consumer.consume_once(now=fixtures.WHEN+timedelta(minutes=10))
        self.assertEqual(len(stored),1)  # idempotent batch receipt, not duplicate import
        self.assertEqual(self.queue.recover_cursor(fixtures.SID)['revision'],1)

    def test_quota_rejection_rolls_back_replay_and_payload(self):
        with self.assertRaisesRegex(ValueError,'ingestion_backpressure'):self.submit(fixtures.parts(1)[0],max_source_bytes=1)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM argus_ingest.batches').fetchone()[0],0)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM argus_ingest.oidc_replays').fetchone()[0],0)

    def test_full_manifest_record_count_must_match_before_cursor_commit(self):
        from partition import manifest
        packet=fixtures.parts(1)[0];packet['record_count']=2;packet['group_id']=digest(manifest(packet))
        self.submit(packet);lease=self.lease()
        with self.assertRaisesRegex(ValueError,'checkpoint_record_count_mismatch'):self.ack(lease)
        self.assertEqual(self.queue.recover_cursor(fixtures.SID)['revision'],0)
        self.assertEqual(self.conn.execute('SELECT state FROM argus_ingest.batches').fetchone()[0],'leased')

    def test_real_connection_without_autocommit_cannot_return_queued(self):
        with psycopg.connect(self.dsn) as connection:
            with self.assertRaises(ValueError):
                PostgresQueue(connection).submit_part(canonical(fixtures.parts(1)[0]),jti_hash='a'*64,
                    expires_at=fixtures.WHEN,now=fixtures.WHEN)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM argus_ingest.batches').fetchone()[0],0)

    def test_legacy_claim_and_boolean_ack_cannot_consume_v2(self):
        self.submit(fixtures.parts(1)[0]);self.assertIsNone(self.queue.claim(now=fixtures.WHEN))
        lease=self.lease()
        with self.assertRaises(ValueError):self.queue.acknowledge(lease['run_id'],lease['lease_token'],archive_verified=True,now=fixtures.WHEN)
        self.assertEqual(self.queue.recover_cursor(fixtures.SID)['revision'],0)

    def _archive_fault_is_recoverable(self, fault):
        from test_journal_port import Journal
        from journal_port import ExistingJournalPort
        journal=Journal();self.addCleanup(journal.close)
        port=ExistingJournalPort(journal,lambda:True,enabled=True)
        packet=fixtures.parts(2)[0];body=canonical(packet);bid=packet['group_id']+':0'
        self.submit(packet)
        port.import_candidate(body,batch_id=bid)
        name='live-'+journal.head['sha']+'.json';original=journal.store.files[name]
        if fault=='missing':del journal.store.files[name]
        else:journal.store.files[name]=b'corrupt journal bytes'
        consumer=TrustedHostlessConsumer(self.queue,port,lambda:True,enabled=True)
        for offset in (0,10):
            with self.assertRaises(ValueError):consumer.consume_once(now=fixtures.WHEN+timedelta(minutes=offset))
            self.assertEqual(self.queue.recover_cursor(fixtures.SID)['revision'],0)
            state,saved=self.conn.execute('SELECT state,body FROM argus_ingest.batches').fetchone()
            self.assertEqual(state,'pending');self.assertEqual(bytes(saved),body)
        journal.store.files[name]=original
        consumer.consume_once(now=fixtures.WHEN+timedelta(minutes=20))
        self.assertEqual(self.queue.recover_cursor(fixtures.SID)['revision'],1)
        self.assertEqual(journal.index.count(),2)

    def test_real_journal_missing_page_never_acknowledges_and_retry_recovers(self):
        self._archive_fault_is_recoverable('missing')

    def test_real_journal_corrupt_page_never_acknowledges_and_retry_recovers(self):
        self._archive_fault_is_recoverable('corrupt')

    def test_stale_receipt_cannot_advance_sql_cursor(self):
        from test_journal_port import Journal
        from journal_port import ExistingJournalPort
        journal=Journal();self.addCleanup(journal.close)
        port=ExistingJournalPort(journal,lambda:True,enabled=True)
        packet=fixtures.parts(2)[0];body=canonical(packet);bid=packet['group_id']+':0'
        journal.store.put(port._name(bid),body);self.submit(packet)
        with self.assertRaises(ValueError):TrustedHostlessConsumer(self.queue,port,lambda:True,enabled=True).consume_once(now=fixtures.WHEN)
        self.assertEqual(self.queue.recover_cursor(fixtures.SID)['revision'],0)
        state,saved=self.conn.execute('SELECT state,body FROM argus_ingest.batches').fetchone()
        self.assertEqual(state,'pending');self.assertEqual(bytes(saved),body)

    def test_missing_earlier_acknowledged_page_blocks_last_part_cursor_commit(self):
        from test_journal_port import Journal
        from journal_port import ExistingJournalPort
        journal=Journal();self.addCleanup(journal.close)
        port=ExistingJournalPort(journal,lambda:True,enabled=True)
        packets=fixtures.parts(648)
        for packet in packets:self.submit(packet)
        consumer=TrustedHostlessConsumer(self.queue,port,lambda:True,enabled=True)
        consumer.consume_once(now=fixtures.WHEN)
        name='live-'+journal.head['sha']+'.json';original=journal.store.files[name]
        consumer.consume_once(now=fixtures.WHEN)
        self.assertEqual(self.queue.recover_cursor(fixtures.SID)['revision'],0)
        del journal.store.files[name]
        with self.assertRaises(ValueError):consumer.consume_once(now=fixtures.WHEN)
        self.assertEqual(self.queue.recover_cursor(fixtures.SID)['revision'],0)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM argus_ingest.batches WHERE state='acknowledged'").fetchone()[0],2)
        journal.store.files[name]=original
        consumer.consume_once(now=fixtures.WHEN+timedelta(minutes=10))
        self.assertEqual(self.queue.recover_cursor(fixtures.SID)['revision'],1)
        self.assertEqual(journal.index.count(),648)

    def test_compacted_earlier_acknowledgement_is_reverified_before_last_cursor_commit(self):
        from test_journal_port import Journal
        from journal_port import ExistingJournalPort
        journal=Journal();self.addCleanup(journal.close)
        port=ExistingJournalPort(journal,lambda:True,enabled=True)
        for packet in fixtures.parts(648):self.submit(packet)
        consumer=TrustedHostlessConsumer(self.queue,port,lambda:True,enabled=True)
        consumer.consume_once(now=fixtures.WHEN)
        journal.compact(force=True)
        base_name=journal.base['snapshot_name'];original=journal.store.files.pop(base_name)
        with self.assertRaises(ValueError):consumer.consume_once(now=fixtures.WHEN)
        self.assertEqual(self.queue.recover_cursor(fixtures.SID)['revision'],0)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM argus_ingest.batches WHERE state='acknowledged'").fetchone()[0],1)
        journal.store.files[base_name]=original
        # The third part is due before the failed second part's retry.
        consumer.consume_once(now=fixtures.WHEN)
        self.assertEqual(self.queue.recover_cursor(fixtures.SID)['revision'],0)
        consumer.consume_once(now=fixtures.WHEN+timedelta(minutes=10))
        self.assertEqual(self.queue.recover_cursor(fixtures.SID)['revision'],1)
        self.assertEqual(journal.index.count(),648)


if __name__=='__main__':unittest.main()
