"""Post-verification authorization policy for ARGUS ingestion.

IMPORTANT: Input MUST be claims from an OIDC JWT whose signature, issuer,
expiry, not-before, audience, and key provenance were independently verified
against GitHub's current official JWKS. This module DOES NOT decode or verify
JWTs, fetch JWKS, create network endpoints, or grant archive access.
"""

import re

REPO = 'cutebigmonkey-team/ArgusPRO-Public'
REF = 'refs/heads/main'
WORKFLOW = '.github/workflows/argus-public-collection.yml'
ISSUER = 'https://token.actions.githubusercontent.com'
AUDIENCE = 'argus-ingestion'
ALLOWED_EVENTS = frozenset({'schedule', 'workflow_dispatch'})


def authorize_verified_claims(claims):
    """Fail closed on mismatches. Claims must be authenticated by the caller."""
    if not isinstance(claims, dict):
        raise ValueError('invalid_claims')
    exact = {
        'iss': ISSUER,
        'aud': AUDIENCE,
        'repository': REPO,
        'repository_visibility': 'public',
        'ref': REF,
        'ref_type': 'branch',
        'sub': f'repo:{REPO}:ref:{REF}',
        'runner_environment': 'github-hosted',
        'repository_owner': 'cutebigmonkey-team',
    }
    for field, value in exact.items():
        if claims.get(field) != value:
            raise ValueError('unauthorized_' + field)
    if claims.get('event_name') not in ALLOWED_EVENTS:
        raise ValueError('unauthorized_event')
    workflow = claims.get('workflow_ref') or claims.get('job_workflow_ref')
    if workflow != f'{REPO}/{WORKFLOW}@{REF}':
        raise ValueError('unauthorized_workflow')
    sha = claims.get('sha')
    if not isinstance(sha, str) or not re.fullmatch(r'[0-9a-f]{40}', sha):
        raise ValueError('invalid_commit_sha')
    # IDs must remain opaque, positive decimal strings; archive batch IDs are
    # derived from these verified values and the verified envelope lane/shard.
    for field in ('run_id', 'run_attempt'):
        value = claims.get(field)
        if not isinstance(value, str) or not re.fullmatch(r'[1-9][0-9]{0,19}', value):
            raise ValueError('invalid_' + field)
    return {
        'run_id': claims['run_id'],
        'run_attempt': claims['run_attempt'],
        'sha': sha,
        'repository': REPO,
        'workflow': workflow,
        'event_name': claims['event_name'],
    }
