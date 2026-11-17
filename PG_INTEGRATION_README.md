# Isolated ARGUS PostgreSQL queue integration checks

This patch adds a PR-only GitHub Actions check backed by `postgres:16` service.
No Hostless database credentials, writer access, secrets, migrations or deployment.
The schema is applied ONLY to the temporary database `argus_ingest_ci`.

The suite validates eight scenarios: insertion/replay, conflicting replay,
invalid signature, acknowledgement gate, expired lease fencing,
release/retry, reconnection persistence, and concurrent independent consumers.

**Security guard:** tests refuse any DSN that is not the explicit loopback fixture
with database `argus_ingest_ci` and role `argus_ci`.

The workflow does not run on `push` or schedule. All credentials appearing in this
file are disposable CI fixtures, not credentials of ARGUS infrastructure.

This remains pre-production validation. Before enabling real ingress, segregate
Postgres privileges for ingress versus writer, add quotas and retention, ensure
backups and operator metrics, and have the existing fenced Hostless writer perform
independent archive readback before acknowledging batches.
