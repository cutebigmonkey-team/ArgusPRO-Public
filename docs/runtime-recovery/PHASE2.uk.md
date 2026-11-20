# Phase 2 — Durable Ingestion & Cursor Recovery

Статус: IMPLEMENTED / TESTED_OFFLINE. Production integration DISABLED.
Гілка: `feat/durable-ingestion-phase2`, база PR #6 `94ec43a7edfcc45f0dc567626184bf3e4db1602a`.
PR #3 `8203b8e73511725f9d79a082304539ae00feb385`: використано його `pg_queue.py`,
manual schema та всі contract/integration tests. PR #3/#6 не merged. PostgreSQL
adapter розширено в тому самому класі; другого queue backend не створено.
Старі v1/HMAC та offline_delivery contracts збережено для compatibility tests.

Baseline run 44245245181: 47 checked sources, 848 records / 808 dated, 30 ok —
надані користувачем verified measurements. Це не доказ durable delivery.
Жодна production конфігурація, архів, pending package або source cursor не змінені.

## Реалізований шлях

```
existing collector output
  -> producer.prepare (private files, no HTTP)
  -> deterministic source groups / v2 parts (v1 validation reused)
  -> private_gateway (disabled, OIDC + private source policy)
  -> existing PostgresQueue + transactional group/replay state
  -> TrustedHostlessConsumer (disabled, called INSIDE existing writer)
  -> ExistingJournalPort(existing IndexedFeedJournal, existing owner guard)
  -> existing verified journal delivery, NO per-part Drive cursor commit
  -> immutable ingestion input receipt, actual DriveStore byte read-back
  -> lease-fenced SQL acknowledgement
  -> all parts + record count + identity set verified
  -> older-group barrier + cursor revision CAS
```

Жоден module не створює Drive client, IndexedFeedJournal, worker чи writer lock.
Немає HTTP listener, service creation, deployment або auto-migration. WSGI callable
не mount-иться автоматично. Усі три активації (gateway, consumer, archive bridge)
мають enabled=False за замовчуванням. Phase 1 workflows не відправляють delivery,
не отримують нові production secrets/OIDC privileges; Fast/Extended збережені.
Docker COPY додає лише disabled contracts, без зміни entrypoint або DB bootstrap.

## Producer та wire protocol

- `partition.py`: per-source group, stable sort за canonical_key, незмінні post fields
  та source provenance. 648 records -> 250 + 250 + 148. При byte overflow parts
  детерміновано діляться додатково; oversized single post відхиляється, не обрізається.
- Кожен part <=250 posts / 2 MB. Максимум 256 parts, 10000 records per source,
  checkpoint JSON <=64 KB; producer input bounded 64 MB, report bounded 8 MB.
- Group digest охоплює source, producer run/attempt/lane/shard/SHA, checkpoint base
  revision/proposal, creation timestamp, усі part digests та загальний count.
  Порядок/metadata/chunk hashes не можна замінити непомітно. JSON canonical, duplicate
  keys/NaN/deep input/unknown top-level fields rejected. UUID/IDs не генеруються наново
  при retry; report.updated_at зберігає стабільний creation timestamp.
- Кожен part перевіряється чинним v1 envelope validator, з додатковою v2 перевіркою
  source-platform/key prefix, observed_at, post URL, text та links-only media.
  Unknown published_at залишається unknown. Inline media files не підтримуються цим path.
- Report keys/counts мають точно відповідати posts.jsonl. Ambiguous overlapping source
  identity reports відхиляються fail-closed: originals зберігаються для reconciliation;
  немає мовчазного присвоєння публікації іншому source.
- Producer files mode 0600, directory 0700, fsync; існуючий output не перезаписується.
  Source files не видаляються. CLI лише готує files і друкує counts, не payloads.
- `submit_prepared` — disabled callback contract, не HTTP client. Кожен part отримує
  свіжу short-lived identity, positive queue receipt НЕ означає archive receipt.
  Помилка зберігає prepared payloads; durable cursors ніколи не комітяться producer-ом.

## Private gateway / OIDC

