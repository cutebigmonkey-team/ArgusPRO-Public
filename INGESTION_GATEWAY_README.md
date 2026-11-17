# ARGUS Ingestion Gateway — isolated WSGI contract

Contains an **undeployed** WSGI application and test suite. It is **not** a
production gateway, not a configured server, and does not integrate with
Hostless, PostgreSQL, or the archive writer.

The candidate JSON must be canonical, bounded and match the envelope schema.
The WSGI caller must provide a verified HTTPS scheme (never copy untrusted
`X-Forwarded-Proto`). The gateway validates GitHub's RS256 OIDC JWT through
`oidc_verifier.authorize_envelope` and signs the candidate with an **ingress-
private** HMAC key before storing in a queue. Do not put this key in Actions.
`202 Accepted` means **queued only**, not archived.

Test:

```bash
PYTHONPATH=ops/ingest python -m unittest discover -s ops/ingest -p 'test_*.py' -v
```

**Deployment gates**: confirm Hostless supports HTTP ingress on the existing
resource and its cost; obtain operator-approved TLS routing; do NOT use ephemeral
SQLite as shared cloud storage; implement durable private PostgreSQL queue with
quotas, replay protection, observability and backups; integrate only with fenced
Hostless writer; prevent PR or fork delivery; do not handle real source catalogs
or post contents with public CI artifacts; verify final archive readback before
acknowledging. This patch does not configure a listener or send traffic.
