# ARGUS ingestion envelope — isolated prototype

Scope: bounded canonical JSON + HMAC-SHA256 authenticity/integrity for candidate microbatches. No HTTP endpoint, no durable queue, no production writer, no credentials, no deployments and no live posts.

Test in public repo:

```bash
PYTHONPATH=ops/ingest python3 -m unittest discover -s ops/ingest -p 'test_*.py' -v
```

Before enabling real delivery:

1. Create a private TLS ingress with workload identity. Never grant production write credentials to PR jobs. Prefer short-lived OIDC credentials or scoped verified signatures.
2. Durable storage with unique run_id and digest, rejecting any replay with a different digest. HMAC alone does not prevent replay.
3. Explicit limits, retention, backlog metrics, quarantining and acknowledgement only after read-back from the fenced Hostless archive writer.
4. Perform final record, origin, timestamp, platform, ownership, URL, and canonical-key validation in the existing package validation path before archiving.
5. Do not use public GitHub artifacts for private posts, catalogs or candidate packages.
6. Keep the Hostless fenced writer as the only archive writer. Test queue and writer integration before enabling any collection-to-archive delivery.

This prototype expects four shards as used by PR #2; not yet an approved production interface.
