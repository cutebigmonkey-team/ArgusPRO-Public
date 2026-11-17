"""Verify GitHub Actions OIDC JWT before applying the ARGUS authorization policy.

No HTTP endpoint, queue writes, archive access or production credentials.
Only fetch keys from the fixed GitHub OIDC JWKS; never use JWT-supplied URLs.
"""
from datetime import datetime, timezone
import re

import jwt
from jwt import PyJWKClient
from jwt.exceptions import PyJWTError
from oidc_policy import AUDIENCE, ISSUER, authorize_verified_claims

JWKS_URL = 'https://token.actions.githubusercontent.com/.well-known/jwks'
MAX_TOKEN_SECONDS = 600
MAX_TOKEN_BYTES = 16384

# Instantiating this client never performs a request; the first verification does.
_CLIENT = PyJWKClient(JWKS_URL, cache_jwk_set=True, lifespan=300,
                      cache_keys=False, timeout=5)


def verify_token(token, *, key_client=None, now=None):
    """Return authenticated, authorized identity, rejecting anything malformed.

    `key_client` exists solely for deterministic offline testing. In production,
    do not accept user-selected key clients or JWKS endpoints.
    """
    if not isinstance(token, str) or len(token.encode('utf-8')) > MAX_TOKEN_BYTES or token.count('.') != 2:
        raise ValueError('invalid_oidc_token')
    try:
        header = jwt.get_unverified_header(token)
        # Only the key identifier may influence key lookup, never issuer URL/alg.
        if header.get('alg') != 'RS256' or not isinstance(header.get('kid'), str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,256}', header['kid']):
            raise ValueError('unsupported_oidc_header')
        client = _CLIENT if key_client is None else key_client
        key = client.get_signing_key_from_jwt(token).key
        # Time may be overridden only in offline tests; production uses current time.
        if now is None:
            claims = jwt.decode(token, key, algorithms=['RS256'], audience=AUDIENCE,
                                issuer=ISSUER, leeway=10,
                                options={'require': ['exp', 'iat', 'nbf', 'iss', 'aud', 'sub', 'jti']})
            current = datetime.now(timezone.utc).timestamp()
        else:
            current = now.timestamp() if isinstance(now, datetime) else float(now)
            # Test clock: explicit post-decode verification below.
            claims = jwt.decode(token, key, algorithms=['RS256'], audience=AUDIENCE,
                                issuer=ISSUER, options={
                                    'verify_exp': False, 'verify_nbf': False,
                                    'verify_iat': False,
                                    'require': ['exp','iat','nbf','iss','aud','sub','jti'],
                                })
        for name in ('iat', 'nbf', 'exp'):
            if type(claims.get(name)) is not int:
                raise ValueError('invalid_oidc_' + name)
        if not (claims['iat'] - 10 <= current <= claims['exp'] + 10 and
                claims['nbf'] - 10 <= current and
                0 < claims['exp'] - claims['iat'] <= MAX_TOKEN_SECONDS and
                claims['nbf'] <= claims['exp']):
            raise ValueError('expired_or_invalid_oidc_lifetime')
        if not isinstance(claims.get('jti'), str) or not 1 <= len(claims['jti']) <= 256:
            raise ValueError('invalid_oidc_jti')
        return authorize_verified_claims(claims)
    except (PyJWTError, KeyError, TypeError, OverflowError) as exc:
        raise ValueError('oidc_verification_failed') from exc


def authorize_envelope(token, envelope, *, key_client=None, now=None):
    """Bind verified workflow identity to the candidate envelope run_id."""
    identity = verify_token(token, key_client=key_client, now=now)
    if not isinstance(envelope, dict) or not isinstance(envelope.get('run_id'), str):
        raise ValueError('invalid_envelope_identity')
    fields = envelope['run_id'].split(':')
    if len(fields) != 4 or fields[0] != identity['run_id'] or fields[1] != identity['run_attempt']:
        raise ValueError('run_id_oidc_mismatch')
    if fields[2] != envelope.get('lane') or str(envelope.get('shard')) != fields[3]:
        raise ValueError('envelope_lane_shard_mismatch')
    return identity