Повторно використано `oidc_verifier.py` та `oidc_policy.py` із PR #6: RS256 signature,
фіксовані GitHub issuer/JWKS/audience, expiry/iat/nbf, max lifetime 600 s, token limit,
repository, main ref, sub, public visibility, github-hosted runner, schedule/manual
workflow identity. PR/fork/self-hosted/інший workflow відхиляються.
Додатково required configured repository/owner IDs, workflow SHA, reusable job workflow
ref/SHA; producer run/attempt/SHA зв’язані з verified claims. JWT-supplied jku/x5u/crit
не приймаються. Key-client/test clock не походять із HTTP body.

Native hash assignment і lane/platform policy перевіряються до queue. Private source
policy callback ОБОВ’ЯЗКОВИЙ: його реалізація має перевірити private registry, source
ownership і platform-specific cursor semantics. Самого source_id від producer недостатньо.
Credentials/control fields і credential URL userinfo rejected. Немає public archive ack API.

JWT jti зберігається лише як hash. Один jti прив’язаний до одного exact batch/body;
ідентичний replay ідемпотентний, інший body/part відхиляється. Для кожного part потрібен
свіжий JWT; це слід підтвердити реальним GitHub OIDC bootstrap до активації. Новий jti
для retry того самого batch не створює дубль. JWT потрібен при кожному HTTP retry;
queue processing після прийняття не припиняється через expiry producer token.

WSGI: POST-only path, content type/length limits, bounded read, generic errors без
credentials/URLs, 503 + Retry-After для backpressure/DB outage. TLS termination,
pre-auth rate limiting, connection pool, trusted catalog bootstrap і network mounting
поки не реалізовані в production. Не expose загальний PostgresQueue із writer role.

## PostgreSQL safety

`pg_queue.py` — єдиний adapter. `pg_phase2_schema.sql` — manual extension schema,
не виконується runtime. Потрібне private review SQL roles/grants/security-definer API.
PUBLIC grants revoked для нових tables; це НЕ замінює окремих least-privilege roles.

- Atomic payload + manifest + replay insert. Unique batch/source-run slots, digest
  conflicts reject; немає успішного queued до committed transaction.
- Phase 2 потребує dedicated autocommit connection в IDLE. Nested savepoint у чужій
  archive transaction rejected: його завершення ще не доводить durable commit.
- SKIP LOCKED, bounded lease 5..3600 s, random fencing tokens, expired lease recovery.
- Backoff exponential + jitter, bounded attempts; exhausted/expired packages -> dead,
  payload збережено. Dead letters не просувають cursors і не займають consumer lease.
- Global stored-body quota 64 MB, per-source unacknowledged quota 8 MB, replay row cap
  100000; переповнення дає backpressure. Acked payloads і replay rows автоматично не
  видаляються. Потрібні reviewed retention/cleanup і quota monitoring до live activation.
- Legacy claim/boolean acknowledgement не можуть обробляти v2 IDs.
- Last-part ack перевіряє ВСІ parts, total record count і відсутність cross-part identity
  collisions. Reconstruction uses server cursor / one part at a time.
- SQL batch ack + group completion + cursor CAS в одній transaction. Older pending
  group того самого source блокує новіший checkpoint: cursor не перестрибне payloads
  після crash. Unrelated sources продовжуються незалежно. Revision conflict held safely.
- `recover_cursor` повертає тільки verified committed state. Перед rollout треба seed
  SQL state з independently verified existing CursorJournal, не reset старих cursors.
  Private source policy має перевіряти base revision/continuation lineage.

## Trusted writer та archive read-back

`trusted_consumer.py` не реалізує writer. Owner guard має бути прив’язаний до реальної
session/advisory ownership чинного Hostless writer, не до running flag/постійного True.
`ExistingJournalPort` приймає тільки вже відкритий indexed journal, не будує новий.
Для per-part delivery використовує наявний import-only/permalink_backfill path з
порожнім state_updates: чинний CursorJournal не комітиться перед SQL all-parts gate.

