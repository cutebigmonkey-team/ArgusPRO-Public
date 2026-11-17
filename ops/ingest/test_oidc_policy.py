import copy
import unittest
from oidc_policy import authorize_verified_claims, REPO, REF, WORKFLOW, ISSUER, AUDIENCE


def fixture():
    return {
        'iss': ISSUER,
        'aud': AUDIENCE,
        'repository': REPO,
        'repository_visibility': 'public',
        'ref': REF,
        'ref_type': 'branch',
        'sub': f'repo:{REPO}:ref:{REF}',
        'runner_environment': 'github-hosted',
        'repository_owner': 'cutebigmonkey-team',
        'event_name': 'schedule',
        'workflow_ref': f'{REPO}/{WORKFLOW}@{REF}',
        'sha': 'f'*40,
        'run_id': '123456789',
        'run_attempt': '1',
    }


class OIDCPolicyTests(unittest.TestCase):
    def test_trusted_scheduled_main_allowed(self):
        self.assertEqual(authorize_verified_claims(fixture())['run_id'], '123456789')

    def test_trusted_manual_main_allowed(self):
        claims = fixture(); claims['event_name'] = 'workflow_dispatch'
        self.assertEqual(authorize_verified_claims(claims)['event_name'], 'workflow_dispatch')

    def test_pull_request_rejected(self):
        claims = fixture(); claims['event_name'] = 'pull_request'
        with self.assertRaisesRegex(ValueError, 'unauthorized_event'):
            authorize_verified_claims(claims)

    def test_wrong_branch_rejected(self):
        claims = fixture(); claims['ref'] = 'refs/heads/feat/public-cloud-collection'
        with self.assertRaisesRegex(ValueError, 'unauthorized_ref'):
            authorize_verified_claims(claims)

    def test_fork_rejected(self):
        claims = fixture(); claims['repository'] = 'someone/ArgusPRO-Public'
        with self.assertRaisesRegex(ValueError, 'unauthorized_repository'):
            authorize_verified_claims(claims)

    def test_other_workflow_rejected(self):
        claims = fixture(); claims['workflow_ref'] = f'{REPO}/.github/workflows/public-validation.yml@{REF}'
        with self.assertRaisesRegex(ValueError, 'unauthorized_workflow'):
            authorize_verified_claims(claims)

    def test_self_hosted_runner_rejected(self):
        claims = fixture(); claims['runner_environment'] = 'self-hosted'
        with self.assertRaisesRegex(ValueError, 'unauthorized_runner_environment'):
            authorize_verified_claims(claims)

    def test_wrong_audience_rejected(self):
        claims = fixture(); claims['aud'] = 'other-service'
        with self.assertRaisesRegex(ValueError, 'unauthorized_aud'):
            authorize_verified_claims(claims)

    def test_opaque_id_validation(self):
        claims = fixture(); claims['run_id'] = '1:2:3'
        with self.assertRaisesRegex(ValueError, 'invalid_run_id'):
            authorize_verified_claims(claims)

    def test_missing_claims_rejected(self):
        with self.assertRaises(ValueError):
            authorize_verified_claims({})

    def test_unverified_payload_does_not_count_as_authorized_identity(self):
        # This policy must never be called on raw base64 JWT payload data.
        # Caller has to verify signature + standard JWT claims first.
        claims = fixture(); claims['iss'] = 'https://attacker.invalid'
        with self.assertRaisesRegex(ValueError, 'unauthorized_iss'):
            authorize_verified_claims(claims)


if __name__ == '__main__':
    unittest.main()
