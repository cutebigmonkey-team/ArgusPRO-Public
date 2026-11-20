# Public collection staging and rollback

The continuous Hostless worker owns source reservations, PostgreSQL result
spooling, verified Drive delivery, and the comment queue. The hosted pilot
produces read-only coverage. Its cron schedules are five minutes for the fast
lane and fifteen minutes for the extended lane. The isolated HTTP ingress is a
separate staging test surface.

## Build and test

From the repository root, build the reviewed checkout locally.

```sh
podman build -f ops/hostless/Dockerfile --target production \
  -t localhost/argus-stability-staging .
node --test frontend/cloud/*.test.mjs
```

The Hostless build includes the pinned Chromium runtime, the frontend snapshot
checks and the disposable PostgreSQL backend gate. For the separate HTTP and
PostgreSQL gateway checks, use the PostgreSQL 16 service and commands from
`postgres-queue-validation.yml`. The fixture database is disposable because
tests recreate its schema. Bind the PostgreSQL server itself to loopback. A
container with forwarded ports can report a bridge address and the gateway
will reject it.

For the legacy native backend, preserve its Docker health-check metadata and
run the native test targets in an isolated container.

```sh
podman build --format docker -f Dockerfile -t localhost/argus-native-stability-staging .
podman build -f Dockerfile --target backend-builder -t localhost/argus-stability-native-tests .
podman run --rm --network none localhost/argus-stability-native-tests \
  ctest --test-dir /app/backend/build --output-on-failure
```

## Staging activation

Use a staging-only private Drive folder, its collector-engine registry, an
isolated PostgreSQL direct or session endpoint with verified TLS, and a private
source catalog. Set `ARGUS_MIN_PRIVATE_SOURCES` to the expected minimum size
of the staging catalog. The optional `ARGUS_PRIVATE_SOURCE_FILE` can point
to a read-only mounted catalog outside `/app`. Keep the environment and the
catalog outside the checkout. Platform readers and the comment subprocess run
in anonymous mode.

Start with `ARGUS_RUNTIME_MODE=preflight`. Check startup and private archive
readability in the logs. To exercise delivery, set `ARGUS_RUNTIME_MODE=collect`
and `ARGUS_ARCHIVE_WRITER_OWNER=hostless` after confirming this worker is the
sole staging archive writer. Configure the existing supervisor to restart the
worker after a process exit. For a standalone staging container with Podman,
enable the host restart service for reboot recovery and use the container
restart policy.

```sh
podman run -d --name argus-collection-staging --restart=always \
  --env-file /path/to/staging-worker.env localhost/argus-stability-staging
```

If using a local source catalog, mount that file read-only and reference its
container path in the environment. Use a staging frontend configured for the
staging archive. Compare the source audit against the expected registry, wait
for real public observations, and check matching canonical IDs in SQL delivery
receipts, verified archive pages and the feed UI. Compare comment IDs the same
way for a source that exposes public comments.

Restart the staging container while it has staged work. Confirm the writer
lock is reacquired, the pending batch is recovered, the archive contains one
copy per canonical ID and source checkpoints reflect verified delivery. Check
the 5-minute window against active source counts and measure scheduling lag
under the intended CPU and memory allocation. Explicit cooldowns, source
restrictions and resource backpressure can extend the interval.

## Monitoring and rollback

Use the private source audit, `continuous_status` log event and archived
`monitor.json` together. Configure the existing alert sink to page on a
worker heartbeat missing for more than two minutes, repeated startup or
delivery failures, growing pending age, a saturated spool, or increasing
provider OOM and restart counts. Review platform windows alongside
`collector_batch_interrupted`, access restrictions, and the number of due
sources. Use complete windows for rates. An empty successful check measures
reachability. A verified insertion measures a new archived record.

Record the previous image ID and environment revision before activation.
Stop the candidate, confirm it released its writer session, then start the
previous image against the same staging state. Preserve PostgreSQL results,
pending packages, and Drive data during rollback. Verify recovery and compare
the last successful delivery, deduplicated IDs and cursor head after restart.
If the prior image cannot read pending state, preserve the state and stop the
rollback before starting a second writer.

## Public sample coverage, November 20, 2026

The checked-in source catalog has zero entries. The private runtime source
counts, active schedules, production success rates, provider OOM events, and
current archive delivery are unmeasured. The table records one public sample
per tested platform. Telegram, Facebook, VK, and OK had a second attempt.