Після verified journal delivery зберігається immutable bounded `ingest-<group>-<part>`
receipt з original input records. `DriveStore.read_bytes` робить справжній object lookup
та get_media; HTTP 200/upload result не є підтвердженням. Consumer порівнює actual
record content, batch ID та SHA256. Partial/missing/corrupt read-back не acknowledge.
Receipt name/idempotent package timestamp незмінні при повторі; canonical archive
identities дедуплікує наявний journal. Zero-post checkpoint також потребує durable
receipt; порожній список сам по собі не доводить archive persistence.

Original-input receipts додають Drive storage overhead. Quota, compaction та recovery
старих receipts/packages (особливо поза existing journal delta window) потребують
staging validation. Невизначений replay не вважається success: залишається pending/dead
для trusted reconciliation. Existing posts, comments і metadata не очищаються.

## Тести та чесні межі гарантій

Offline suites перевіряють 648 lossless partitioning/reassembly, body limits, corrupt
manifests, provenance, signed OIDC tampering/claims/expiry, disabled endpoint, rejected
credentials, prepared-file recovery, upload-200/partial read-back, crash та worker fence.
Real IndexedFeedJournal + MemoryStore fixture перевіряє import усіх 648 records,
повторне idempotent delivery, незмінні cursor files, збережені старі posts/comments.
Це справжній journal implementation, але НЕ live Google Drive evidence.

PR #3 додає 8 PostgreSQL integration scenarios. Phase 2 додає ще 11: all-part commit,
out-of-order admission, replay/rollback, lease restart/expiry, earlier-group barrier,
dead-letter, fairness/backoff, partial readback retry, quotas, v1/v2 compatibility,
manifest count і nested-transaction rejection. Mandatory `run_postgres_gate.py`
вимагає >=19 executed tests, нуль skips та success; відсутній PG service не дає green.
Tests відмовляються від будь-якого target крім explicit loopback CI fixture.

Локально real PostgreSQL service не налаштований; 19 scenarios НЕ ВИКОНАНІ.
Unit discovery показує skipped setUpClass для обох PG suites; це не PASS.
Немає заяви про real concurrency/transaction/archive delivery у production.
CI використовує наявний PR #3 postgres:16 service, standard runner, synthetic credentials,
contents:read, без production secrets, id-token:write, PR privilege чи deployment.
Жоден safety test не вимкнено. Phase 1 regression workflows та тести збережені.

Filesystem GitHub runner disposable: гарантія durable custody починається з SQL queued
receipt. Неприйняті частини при зникненні runner неможливо відновити лише з metadata.
Cursor barrier не дозволить мовчки пропустити прийняту incomplete group, але для
production потрібна reviewed producer recovery/retention policy, своєчасне staging
всіх parts і private alerting. Неприйняті payloads зараз лишаються у prepared files,
не видаляються; universal data recovery поза durable boundary не обіцяється.

## Production rollout — тільки після review та окремої авторизації

1. Draft stacked PR від Phase 1; узгодити порядок PR #3/#6 без auto-merge. Rebase/review
   єдиного pg_queue adapter після їхнього схвалення. CI PG gate, security, Phase 1,
   exact image/browser/archive/frontend gates мають пройти на supported runner.
2. На існуючій authorized staging DB перевірити additive schema, grants, durable commits,
   concurrent replay/claims, backups, quotas, retry/dead-letter metrics та restoration.
   Ніяких migrations у production до окремого дозволу.
3. Mount private gateway у наявному cloud backend, TLS/rate limits та JWKS egress policy.
   Server-side DB credentials/private catalog/IDs залишаються в secure cloud config.
   Public PR jobs їх не отримують. Поточні workflows не мають ingestion privileges.
4. Прив’язати consumer/JournalPort до чинного single writer та реального ownership guard.
   Не запускати archive-worker або другий writer. Перевірити Drive read-back failure,
   restart до/після receipt/SQL ack, latest-feed visibility та resource use.
5. Узгодити authoritative cursor mapping із чинним scheduler/CursorJournal і seed з
   verified legacy state. Визначити producer ownership без duplicate collection runs.
