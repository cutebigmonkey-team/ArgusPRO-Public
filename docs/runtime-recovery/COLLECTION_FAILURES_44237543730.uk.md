# Collection failures — baseline 44237543730

Статус: IMPLEMENTED / TESTED_WITH_FIXTURES; live recovery NOT_VERIFIED.

Baseline: [GitHub run 44237543730](https://github.com/cutebigmonkey-team/ArgusPRO-Public/actions/runs/44237543730),
commit `2bee018093abc67076807c11defe1b233b648f27`, 2026-11-18 06:28:17–06:31:59 Europe/Kyiv.
Прочитано aggregate coverage logs усіх восьми collection jobs. Raw logs, source URLs,
catalog rows, tokens та credential diagnostics до цього звіту не копіювались.

## Точний baseline

`ok` — кількість source outcomes зі статусом ok, а не успішних GitHub jobs.
47/47 assigned/started/reported; 8/8 jobs success. Source ok: 0; sources_with_posts: 1.
40 source outcomes приховані під other_collector_error. Решта: robots_denied 5,
login_required 1, http_429 1. Лише Telegram має records. Archive delivery disabled;
checkpoint_advanced=false у всіх восьми summaries.

| Platform | Assigned | Source ok | Records / dated | Fresh 5 / 30 / 60 хв | Source statuses | After live |
|---|---:|---:|---:|---:|---|---|
| facebook | 4 | 0 | 0 / 0 | 0 / 0 / 0 | login_required=1, other_collector_error=3 | NOT_VERIFIED |
| instagram | 4 | 0 | 0 / 0 | 0 / 0 / 0 | http_429=1, other_collector_error=3 | NOT_VERIFIED |
| mailru | 3 | 0 | 0 / 0 | 0 / 0 / 0 | other_collector_error=2, robots_denied=1 | NOT_VERIFIED |
| ok | 4 | 0 | 0 / 0 | 0 / 0 / 0 | other_collector_error=4 | NOT_VERIFIED |
| rss | 4 | 0 | 0 / 0 | 0 / 0 / 0 | other_collector_error=4 | NOT_VERIFIED |
| rutube | 4 | 0 | 0 / 0 | 0 / 0 / 0 | other_collector_error=4 | NOT_VERIFIED |
| telegram | 4 | 0 | 17 / 17 | 0 / 2 / 2 | other_collector_error=4 | NOT_VERIFIED |
| threads | 4 | 0 | 0 / 0 | 0 / 0 / 0 | other_collector_error=4 | NOT_VERIFIED |
| tiktok | 4 | 0 | 0 / 0 | 0 / 0 / 0 | other_collector_error=4 | NOT_VERIFIED |
| vk | 4 | 0 | 0 / 0 | 0 / 0 / 0 | other_collector_error=4 | NOT_VERIFIED |
| x | 4 | 0 | 0 / 0 | 0 / 0 / 0 | robots_denied=4 | NOT_VERIFIED |
| youtube | 4 | 0 | 0 / 0 | 0 / 0 / 0 | other_collector_error=4 | NOT_VERIFIED |

Freshness рахує вік original published_at, а не discovery time або нові archive inserts.
Нуль fresh_5m не доводить, що sources мали нові публікації, які collector пропустив.
Без незалежної public freshness перевірки такого висновку не робимо.

## Підтверджені дефекти та мінімальні виправлення

1. `pilot_runtime.coverage` зводив http_404/http_5xx, network/TLS errors, native reader
   statuses і programmer exceptions до other_collector_error, а частину native readers
   до other_reader. Приклади baseline: Threads reader ok=4, Instagram rss_bridge ok=3,
   OK skraper ok=4, але parent outcomes залишились прихованими. Новий
   `collection_diagnostics.py` задає fixed public allowlists. `_error_code` розрізняє
   прямі TLS/network exceptions, TypeError/KeyError/NameError/AttributeError та missing
   dependencies. `error_code` записує bounded diagnostic entries: fixed code/type/phase,
   allowlisted module та line. Exception text, traceback paths, args, URLs не експортуються.
2. `run.collect_one`: non-streaming incremental metrics exception проходив до outer
   source failure handler і відкидав вже extracted posts. На baseline-коді відтворено
   controlled TypeError після native Telegram extraction: status collection_failed,
   source posts=0, posts.jsonl records=0. Після зміни на тих самих synthetic даних:
   status ok, source posts=1, records=1. Metadata працює через bounded_refresh з копією
   даних, власним time budget і catch. Це verified code defect, але НЕ доказ, що саме
   цей TypeError стався у baseline live run.
3. Pilot мав outer source deadline 30 с, але non-streaming native history policy —
   45 с, 12 Telegram pages, 12 YouTube details. Новий --bounded-poll активує наявний
   bounded режим без вимоги source lease/spool. Fixtures підтверджують: native budget
   25 с, cold Telegram head 1 page, RSS/Mail.ru <=5 pages, YouTube 2 details.
   Історичні continuations не видаляються; durable cursors у pilot не читаються/не пишуться.
4. `http_pool.request`: asyncio cancellation не припиняє sync worker. Pool wait +
   HTTP timeout тепер обмежені remaining source deadline з 1 с finalization headroom.
   Expired deadline не починає HTTP request. Retry/redirect/TLS verification не послаблені.
5. Outer source exception раніше завжди мав tool=source_deadline, навіть при TypeError.
   Тепер лише timeout має цей tool; інші failures — source_execution, з phase diagnostics.
6. Public coverage окремо показує execution_status та completed/failed/partial/restricted/
   deferred checks, collection_health і sanitized error diagnostics. Reader ok не
   зараховується як completed source. Warnings явно пояснюють, що job completion не
   встановлює collector health. Немає автоматичного HEALTHY лише через records.

## Що НЕ вдалося ретроспективно довести

Baseline не публікував source raw status, error_type або traceback location. Report files
та raw collector log були тимчасовими, artifacts з ними відсутні. Тому точні винятки
за всіма 40 other_collector_error зараз NOT_MEASURABLE. Не можна приписувати їх усі
одному bug або вважати всі успішні reader attempts валідними accepted posts.
RSS/YouTube: 4+4 hidden outcomes без records; причина кожного потребує нового safe
instrumented run. Telegram: 17 dated records, але всі source outcomes hidden; 3 sources
без records. Часткові extraction, пізня normalization/enrichment failure, HTTP errors
та parser/native statuses залишаються різними гіпотезами, не підтвердженими live causes.
X robots_denied=4, Mail.ru robots_denied=1, Facebook login_required=1 та Instagram
http_429=1 — підтверджені restrictions; їх не обходили і не трактували як parser bugs.

## Валідація

12 targeted suites, 92 tests PASS у cached Python 3.12: recovery=10, pilot=20, workers=3,
collect=7, completeness=4, HTTP pool=3, targeted recovery=12, website repairs=8,
feed formats=2, YouTube feed=9, admission=3, depth order=11. Security policy: 10 PASS
у system Python. Existing platform_workers.py та його regression tests незмінні.
Нові integration fixtures використовують inline mocked thread dispatch тільки у tests
для deterministic execution під sandbox; production thread implementation не замінено.
Не повторюємо попередній повний suite як green: дев’ять async/thread suites були
незавершені у sandbox; real Crawlee/Crawl4AI та exact Docker тут не перевірені.

## Live after та блокер запуску

Новий Fast/Extended run ще не виконано. After source success, records, dated records,
freshness та error distributions: NOT_VERIFIED для кожної platform, не нулі.
Локальний git fetch не має DNS-доступу до github.com; gh auth status повідомляє про
недійсний default token. Доступний GitHub connector читає baseline, але workflow
dispatch API не представлений доступними tools. Потрібні авторизовані GitHub write/
Actions dispatch можливості для публікації patch у draft branch та повторного запуску.
Повторний create-tree через connector для готового patch повернув HTTP 403
Resource not accessible by integration; remote branch лишився на baseline SHA.
Не запускаємо baseline повторно й не видаємо його за перевірку нового коду.

Після підтвердженого push до recovery/runtime-phase1 (без merge):

```bash
gh workflow run argus-public-collection.yml --repo cutebigmonkey-team/ArgusPRO-Public --ref recovery/runtime-phase1
```

Цей manual workflow запускає обидва lanes. Порівнювати треба assigned/started/reported,
completed_checks, sources_with_posts, extracted/dated records, freshness 5/30/60 хв,
statuses та error_diagnostics усіх shards, а не job conclusion. Не сумувати повторені
catalog configured counts між shards. Підтвердити head SHA нового run, read-only mode
та checkpoint_advanced=false. PR #6 не merge, Hostless не deploy, archive delivery не enable.
Поточний patch не є заявою про production recovery.
