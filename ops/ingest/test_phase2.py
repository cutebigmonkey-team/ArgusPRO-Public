import copy
import hashlib
import io
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from envelope import canonical
from partition import partition_source, validate_part, decode, from_collector
from private_gateway import PrivateGateway, REUSABLE
from trusted_consumer import TrustedHostlessConsumer, verify_readback
from test_oidc_verifier import valid_claims, NOW

WHEN=datetime.fromtimestamp(NOW,timezone.utc)
SID='rss_fixture'
SHARD=int(hashlib.sha256(SID.encode()).hexdigest(),16)%4
PRODUCER={'run_id':44058882718,'attempt':1,'lane':'fast','shard':SHARD,'sha':'a'*40}
SOURCE={'source_id':SID,'platform':'rss','status':'ok'}


def posts(n):
    return [dict(canonical_key=f'rss:fixture/{i}',source_id=SID,platform='rss',text='Publication '+str(i),
                 published_at=WHEN.isoformat(),observed_at=WHEN.isoformat(),post_url=f'https://example.org/posts/{i}',metadata={'ordinal':i}) for i in range(n)]


def parts(n=648,revision=0):
    return partition_source(PRODUCER,SOURCE,posts(n),{'base_revision':revision,'proposal':{'head_id':'648'}},WHEN.isoformat(),now=WHEN)


class PartitionTests(unittest.TestCase):
    def test_648_lossless_deterministic_and_v1_compatible(self):
        expected=posts(648);result=parts()
        self.assertEqual([len(p['posts']) for p in result],[250,250,148])
        self.assertEqual(sorted((p for part in result for p in part['posts']),key=lambda p:p['canonical_key']),sorted(expected,key=lambda p:p['canonical_key']))
        other=partition_source(PRODUCER,SOURCE,list(reversed(expected)),result[0]['checkpoint'],WHEN.isoformat(),now=WHEN)
        self.assertEqual([canonical(p) for p in result],[canonical(p) for p in other])
        for p in result:self.assertEqual(decode(validate_part(p,now=WHEN),now=WHEN),p)

    def test_byte_budget_splits_before_overflow_and_single_oversize_fails(self):
        rows=posts(30)
        for p in rows:p['text']='x'*90000
        result=partition_source(PRODUCER,SOURCE,rows,parts(1)[0]['checkpoint'],WHEN.isoformat(),now=WHEN)
        self.assertGreater(len(result),1)
        self.assertEqual(sum(len(p['posts']) for p in result),30)
        rows[0]['text']='x'*2100000
        with self.assertRaises(ValueError):partition_source(PRODUCER,SOURCE,rows[:1],parts(1)[0]['checkpoint'],WHEN.isoformat(),now=WHEN)

    def test_provenance_duplicate_and_manifest_tampering_rejected(self):
        p=parts(1)[0]
        for mutate in (lambda x:x['posts'][0].update(platform='telegram'),lambda x:x['parts'].append('0'*64),lambda x:x.update(index=3)):
            broken=copy.deepcopy(p);mutate(broken)
            with self.assertRaises(ValueError):validate_part(broken,now=WHEN)
        with self.assertRaises(ValueError):partition_source(PRODUCER,SOURCE,posts(1)*2,p['checkpoint'],WHEN.isoformat(),now=WHEN)

    def test_collector_report_requires_complete_identity_set(self):
        rows=posts(648)
        report={'format':'argus-collection-report-v1','shard_index':SHARD,'shard_count':4,'updated_at':WHEN.isoformat(),
                'sources':[{**SOURCE,'posts':648,'canonical_keys':[p['canonical_key'] for p in rows]}],
                'state_updates':{SID:{'head_id':'648'}}}
        kwargs=dict(run_id=PRODUCER['run_id'],attempt=1,lane='fast',shard=SHARD,sha='a'*40,now=WHEN)
        self.assertEqual(len(from_collector(report,rows,**kwargs)),3)
        with self.assertRaises(ValueError):from_collector(report,rows[:-1],**kwargs)


class GatewayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.key=rsa.generate_private_key(public_exponent=65537,key_size=2048)

    def gateway(self,*,enabled=True,source_policy=None):
        self.store=Mock();self.store.submit_part.return_value='queued'
        return PrivateGateway(self.store,source_policy or (lambda p,i:True),repository_id='fixture_repo',owner_id='fixture_owner',enabled=enabled,
            key_client=Mock(get_signing_key_from_jwt=Mock(return_value=Mock(key=self.key.public_key()))))

    def token(self,**overrides):
        claims={**valid_claims(),'repository_id':'fixture_repo','repository_owner_id':'fixture_owner',
                'job_workflow_ref':REUSABLE,'job_workflow_sha':'a'*40,'workflow_sha':'a'*40,**overrides}
        return jwt.encode(claims,self.key,algorithm='RS256',headers={'kid':'fixture'})

    def test_valid_oidc_binds_exact_producer_and_durable_store(self):
        gateway=self.gateway();body=canonical(parts(1)[0])
        self.assertEqual(gateway.admit(body,authorization='Bearer '+self.token(),now=WHEN),'queued')
        self.store.submit_part.assert_called_once()
        self.assertEqual(self.store.submit_part.call_args.args,(body,))

    def test_untrusted_claims_never_enter_queue(self):
        for change in ({'event_name':'pull_request'},{'repository':'attacker/repo'},{'repository_id':'other'},
                       {'job_workflow_ref':'wrong'},{'job_workflow_sha':'b'*40},{'exp':NOW-50}, {'run_id':'999'}):
            gateway=self.gateway()
            with self.assertRaises(ValueError):gateway.admit(canonical(parts(1)[0]),authorization='Bearer '+self.token(**change),now=WHEN)
            self.store.submit_part.assert_not_called()

    def test_disabled_policy_malformed_and_credential_input_rejected(self):
        gateway=self.gateway(enabled=False)
        with self.assertRaises(ValueError):gateway.admit(canonical(parts(1)[0]),authorization='Bearer '+self.token(),now=WHEN)
        self.store.submit_part.assert_not_called()
        for body in (b'{}',b'{"schema":1,"schema":2}',b'['*200,b'x'*2000001):
            gateway=self.gateway()
            with self.assertRaises((ValueError,RecursionError)):gateway.admit(body,authorization='Bearer '+self.token(),now=WHEN)
            self.store.submit_part.assert_not_called()
        gateway=self.gateway(source_policy=lambda p,i:False)
        with self.assertRaises(ValueError):gateway.admit(canonical(parts(1)[0]),authorization='Bearer '+self.token(),now=WHEN)
        self.store.submit_part.assert_not_called()

    def test_credential_metadata_and_untrusted_jwt_key_urls_are_rejected(self):
        rows=posts(1);rows[0]['metadata']={'authorization':'synthetic forbidden field'}
        packet=partition_source(PRODUCER,SOURCE,rows,parts(1)[0]['checkpoint'],WHEN.isoformat(),now=WHEN)[0]
        gateway=self.gateway()
        with self.assertRaises(ValueError):gateway.admit(canonical(packet),authorization='Bearer '+self.token(),now=WHEN)
        self.store.submit_part.assert_not_called()
        claims={**valid_claims(),'repository_id':'fixture_repo','repository_owner_id':'fixture_owner',
                'job_workflow_ref':REUSABLE,'job_workflow_sha':'a'*40,'workflow_sha':'a'*40}
        token=jwt.encode(claims,self.key,algorithm='RS256',headers={'kid':'fixture','jku':'https://example.invalid/keys'})
        with self.assertRaises(ValueError):gateway.admit(canonical(parts(1)[0]),authorization='Bearer '+token,now=WHEN)
        self.store.submit_part.assert_not_called()

    def test_public_endpoint_disabled_without_reading_body(self):
        gateway=self.gateway(enabled=False);start=Mock();stream=Mock()
        result=gateway.http({'wsgi.input':stream},start)
        self.assertIn(b'ingestion_disabled',result[0]);stream.read.assert_not_called()


