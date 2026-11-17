import copy
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from oidc_policy import REPO, REF, ISSUER, AUDIENCE, WORKFLOW
from oidc_verifier import verify_token, authorize_envelope, JWKS_URL

NOW = 1794916800  # synthetic time for stable, network-free tests


def valid_claims():
    return {'iss': ISSUER, 'aud': AUDIENCE,
        'sub': f'repo:{REPO}:ref:{REF}', 'repository': REPO,
        'repository_visibility': 'public', 'ref': REF, 'ref_type': 'branch',
        'runner_environment': 'github-hosted', 'repository_owner':'cutebigmonkey-team',
        'event_name':'schedule', 'workflow_ref':f'{REPO}/{WORKFLOW}@{REF}',
        'sha':'a'*40, 'run_id':'44058882718', 'run_attempt':'1',
        'jti':'fixture-token-id', 'iat':NOW-30, 'nbf':NOW-30, 'exp':NOW+180}


class JWTVerificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.other_private = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def token(self, claims=None, key=None, alg='RS256', headers=None):
        return jwt.encode(valid_claims() if claims is None else claims,
                          self.private if key is None else key,
                          algorithm=alg, headers={'kid':'fixture-key', **(headers or {})})

    def client(self, key=None):
        return Mock(get_signing_key_from_jwt=Mock(return_value=Mock(key=key or self.private.public_key())))

    def check(self, claims=None, token=None, key=None):
        return verify_token(token or self.token(claims), key_client=self.client(key), now=NOW)

    def test_valid_signed_trusted_main_schedule(self):
        self.assertEqual(self.check()['run_id'], '44058882718')

    def test_valid_manual_event(self):
        claims=valid_claims();claims['event_name']='workflow_dispatch'
        self.assertEqual(self.check(claims)['event_name'], 'workflow_dispatch')

    def test_tampered_payload_rejected(self):
        parts=self.token().split('.')
        import base64, json
        data=json.loads(base64.urlsafe_b64decode(parts[1]+'=='))
        data['repository']='attacker/repo'
        parts[1]=base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b'=').decode()
        with self.assertRaises(ValueError): self.check(token='.'.join(parts))

    def test_wrong_signing_key_rejected(self):
        with self.assertRaises(ValueError): self.check(key=self.other_private.public_key())

    def test_unsigned_algorithm_rejected(self):
        with self.assertRaisesRegex(ValueError, 'unsupported_oidc_header'):
            self.check(token=jwt.encode(valid_claims(), 'secret', algorithm='HS256', headers={'kid':'fixture-key'}))

    def test_expired_token_rejected(self):
        claims=valid_claims();claims['iat']=NOW-300;claims['nbf']=NOW-300;claims['exp']=NOW-40
        with self.assertRaisesRegex(ValueError,'expired_or_invalid_oidc_lifetime'): self.check(claims)

    def test_too_long_lived_rejected(self):
        claims=valid_claims();claims['exp']=NOW+1000
        with self.assertRaisesRegex(ValueError,'expired_or_invalid_oidc_lifetime'): self.check(claims)

    def test_future_not_before_rejected(self):
        claims=valid_claims();claims['nbf']=NOW+60
        with self.assertRaisesRegex(ValueError,'expired_or_invalid_oidc_lifetime'): self.check(claims)

    def test_wrong_audience_rejected(self):
        claims=valid_claims();claims['aud']='other'
        with self.assertRaisesRegex(ValueError,'oidc_verification_failed'): self.check(claims)

    def test_wrong_issuer_rejected(self):
        claims=valid_claims();claims['iss']='https://evil.example'
        with self.assertRaisesRegex(ValueError,'oidc_verification_failed'): self.check(claims)

    def test_pull_request_rejected(self):
        claims=valid_claims();claims['event_name']='pull_request'
        with self.assertRaisesRegex(ValueError,'unauthorized_event'): self.check(claims)

    def test_wrong_workflow_rejected(self):
        claims=valid_claims();claims['workflow_ref']='other'
        with self.assertRaisesRegex(ValueError,'unauthorized_workflow'): self.check(claims)

    def test_missing_jti_rejected(self):
        claims=valid_claims();del claims['jti']
        with self.assertRaisesRegex(ValueError,'oidc_verification_failed'): self.check(claims)

    def test_envelope_identity_bound(self):
        envelope={'run_id':'44058882718:1:fast:0', 'lane':'fast', 'shard':0}
        self.assertEqual(authorize_envelope(self.token(),envelope,key_client=self.client(),now=NOW)['run_id'],'44058882718')
        envelope['run_id']='1234:1:fast:0'
        with self.assertRaisesRegex(ValueError,'run_id_oidc_mismatch'):
            authorize_envelope(self.token(),envelope,key_client=self.client(),now=NOW)

    def test_fixed_jwks_endpoint(self):
        self.assertEqual(JWKS_URL,'https://token.actions.githubusercontent.com/.well-known/jwks')

if __name__=='__main__': unittest.main()
