# Hostless release candidate

## Runtime and configuration

Use the existing background worker. Build from repository root with
`ops/hostless/Dockerfile`, target `production`, and retain the image's
default command and UID 10001. Pass the approved commit as
`ARGUS_SOURCE_SHA` when the provider supports a build argument. Otherwise
record the provider's source SHA and resolved image ID separately. Keep
the current frontend deployment and archive contract.

| Boundary | Verified local path | Hostless requirement |
| --- | --- | --- |
| Registry | Checked-in public test catalog and default engine registry | `ARGUS_PRIVATE_SOURCE_FILE` if an approved readable absolute mount outside `/app` is supplied. Otherwise use private Drive `sources.json`. Both paths reconcile existing Drive submissions and check `ARGUS_MIN_PRIVATE_SOURCES` against the resulting catalog. JSONBin enables platform engines separately. |
| Runtime | Explicit loopback harness and public-only children | `ARGUS_RUNTIME_MODE=collect` and `ARGUS_ARCHIVE_WRITER_OWNER=hostless` for activation. The image sets `ARGUS_COLLECTOR_RUNTIME=hostless`. Confirm provider restart policy, CPU/memory allocation, and permitted egress. |
| Registry and archive access | Filesystem archive, no Google credentials | Staging or production-specific bindings for `ARGUS_DRIVE_OAUTH`, `ARGUS_DRIVE_FOLDER_ID`, `ARGUS_JSONBIN_BIN_ID`, `ARGUS_JSONBIN_KEY` and the appropriate `ARGUS_JSONBIN_KEY_TYPE`. Drive must be private, writable by the worker identity and shared with individual accounts only. |
| PostgreSQL | Disposable local connection and fixed local advisory lock | `DATABASE_URL` is required. Use `ARGUS_DATABASE_SESSION_URL` when a separate session endpoint is needed. The writer needs verified TLS, stable PostgreSQL backend PID and the exclusive advisory lock for the schema derived from the Drive folder. Transaction pooling is unsupported. |
| Ingestion and API | Real continuous scheduler and journal, loopback archive view | The existing Hostless worker runs the same continuous scheduler, normalized spool and journal. Keep the HTTP ingress and legacy native pipeline outside this activation. Production browser acceptance must use the current cloud frontend and the same private archive. |

Names-only provider inventory establishes that a binding name exists.
Confirm non-secret settings, actual binding targets, mount readability, registry
minimum, archive ownership and egress with the operator. Resolve entries in the
[staging acceptance runbook](PUBLIC_COLLECTION_STAGING.md) before cutover.

## Backup and rollback checkpoints

- Record the last known-good deployment SHA and image ID, the worker
  configuration revision, restart policy, writer inventory, and database
  schema identity. Confirm which jobs can write to the same archive.
- Quiesce the previous writer and confirm it released its SQL session.
  Export the matching PostgreSQL schema with pending package and results,
  leases, source health, comments and control tables. Preserve the private
  Drive catalog and cursor pointers, feed and comment heads, and every
  referenced immutable object. Confirm the backup can be read and retain it
  privately. Capture the pair at one quiescent boundary.
- Hostless initializes its existing derived runtime schema under its session
  lock. The candidate adds no new Hostless SQL columns and changes neither
  feed pages nor cursor format. Check the existing runtime table shape and
  writer role before selecting collect mode. Do not apply native migrations
  or the isolated ingress schemas to this worker.
- On failure, stop the candidate and confirm its session is gone. Start the
  captured last known-good revision against the existing state only after
  checking its ability to recover any pending packages and cursor head.
  Preserve new records and pending work. If versions cannot reconcile,
  keep the writer stopped and validate a coordinated recovery privately.
  Restoring only one side of the database and archive boundary can cause
  replay or missing data.

## Deployment and verification after approval

First validate the reviewed image in the authorized isolated staging
environment. Run the catalog preflight, verify exact SQL and archive
identity, then complete the two-window smoke and restart procedure from the
[staging runbook](PUBLIC_COLLECTION_STAGING.md). For production, confirm
the reviewed commit is available to the existing provider project after
separate source publication authorization. Use only the explicitly selected
project and worker.

```sh
hostless --project "$TARGET_PROJECT" --worker "$TARGET_WORKER" env list
hostless --project "$TARGET_PROJECT" --worker "$TARGET_WORKER" \
  deploy --commit "$APPROVED_RELEASE_SHA" --wait
hostless --project "$TARGET_PROJECT" --worker "$TARGET_WORKER" logs --since 15m
```

First launch in preflight mode when validating archive and engine-registry
access. Worker preflight does not open the database or attest to catalog
admission. Check the catalog preflight and staging-only TLS/database path
separately. After the owner confirms backup, writer cutover and approval,
activate collect mode and verify these signals:

1. Writer session acquired, catalog reconciled, journal verified, pending
   recovery completed, and correct active source counts.
2. Two automatic windows at approximately five minutes with real
   observations and SQL-to-archive receipts. Review overdue sources,
   restricted platforms, errors, queue age and restart/OOM indicators.
3. Match canonical IDs through the current frontend and verify one reply
   where the platform exposes comments. Retain evidence privately.
4. Exercise a controlled restart in staging, with pending package replay
   where possible. Confirm acknowledgement and no duplicate IDs.

If the cutover fails and the compatibility checks above pass, use the same
provider command with the captured previous SHA after stopping the candidate.
Confirm recovery, the cursor head, new archive delivery and a single active
writer.

**Production remains no-go** until provider settings and identities are
confirmed, backup and rollback evidence is available, and isolated
Hostless smoke checks pass.