class ConsumerTests(unittest.TestCase):
    def lease(self):
        p=parts(2)[0];body=canonical(p)
        return dict(run_id=f"{p['group_id']}:0",sha256=hashlib.sha256(body).hexdigest(),body=body,lease_token='fixture')

    def receipt(self,lease):
        return {'batch_id':lease['run_id'],'sha256':lease['sha256'],'posts':json.loads(lease['body'])['posts']}

    def test_http_200_or_partial_content_never_acknowledges(self):
        for receipt in ({'status':200},None):
            lease=self.lease();queue=Mock();queue.verification_siblings.return_value=[];queue.claim_part.return_value=lease
            archive=Mock();archive.readback.return_value=receipt
            consumer=TrustedHostlessConsumer(queue,archive,lambda:True,enabled=True)
            with self.assertRaises(ValueError):consumer.consume_once(now=WHEN)
            queue.acknowledge_part.assert_not_called();queue.retry_part.assert_called_once()
        lease=self.lease();receipt=self.receipt(lease);receipt['posts'].pop()
        with self.assertRaises(ValueError):verify_readback(lease['body'],receipt)

    def test_exact_independent_content_readback_is_required(self):
        lease=self.lease();queue=Mock();queue.verification_siblings.return_value=[];queue.claim_part.return_value=lease
        archive=Mock();archive.readback.return_value=self.receipt(lease)
        TrustedHostlessConsumer(queue,archive,lambda:True,enabled=True).consume_once(now=WHEN)
        queue.acknowledge_part.assert_called_once_with(lease['run_id'],'fixture',verified_body_sha=lease['sha256'],now=WHEN)

    def test_crash_and_expired_writer_never_advance(self):
        lease=self.lease();queue=Mock();queue.verification_siblings.return_value=[];queue.claim_part.return_value=lease
        archive=Mock();archive.import_candidate.side_effect=SystemExit('fixture crash')
        with self.assertRaises(SystemExit):TrustedHostlessConsumer(queue,archive,lambda:True,enabled=True).consume_once(now=WHEN)
        queue.acknowledge_part.assert_not_called()
        disabled=TrustedHostlessConsumer(queue,archive,lambda:True)
        with self.assertRaises(ValueError):disabled.consume_once(now=WHEN)


if __name__=='__main__':unittest.main()


class ProducerTests(unittest.TestCase):
    def test_prepare_648_private_files_and_retry_never_advances_or_deletes(self):
        import tempfile
        from pathlib import Path
        from producer import prepare, submit_prepared
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);rows=posts(648)
            report={'format':'argus-collection-report-v1','shard_index':SHARD,'shard_count':4,
                    'updated_at':datetime.now(timezone.utc).isoformat(),
                    'sources':[{**SOURCE,'posts':648,'canonical_keys':[p['canonical_key'] for p in rows]}]}
            (root/'report.json').write_text(json.dumps(report))
            (root/'posts.jsonl').write_text(''.join(json.dumps(p)+'\n' for p in rows))
            original=(root/'posts.jsonl').read_bytes();out=root/'parts'
            summary=prepare(root,out,run_id=PRODUCER['run_id'],attempt=1,lane='fast',shard=SHARD,sha='a'*40)
            self.assertEqual(summary['parts'],3);self.assertFalse(summary['checkpoint_advanced'])
            self.assertEqual(out.stat().st_mode & 0o777,0o700)
            for file in out.iterdir():self.assertEqual(file.stat().st_mode & 0o777,0o600)
            with self.assertRaises(ValueError):submit_prepared(out,lambda:'fixture',lambda b,t:'queued')
            first=submit_prepared(out,lambda:'fixture',lambda b,t:'queued',enabled=True)
            second=submit_prepared(out,lambda:'fixture',lambda b,t:'already_queued',enabled=True)
            self.assertEqual(len(first),3);self.assertEqual(len(second),3)
            self.assertEqual((root/'posts.jsonl').read_bytes(),original)
            self.assertEqual(len(list(out.glob('*.json'))),4)

    def test_nested_outer_transaction_cannot_claim_durable_queue_success(self):
        from pg_queue import PostgresQueue
        connection=Mock(autocommit=False)
        with self.assertRaisesRegex(ValueError,'dedicated_autocommit_connection_required'):
            PostgresQueue(connection).submit_part(canonical(parts(1)[0]),jti_hash='a'*64,expires_at=WHEN,now=WHEN)
        connection.transaction.assert_not_called()


