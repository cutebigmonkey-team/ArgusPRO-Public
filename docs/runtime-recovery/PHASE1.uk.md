# Collector Runtime Recovery — Phase 1

Статус: IMPLEMENTED / PARTIALLY_TESTED. Pilot залишається read-only.

## Межі зміни

Окрема локальна гілка `recovery/runtime-phase1`, worktree
`ArgusPRO-Public-runtime-phase1`. Початкова публічна робоча директорія не змінена.
Базу оновлено fast-forward з `1c8cf5304722f7066538a228617167e90f6271a2`
до актуального main `27dc37006f308a0dc5042d0253354ac877e131af` (2026-11-17).
Єдина upstream зміна — cron окремого heartbeat workflow. Звичайний git fetch
блокує DNS sandbox; commit отримано GitHub read-only connector, відновлено
byte-for-byte signed Git object і перевірено SHA до імпорту. Дерево вже було
доступне локально. SHA256 recovery файлів збіглися до/після fast-forward.

Приватні каталоги, OAuth, tokens, production DSN не копіювались.
Hostless, Drive, production cursors, pending queue та archive writer не змінювались.

## Виправлені причини

1. Спільна workflow concurrency group могла ставити Fast за Extended.
   Тепер orchestration має незалежні jobs `fast` / `extended` без cross-lane needs.
   Один reusable workflow запускає наявний collector; concurrency group містить
   lane і shard. Зберігаються чотири shards, Fast cron 5 хв, Extended cron 15 хв.
2. Старий public workflow не використовував incremental/adaptive маршрути.
   Read-only adapter викликає той самий `run.py` з `--incremental --adaptive
   --telegram-history`. Немає дублювання collectors. `platform_workers.py`, його
   тести, platform pacing, source timeout 30 с, batch budget 180 с не змінені.
3. Deferred sources зараховувалися до checked у старому diagnostics.
   Новий coverage відокремлює assigned, started, reported і fixed status counters.
   Відсутній source report означає `unreported`/`collector_failed` і ненульовий exit.
   Deferred budget/rate limit залишаються явно видимими, а не success.
4. Однаковий порядок міг постійно відкладати tail джерел. Для кожного platform/shard
   додається ротація за часовим tick його lane. Шардинг стабільний, дублювання немає.
   Це best-effort fairness; durable fairness при пропущених cron потребує ingestion.
5. PR більше не запускає live collection і не отримує pilot catalog secret.
   PR запускає тільки deterministic tests з contents:read, без secrets і deployment.
   Два PR fixture jobs викликають сам reusable workflow з validation_only=true;
   collector job додатково заборонений для pull_request всередині reusable workflow.

## Checkpoint safety

Phase 1 не реалізує delivery. Collector state_updates — лише непідтверджені
proposals у тимчасовому report. Wrapper не передає `--cursors`, не імпортує
publisher, не відкриває archive/database і не записує durable checkpoint.
`advance_checkpoint()` завжди fail-closed: `authenticated_archive_readback_not_implemented`.
Caller JSON з `archive_verified: true` та HTTP 200 не є дозволом на checkpoint.
Тести перевіряють незмінність checkpoint при extraction success, failure та
непідтвердженому receipt. Жодна з цих перевірок не є production archive receipt.

Вхідний catalog проходить попередню HTTPS/domain/query/ID allowlist validation,
як і раніше. Source assignment додатково відхиляє дублікати та невідомі platforms.
Raw collector logs і payload не публікуються, не завантажуються як Actions artifacts.
Тимчасові каталоги мають restricted permissions. Публічний summary містить лише
platform counters і fixed status codes; довільні повідомлення не виводяться.
Порожній lane/shard є валідним no-op. Непорожній output від попереднього запуску
відхиляється; invalid/missing reports не перетворюються на успішну collection.

## Покриття та свіжість

| Lane | Маршрутизовані platforms | Інтервал запуску |
|---|---|---|
| Fast | Telegram, RSS, YouTube, Threads, TikTok, RuTube, Mail.ru, Reddit, GDELT | 5 хв |
| Extended | Facebook, Instagram, VK, OK, X | 15 хв |

Це reachable routing, а не підтвердження працездатності кожного upstream.
MAX / WhatsApp не додані до публічного allowlist; невідомі platforms fail closed.
47 pilot sources / 4 shards — вихідні дані завдання. Актуальний catalog secret
не читався; поточні counts за platform: NOT_VERIFIED. Кожен trusted live run
обчислюватиме catalog configured/enabled counts і assigned counts свого shard.
Не слід сумувати catalog counts між shards: вони повторюють загальний catalog.

Publication freshness: NOT_VERIFIED — реальні нові публікації цим виконанням
не збирались. Fixtures не є live evidence. Runtime summary рахує observed records
із timezone-aware published_at за 5/30/60 хв; estimated, unknown і future dates
не підміняються discovery time. Це вік publications, не кількість нових archive inserts.
Archive writes, new archived posts і frontend visibility: NOT_MEASURABLE.
Нуль observed records у failure не доводить відсутність публікацій upstream.

## Валідація

