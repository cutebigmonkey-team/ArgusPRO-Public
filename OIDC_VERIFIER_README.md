# ARGUS GitHub OIDC JWT verifier (offline-tested component)

- Uses `PyJWT[crypto]` and fixed official GitHub JWKS at `https://token.actions.githubusercontent.com/.well-known/jwks`.
- Only accepts `RS256`; verifies signature, issuer, audience, expiry, issued-at, not-before, maximum TTL, and then the existing `oidc_policy` claims.
- `authorize_envelope()` binds authenticated `run_id` and `run_attempt` to the envelope.
- **No endpoint, server, production credentials, shared queue, GitHub workflow OIDC request, or archive writer changes.**
- `key_client` and `now` injection are for deterministic offline testing; a real HTTP ingress must **never** expose these arguments to a request.
- Production integration must validate envelope HMAC/shape, impose transport rate limits, enforce persistent JWT `jti` replay protection or equivalent one-use credentials, verify GitHub repository immutable IDs/workflow identity where available, and ensure queue `run_id` uniqueness.
- Do not trust token-provided `jku`, `x5u`, arbitrary JWKS URLs, nor client-supplied issuer or audience.
- Note: JWK retrieval uses fixed HTTPS GitHub endpoint. Real network connectivity and official JWT compatibility have **not** been exercised by these offline tests.

Test with an isolated environment:
```bash
python3 -m venv /tmp/argus-oidc-test-env
/tmp/argus-oidc-test-env/bin/pip install 'PyJWT[crypto]>=2.10,<3'
PYTHONPATH=ops/ingest /tmp/argus-oidc-test-env/bin/python -m unittest discover -s ops/ingest -p 'test_*.py' -v
```