class DeploymentBoundaryTests(unittest.TestCase):
    def test_ci_is_unprivileged_and_postgres_gate_cannot_silently_skip(self):
        from pathlib import Path
        root=Path(__file__).resolve().parents[2]
        workflow=(root/'.github/workflows/postgres-queue-validation.yml').read_text()
        self.assertIn('contents: read',workflow)
        self.assertNotIn('secrets.',workflow)
        self.assertNotIn('id-token:',workflow)
        self.assertNotIn('pull_request_target',workflow)
        self.assertIn('run_postgres_gate.py',workflow)
        gate=(root/'ops/ingest/run_postgres_gate.py').read_text()
        self.assertIn('result.skipped',gate)
        self.assertIn('result.testsRun<24',gate)


class CursorRecoveryTests(unittest.TestCase):
    def test_verified_snapshot_restores_existing_native_cursor_without_advancing(self):
        import tempfile
        from pathlib import Path
        from producer import restore_for_collector
        source={'id':SID,'url':'https://example.org/feed'}
        checkpoint={'url':source['url'],'head_id':'retained','pending':['https://example.org/older']}
        callback=Mock(return_value={'source_id':SID,'revision':7,'checkpoint':checkpoint,'verified_group':'a'*64})
        with tempfile.TemporaryDirectory() as directory:
            target=Path(directory)/'restore'
            with self.assertRaises(ValueError):restore_for_collector([source],callback,target)
            callback.assert_not_called()
            result=restore_for_collector([source],callback,target,enabled=True)
            self.assertFalse(result['checkpoint_advanced'])
            restored=json.loads((target/'cursors.json').read_text())
            self.assertEqual(restored['incremental_sources'][SID],checkpoint)
            self.assertEqual(json.loads((target/'revisions.json').read_text())['revisions'][SID],7)
            self.assertEqual((target/'cursors.json').stat().st_mode & 0o777,0o600)

    def test_cross_source_or_unverified_recovery_never_creates_output(self):
        import tempfile
        from pathlib import Path
        from producer import restore_for_collector
        for value in ({'source_id':'other','revision':1,'checkpoint':None,'verified_group':'a'*64},
                      {'source_id':SID,'revision':1,'checkpoint':None,'verified_group':None}):
            with tempfile.TemporaryDirectory() as directory:
                target=Path(directory)/'restore'
                with self.assertRaises(ValueError):restore_for_collector([{'id':SID,'url':'https://example.org/feed'}],lambda sid:value,target,enabled=True)
                self.assertFalse(target.exists())


class UnknownTimestampTests(unittest.TestCase):
    def test_unknown_publication_date_is_preserved_and_failed_source_cannot_commit_cursor(self):
        rows=posts(1);rows[0]['published_at']=None
        p=partition_source(PRODUCER,SOURCE,rows,{'base_revision':0,'proposal':None},WHEN.isoformat(),now=WHEN)[0]
        self.assertIsNone(decode(canonical(p),now=WHEN)['posts'][0]['published_at'])
        with self.assertRaisesRegex(ValueError,'unsafe_checkpoint'):
            partition_source(PRODUCER,{**SOURCE,'status':'http_429'},rows,{'base_revision':0,'proposal':{'head_id':'new'}},WHEN.isoformat(),now=WHEN)


class IncompleteCheckpointTests(unittest.TestCase):
    def test_partial_empty_cannot_advance_cursor_even_with_a_durable_empty_receipt(self):
        with self.assertRaisesRegex(ValueError,'incomplete_empty_checkpoint'):
            partition_source(PRODUCER,{**SOURCE,'status':'partial_results'},[],
                {'base_revision':0,'proposal':{'head_id':'unverified'}},WHEN.isoformat(),now=WHEN)