- Фінальні Phase 1 tests після review: 20 PASS.
- Незмінні platform_workers regression tests: 3 PASS.
- Publication/security policy tests: 10 PASS у system Python.
  Додатковий cached Python 3.12 прогін: 9 PASS / 1 dependency ERROR (відсутній PyYAML);
  public-validation встановлює PyYAML перед цією suite.
- YAML parsing двох workflows: PASS; git diff --check: PASS.
- 48 collector suites запущено в окремих Python 3.12 процесах, з лімітом 12 с
  на suite. 38 suites завершилися PASS (263 tests на момент широкого прогону,
  включно з попередньою версією 15 Phase 1 tests; фінальні 20 перезапущено окремо).
- test_enhanced: FAIL в наявному середовищі — відсутні Crawlee module і Crawl4AI
  package metadata; 1 failure / 1 error із 15 tests. Readers у цій зміні не змінені.
- Дев'ять suites не завершились за 12 с: test_incremental, test_media,
  test_post_metrics, test_public, test_rss_gateway, test_source_adapters,
  test_stream_results, test_tinyfish_fetch, test_x_api. Це незавершені перевірки,
  не skipped і не підтверджені application regressions.
- Sandbox probe: socketpair send → PermissionError errno 1;
  asyncio.to_thread не завершується у bounded probe. Це обмежує async/thread tests.
- Повного зеленого regression прогону, Docker build і live acceptance тут немає.
  Наявний public-validation workflow з exact Docker/Chromium/PG перевірками збережено.
  Podman info також заблокований read-only libpod runtime directory; image не будувався.
  Workflows не dispatch-ились. Тести не вимикались і safety guards не послаблювались.

## До merge та наступної фази

1. Провести review п'яти змінених/нових файлів; main уже включено.
2. Запустити existing public-validation та нові deterministic tests на підтримуваному
   стандартному GitHub runner; усунути справжні regressions, не вимикати safety tests.
3. На trusted read-only manual run перевірити coverage кожного shard/lane, зібрати
   actual publication evidence. Cron є best-effort і не гарантує 5-хв freshness.
4. До будь-якого durable delivery потрібний приватний authenticated ingestion
   contract: idempotent batch ID/content digest, single writer, archive read-back
   по IDs/digest, authenticated receipt, atomic acknowledgement/cursor commit,
   повтор pending при crash і durable Retry-After/cooldown state.
5. Не додавати Drive production secrets до public PR, GitHub cache як authoritative
   checkpoint чи ще один archive writer. До ingestion кожен запуск cold incremental:
   durable continuity, cross-run dedup і cooldown recovery залишаються BLOCKED.
6. Hostless rollout / merge потребують окремого review та авторизації. Для відкату
   цієї Phase 1 достатньо revert public workflow/adapter patch; production state
   міграцій немає. Не трактувати revert як дозвіл повертати live collection у PR.

## Повторний release review

Повний diff перевірено, включно з 302 видаленими рядками orchestration workflow.
Catalog validation shell (SHA256
`cc5bd088ae504907c78bf80a794b0ce5317c85a5f158305cfd17c78658087a76`),
public readers install і pinned extended readers install перенесено до reusable
workflow byte-for-byte. Checkout pins, persist-credentials:false, restricted
permissions і відсутність production credentials збережені.
Platform Health доповнено sources_with_posts, dated_sources, source freshness
5/15/60 хв, median source age і allowlisted reader attempt counters. Deferred
джерела не видаються за started checks. Збережено початкові native shard coordinates,
оскільки run.py використовує їх також для розподілу optional upstream quotas;
повторний native hash guard на assigned subset є ідемпотентним.

Усі дев'ять timeout suites повторно запущено з faulthandler (dump через 5 с,
process limit 9 с). Усі дев'ять знову exit 124 — вони НЕ PASS і НЕ skipped.
Стек зупиняється в asyncio event loop selector / shutdown; stream_results та x_api
також показують executor worker. Окремий socketpair send probe отримує errno 1,
а мінімальний asyncio.to_thread probe зависає. Ці докази підтримують environmental
blocker; без supported runner повного висновку про ці suites немає.
Релевантні місця: test_incremental.py:37, test_media.py:33,
test_post_metrics.py:36, test_public.py:52, test_rss_gateway.py:56,
test_source_adapters.py:130, test_x_api.py:93. Жоден із цих tests/collectors не змінено.

Publication audit: 0 blocking findings, 0 scan gaps; два historical findings —
наявні reviewed_disposable_fixture. Gitleaks history та directory scan: exit 0,
no leaks. Перший directory scan знайшов generated test bytecode із synthetic
PAT fixtures; видалено лише generated __pycache__, без нових allowlists/послаблення
scanner, і незалежний scan повторено успішно.

На час commit mandatory hosted checks ще NOT_VERIFIED. Draft PR запускає наявні
public-validation (включно з real Crawlee/Crawl4AI, exact Docker, PostgreSQL і
Chromium) та нові reusable fixture jobs; стан і посилання на runs слід дивитися
у PR. Це не production-ready release. Live pilot не dispatch-иться, archive
writer не додається, durable cursors залишаються незмінними.
