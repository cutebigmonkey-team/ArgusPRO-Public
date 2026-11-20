# ARGUS staging-only private ingress bootstrap

This addition is **NOT a production service**. `staging_http.py` is a loopback-only WSGI HTTP listener for isolated staging integration tests. It reuses `PrivateGateway` and `PostgresQueue.submit_part` from PR #8. Nothing is enabled by default, no SQL migrations are run, and no Hostless worker or archive writer is started. GitHub Actions are **not** changed to send production data.

## Preconditions

- An entirely isolated PostgreSQL 16 instance, database `argus_ingest_ci` or `argus_ingest_staging`, role `argus_ci` or `argus_staging_ingress`, and pre-applied `pg_queue_schema.sql` + `pg_phase2_schema.sql` in this disposable instance only.
- A local JSON file outside Git tracking: `{"sources":[{"id":"rss_test","platform":"rss"}]}`. Only the private registry owner should provision this; never commit a source catalog.
- Installed `ops/ingest/requirements.txt` dependencies.
- Real GitHub OIDC tokens with the current main workflow identity will be rejected for other refs. Use deterministic fixtures for staging-only HTTP tests; do not weaken production OIDC policy to make a test pass.

## Intentionally fail-closed

The server refuses to start without `ARGUS_STAGING_GATEWAY_ENABLED=1`, a dedicated loopback staging DSN, numeric repository/owner IDs and the private source policy path. It refuses to start with archive credentials or writer ownership flags set. The listener binds only `127.0.0.1:8768`, not public interfaces. Requests still require signed GitHub OIDC. No response implies a post reached the Drive archive; HTTP 202 only means database queue receipt.

This bootstrap does **not** solve least-privilege SQL authorization, production ingress/TLS, durable producer outbox, Hostless integration, or trustworthy source registry/cursor synchronization. The staging role is trusted only within a throwaway database. Do not use a production DSN even if the database/role names are spoofed; verify isolation out-of-band.

## Test

```bash
PYTHONPATH=ops/ingest python -m unittest discover -s ops/ingest -p test_staging_http.py -v
```

Do not set up port forwarding to this listener or expose it to a public interface. Never connect it to the production Hostless database.