6. Лише після security review та explicit approval додати reviewed main-only OIDC
   delivery bootstrap. Canary: малий source set, bounded queue, all-part receipts,
   cursor CAS, archive/API/frontend checks. Не називати queued receipt доставкою.
7. Rollback: вимкнути gateway та consumer admission, залишити payloads/groups/cursors;
   не truncate/reset/drop schema, не requeue dead letters через public API. Відновити
   попередній worker без підключення legacy consumer до v2 records. Source cursor
   compatibility з Drive відновлювати лише з verified committed state, не guess.

## Результати локальної перевірки кандидата (2026-11-18)

- Ingestion discovery: `Ran 81 tests`, OK; додатково 2 skipped `setUpClass`
  для PostgreSQL suites. Ці skips означають 19 невиконаних real-PG scenarios,
  а не успішне проходження integration gate.
- Phase 1: 158 tests, 145 passed, 13 existing environment-dependent skipped;
  failures/errors/timeouts відсутні у виконаних 20 selected regression suites.
- Security regression: 10 passed. `git diff --check`: clean.
- Independent Gitleaks directory scan з redaction та без inline allow rules:
  no leaks found. Publication audit: zero blocking findings,
  `LOCAL_SCAN_CLEAR_REMOTE_REVIEW_REQUIRED`; historical disposable DB fixtures
  окремо позначені scanner як fixtures, production values не додавалися.
- Mandatory PostgreSQL gate без isolated DSN завершується nonzero з
  `isolated_postgres_required`. Це BLOCKED_BY_ENVIRONMENT, не application PASS.
- Exact Docker/Chromium/frontend gates не виконано для Phase 2 image.
- GitHub write probe: HTTP 403 `Resource not accessible by integration`.
  Новий remote branch / Draft PR / Phase 2 CI run поки не створено.
  Existing PR #3 run 44093007821 перевіряв 8 старих PG tests, не цей patch.

Рішення: кандидат придатний для code review, але НЕ production-ready.
Потрібні GitHub write access, mandatory CI, private bootstrap/TLS/rate limiting,
reviewed DB grants, authoritative existing-cursor import, real writer ownership
binding та crash/replay/Drive/API/frontend acceptance на authorized staging.
Жодні production credentials, migrations, writer processes чи delivery flags
цим patch не активуються.

## P1 corrections після pre-merge review (2026-11-20)

Production delivery залишається DISABLED. Цей patch не створює writer, не змінює
production schema, source cursors або pending data.

### Спільний archive record contract

`storage.validate_post` — той самий validator для `validate_package` і v2
`partition.validate_part`. Missing media / `[]` допустимі, `null`, object, string,
number та boolean — ні. Signed wire bytes не нормалізуються і не змінюються;
archive validator працює з копією. Unknown publication dates залишаються unknown.
Gateway і PostgreSQL adapter викликають v2 validation до queue insert/transaction.
Немає ZIP-building або production Drive access під час admission.

### Незалежний archive read-back

`ExistingJournalPort._verify_records` повторно читає checksum-verified pages,
перевіряє chain continuity до verified compacted checkpoint, actual canonical
identity/aliases і normalized content. SQLite index визначає expected normalization,
але НЕ є доказом durable persistence. Receipt/body digest/HEAD equality недостатні.

Це виконується перед створенням receipt, при повторному import з існуючим receipt
та перед consumer acknowledgement. Missing/corrupt page, wrong identity, partial
import або stale receipt блокують verification. Unacknowledged SQL body зберігається;
retry/backoff/dead-letter працюють як раніше. Consumer також повторно перевіряє
раніше acknowledged siblings тієї самої checkpoint group, включно з compacted
records, перш ніж acknowledge поточний part. SQL читається по одному bounded
body, без transaction під час remote I/O. Corruption не ремонтується шляхом
видалення receipt, пропуску page чи вигаданого success. Після trusted repair retry
може завершитися; dead letters потребують private reconciliation.

Normal tail reads не завантажують base archive. Якщо потрібні записи compacted або
незмінні, base перевіряється існуючим streaming checksum reader. Memory bounded до
однієї page та одного bounded part; tail обмежено existing MAX_REPLAY_PAGES. Кількість
remote reads залежить від tail length; live latency/throughput не виміряні.
`IndexedFeedJournal.base_checkpoint` походить із verified bootstrap/compaction,
не з producer request. Restart відновлює його з archive.