| Platform | Samples | Posts | Comment observation or blocker |
| --- | ---: | ---: | --- |
| Telegram | 1 | 0 | Timed out in both the public page and history reader. |
| Facebook | 1 | 1 | Public Chromium read succeeded on retry. The post did not expose comments to the reader. |
| Instagram | 1 | 3 | Public RSS bridge. The sampled post did not expose comments to the reader. |
| Threads | 1 | 3 | The sampled public post returned 13 replies. The reader reported partial coverage. |
| X / Twitter | 1 | 0 | The public embed reader reported a robots restriction. |
| TikTok | 1 | 3 | Public embed. The sampled post did not expose comments to the reader. |
| YouTube | 1 | 15 | Native feed and public uploads reader. The local fallback and the staging image's pinned reader each failed to verify comments on the sampled video. |
| VK | 1 | 0 | The Chromium retry timed out. |
| OK | 1 | 0 | The Chromium retry timed out. |
| MAX | 0 | — | No verified anonymous message reader. |
| RSS / news | 1 | 6 | Feed collection succeeded. The sampled article returned an empty comment result. |
| Reddit | 1 | 0 | The RSS reader reported a robots restriction. |
| RuTube | 1 | 0 | The [public channel](https://rutube.ru/channel/23931703/) request timed out. |
| Mail.ru | 1 | 0 | The public website reader hit its source deadline. |
| GDELT | 1 | 0 | The public article discovery reader timed out. |
| WhatsApp | 0 | — | No verified anonymous message reader. |

Six of the fourteen sampled sources yielded posts. In a disposable replay using
the real collector output, 31 unique posts reached the existing journal and
frontend posts API modules. All 13 Threads comments survived duplicate
delivery, archive read-back, and reopening the SQLite database. This test
used SQLite and an in-memory Drive-compatible test store. It verified the
client API logic separately from browser rendering and from hosted Drive.

The Hostless image build ran 194 Hostless tests and 98 Drive tests, and ran
380 collector tests with 17 explicit hosted-only skips. The isolated
PostgreSQL 16 run passed 129 ingestion tests, including the five HTTP gateway
tests. Six native CTest suites passed. The UI has local snapshot and module
test coverage. Production source schedules, staging provider restart behavior
and live archive-to-browser delivery remain acceptance checks.

## Release acceptance against the configured catalog

Hostless loads the catalog from the path in `ARGUS_PRIVATE_SOURCE_FILE` when
set. Otherwise it reads `sources.json` in the configured private Drive
folder. Both paths apply the existing source submission records. The private
JSONBin registry controls which platform engines are enabled. SQL
`runtime_schedule` reflects effective admission, including operator
controls. Neither a source catalog entry nor an enabled engine establishes
that a source has run.

The local session had no staging credential bindings or mounted runtime
catalog. Configured, enabled, scheduled, overdue and failing source counts
for the actual deployment are therefore unknown. There is no verified
staging URL or evidence of two consecutive automatic staging cycles.

### Provider inventory and deployment settings

The repository contains `ops/hostless/Dockerfile` and no provider manifest
identifying a staging worker. Obtain the existing staging project, worker,
database, Drive folder and frontend identities from the workspace owner.
Confirm isolation with the owner before binding credentials. A name alone
cannot establish isolation.

The operator CLI exposed one project with two workers, one database and zero
sites. No project, worker or database name identified a staging candidate.
Drive isolation could not be verified because no staging bindings were
present. This inventory does not rule out an environment outside the
current account's visibility or one with a different naming convention.

From the authenticated operator CLI, list staging resource metadata and
variable names only. Leave the environment reveal option off.

```sh
hostless list projects
hostless --project "$STAGE_PROJECT" list workers
hostless --project "$STAGE_PROJECT" list db
hostless --project "$STAGE_PROJECT" list sites
hostless --project "$STAGE_PROJECT" --worker "$STAGE_WORKER" env list
```

Use `ops/hostless/staging.env.example` as the binding checklist. Configure
the existing background worker with build context `.`, Dockerfile
`ops/hostless/Dockerfile`, target `production`, and the image's default
command. Enable the provider's restart policy. The worker needs outbound
access to staging PostgreSQL, Drive, JSONBin and permitted public sources.
Bind the staging frontend separately to the staging archive. Keep secrets,
mounted catalog data and evidence outside the build context.

To run acceptance, provision an authorized isolated Hostless service and
staging frontend URL. Bind these inputs through the staging secret manager.

- `ARGUS_DRIVE_OAUTH` and `ARGUS_DRIVE_FOLDER_ID` scoped to a private staging
  archive with its base catalog and any approved submissions. The existing
  Drive check requires the staging identity to be able to add children and
  requires individual-account sharing. Set
  `ARGUS_MIN_PRIVATE_SOURCES` to the approved catalog minimum. If using the
  mounted catalog, bind `ARGUS_PRIVATE_SOURCE_FILE` to an absolute read-only
  path outside the checkout.
- `ARGUS_JSONBIN_BIN_ID` and `ARGUS_JSONBIN_KEY` for the staging engine registry.
  Set `ARGUS_JSONBIN_KEY_TYPE` for the supplied key type.
- `DATABASE_URL` for staging PostgreSQL, with a TLS-verified direct or session
  connection and schema creation privileges for the staging writer. Use
  `ARGUS_DATABASE_SESSION_URL` if the session endpoint differs. For the audit,
  bind `ARGUS_AUDIT_DATABASE_URL` to a staging role with `USAGE` on the runtime
  schema and `SELECT` on its runtime and measurement tables.
- Authorized restart control for the staging worker and read access to the
  staging frontend, archive and provider logs. Record the running image
  digest and align it with the reviewed image before collecting evidence.

Run the following with the staging bindings and the same read-only catalog
mount as the worker. The first command is an offline presence check. The
second requires authorization to read the isolated Drive and JSONBin
resources. It uses the worker's catalog loader and prints the configured,
source-enabled and engine-eligible counts by platform plus the derived
runtime schema. PostgreSQL is checked separately. SQL scheduling may differ
from engine eligibility because operator settings affect admission.
Inventory can run before a source minimum is set. Use its observed
configured count and the catalog owner's completeness confirmation to
set the approved minimum before enabling collect mode.

```sh
python ops/hostless/staging_preflight.py
python ops/hostless/staging_preflight.py --read-catalog
```

Once deployment is explicitly approved, start with
`ARGUS_RUNTIME_MODE=preflight`, check the worker logs for archive and registry
readability, and verify the staging TLS database endpoint separately. The
worker preflight does not open the writer session. Use a reviewed commit
available to the provider. The local working tree requires a reviewed
revision before a git-backed deployment. Repeat the deployment with
`ARGUS_RUNTIME_MODE=collect` and `ARGUS_ARCHIVE_WRITER_OWNER=hostless` after
confirming that this worker is the sole staging writer.

```sh
hostless --project "$STAGE_PROJECT" --worker "$STAGE_WORKER" \
  deploy --commit "$APPROVED_STAGE_SHA" --wait
hostless --project "$STAGE_PROJECT" --worker "$STAGE_WORKER" logs --since 15m
```

On staging, use the exact schema name for its archive and the same catalog
configuration as the worker. The command writes a mode-0600 report with
platform totals and omits source identifiers.

```sh
python ops/hostless/audit_collection.py \
  --schema "$ARGUS_STAGING_RUNTIME_SCHEMA" \
  --output /private/evidence/staging-sources.json --aggregate-only
```

Compare the configured catalog count to the SQL scheduled count, including
disabled operators' settings. Compare observed check intervals and lag
against the five-minute target, and review server cooldowns and failed
checks separately. The SQL check rate divides completed checks by attempts
in the retained observation window. Partial windows are marked incomplete.

### Two automatic collection windows and a restart

1. After pipeline admission, record UTC time **T0** and save an aggregate
   audit as `baseline.json`. Capture the running revision, worker instance
   and start time in private evidence.
2. Let the worker run automatically. Repeat the audit at **T1** and **T2**,
   approximately 300 seconds apart, using separate output files. Compare
   effective source counts, SQL check windows, lag, deferrals and collection
   events. Hostless schedules sources continuously. The two windows measure
   polling cadence without assuming all sources start together.
3. In the staging schema, query `runtime_inserted_posts` with the read-only
   role for `archived_at > T0 AND archived_at <= T1` and
   `archived_at > T1 AND archived_at <= T2`, using UTC epoch seconds. Keep
   `id`, `platform` and `archived_at` in private evidence. Match selected
   IDs from both windows to archive journal read-back and rendered posts in
   the staging frontend. Inspect the cloud frontend's `/api/posts` response
   in its workspace data module, or the HTTP response on the native path.
   Check a matched comment as well if the selected source exposes replies.
   If a window yields zero new records, record that result and continue
   until the chosen active sources supply two consecutive qualifying windows.
4. Restart only the verified staging worker through the provider. Record
   the new start time, writer-session acquisition, recovery events and
   the next automatic check. For pending-batch recovery, capture a staged
   batch before restart, then verify acknowledgement and one copy of each
   selected ID in the archive and frontend. A restart with an empty queue
   establishes startup recovery only. Save a final aggregate audit.

In the anonymous origin probe, DNS resolved for Telegram, VK, OK, RuTube,
Mail.ru and GDELT, but none completed a TCP connection within ten seconds.
That result precedes TLS and content parsing. It identifies a connectivity
block from the probe environment, with staging egress still to be tested.
X and Reddit returned HTTP 200 for their robots files while their sample
collection routes were denied by the collectors' robots checks.

One of the 17 hosted-only fixtures ran and passed in the local
network-disabled Hostless image. It exercised the Facebook parser with
Chromium. The other 16 skipped because their pinned upstream checkouts or
runtime dependencies were absent.

A second local recovery test staged the 31 previously observed public posts
in disposable PostgreSQL 16. The test killed its publisher with SIGKILL
after archive read-back and before SQL acknowledgement. A new process
recovered the same batch, left zero pending results and returned exactly 31
unique IDs from the existing frontend posts API code. It used a local
Drive-compatible test store and the existing journal implementation.
This verifies the recovery logic across a process boundary. The staging
supervisor, hosted Drive and browser delivery still require acceptance.

Production recommendation is **no-go** until the authorized staging
instance supplies the configured-source, two-cycle delivery, browser and
restart evidence above.
