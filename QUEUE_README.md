# ARGUS durable inbox prototype (PR #2)

**Safe scope:** local SQLite queue only. There is no server, endpoint, OIDC exchange, GitHub uploading, external network call, drive access, database production access, or archive writer modification.

Apply from repository root:

```bash
unzip -o ~/Downloads/argus_durable_queue_patch.zip -d .
PYTHONPATH=ops/ingest python3 -m unittest discover -s ops/ingest -p 'test_*.py' -v
```

This zip includes only the two *new* files in `ops/ingest/`; it relies on the existing `ops/ingest/envelope.py` which must already be present on the branch.

Protocol: `submit` verifies canonical signed envelope (HMAC-SHA256), stores it in a transaction, rejects conflicting duplicate `run_id` and tolerates identical replays; `claim` obtains a time-bound one-consumer lease, with a random token that fences prior leases; `release` returns an item for retry; `acknowledge` requires a matching unexpired token **and** `archive_verified=True` asserted by a trusted caller. Status stays in durable SQLite and `metrics` supplies simple counts.

**Not production safe by itself:** `archive_verified=True` is a caller assertion, not an archive read-back verification. A real trusted worker must verify an independent durable readback before calling it. The local SQLite file is suitable for isolated testing, **not** a shared queue across GitHub Actions runners and Hostless. Production ingress needs workload identity/OIDC, authorization that excludes pull-request jobs, a private durable backend, transactionally enforced identity and replay, backlog and byte quotas, retention/alerts, final validation of post ownership and content, and integration with the existing *single* Hostless fenced writer. The HMAC key must not be handed to untrusted PR code.