### Fault-injection evidence

Ті самі 11 нових deterministic tests виконано проти baseline modules `c882c5a`
і patched implementation. Baseline: 8 failing test methods / 12 assertion failures
(5 media subcases), 3 passing methods. Patch: усі 11 tests passing.
Перевірено missing pages, corrupt bytes, valid checksum з wrong identity,
compacted base corruption, stale receipts, partial import, failed retries,
restart/cursor file preservation та відсутність full-base reload у tail path.
Це offline fixtures, не production evidence.

Додано 5 real PostgreSQL scenarios: missing/corrupt journal data з двома failed
retries та recovery; stale receipt; missing earlier acknowledged page перед last
part commit для 648 records; повторна перевірка раніше acknowledged compacted
part перед наступним acknowledgement. Mandatory PG16 gate підвищено з 19 до 24 tests,
без skips. Попередній green run 44716346444 стосувався baseline `c882c5a`, а не
цього нового patch. Нові CI результати слід записати після доступного push.

### Окремі activation blockers, не включені до цих P1 fixes

1. **Durable producer outbox:** disposable files не гарантують recovery payloads,
   які ще не прийняті SQL. Cross-run/attempt recovery та >24h recovery потребують
   trusted protocol; не послаблювати OIDC run binding або expiry.
2. **Least-privilege database roles:** reviewed ingress-only SQL API/grants,
   окремі consumer/migration roles, retention, quotas, backups і dead-letter recovery.
3. **Trusted gateway bootstrap:** private catalog policy, TLS/rate limits/JWKS,
   real short-lived GitHub OIDC smoke та authenticated cursor recovery endpoint.
4. **Existing Hostless delivery executor:** справжній session ownership guard,
   dedicated queue connection, authoritative legacy cursor handoff, один чинний
   writer/executor. Не створювати competing worker або активувати archive-worker.

Усі activation blockers потребують review/staging validation та окремого дозволу.


### Підсумок доступної локальної валідації P1 patch

- Ingestion: 92 tests OK; 2 suite-level PG skips означають, що 24 real PostgreSQL
  scenarios тут НЕ виконані. Mandatory gate без isolated DSN fail-closed exit 1.
- Security: 10 passed у Python environment із PyYAML. Cached image interpreter
  без PyYAML дав 1 import error; повтор із установленою реальною залежністю пройшов.
- Storage: 9 tests OK, включно з 3 existing PostgreSQL-dependent skips.
- Archive: 19 із 20 file suites green, 97 passed. `test_x_delivery.py` timeout;
  цей самий bounded запуск timeout і на original baseline modules `c882c5a`.
- Collectors: 40 із 50 file suites green, 287 tests включно з 17 existing skips.
  `test_enhanced.py`: 15 tests, 1 failure + 1 error через відсутні локальні Crawlee
  та Crawl4AI. Ще 9 file suites timeout: incremental, media, post_metrics, public,
  rss_gateway, source_adapters, stream_results, tinyfish_fetch, x_api.
  Для `test_incremental.py` timeout також відтворено на baseline modules.
  Timeout не вважається PASS; ці suites потребують supported CI environment.
- Full combined archive/collector attempts також завершилися timeout (180 s).
  Test files не змінювалися, щоб приховати ці результати; жодних нових skips.
- Independent Gitleaks directory scan: no leaks. Publication audit:
  LOCAL_SCAN_CLEAR_REMOTE_REVIEW_REQUIRED, zero blocking findings.
- Push/CI/PR update BLOCKED: GitHub integration повертає HTTP 403 на create-tree
  та update-PR; shell git не резолвить github.com. Нових CI run links немає.

Це review candidate, НЕ production-ready release. Baseline CI green не переноситься
на нові зміни. Після відновлення GitHub write access необхідні push, Draft PR update,
PG16 gate (24 без skips), exact image і повні hosted regression gates.
