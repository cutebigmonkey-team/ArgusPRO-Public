# Dependency recovery — run 44241204882

Статус: IMPLEMENTED / TESTED_OFFLINE; новий live pilot NOT_VERIFIED.

Baseline: [run 44241204882](https://github.com/cutebigmonkey-team/ArgusPRO-Public/actions/runs/44241204882),
commit `89d57a1580365acf02bbd524ef338e9040954bf6`, 2026-11-18 07:00:36–07:04:32 Europe/Kyiv.
Прочитано sanitized summaries усіх восьми collection shards. 47/47 sources accounted;
20 records / 20 dated: Telegram 17, Facebook 3. Sources fresh <=5 хв: 0.

## Точна причина

Усі 27 dependency_missing outcomes мають однакову diagnostic точку:
`extraction:dependency_missing:ModuleNotFoundError:run.py:766`.
Це `from source_adapters import WALL` у optional metrics, до захисного try/catch.
`source_adapters.py` одразу імпортує requests; його remaining_http також потребує requests.
Основний ops/collect/requirements.txt не містив requests. Повний Docker dependency
graph міг приховувати це через Drive/OAuth SDK dependencies; новий minimal-manifest
CI job навмисно не встановлює Drive requirements. У main interpreter Fast та
Extended pip receipts його немає. Extended встановив requests лише у двох окремих venvs
для gallery-dl та fbgql/granary; вони не задовольняють imports процесу python run.py.

Ланцюг: reader повернув валідні posts → metrics guard → імпорт adapter access constant
→ відсутній requests у main interpreter → outer failure → posts не доходять до reporting.
Це пояснює успішні reader attempts разом із dependency_missing parent source.
Baseline diagnostics не містив ImportError.name; requests встановлено через call site,
import graph, installation receipts та відтворення без цього пакета, а не через
публікацію raw exception messages. Інших live import failure sites не зафіксовано.

ImportError (окремий клас) та circular import у live summaries: 0 зафіксованих.
Не приписуємо baseline непідтверджений internal/circular defect. Fresh-process
imports у прямому, зворотному та adapter-first порядках проходять із dependencies.

## Виправлення на правильному рівні

- Main collector manifest: requests==2.34.2 та прямий transport dependency urllib3==2.8.0.
  Обидві версії підтверджено наявними installation receipts baseline; ті самі версії
  встановлені в локальному cached Python 3.12, яким виконано import smoke tests.
- WALL перенесено до lightweight cloud_social; source_adapters реекспортує той самий
  об’єкт. Його набір restrictions тотожний попередньому. Metrics імпортує policy без
  adapter SDK. Native RSS/Telegram/YouTube не потребують requests лише заради константи.
- ModuleNotFoundError для відомих internal modules: internal_module_missing;
  missing external dependency: dependency_missing; ImportError symbol/invalid import:
  internal_import_error; CPython circular-import marker: circular_import_error.
- ImportError.name експортується тільки як allowlisted public module root. Невідомі
  значення стають NOT_VERIFIED. Exception messages, source URLs і private paths не логуються.
- collector_import_smoke.py імпортує 15 core/lane modules у main interpreter кожного lane.
  Прямий і reverse порядок — різні процеси. Network connections/DNS у smoke заборонені.
- Live workflow запускає main smoke до source admission, окремо перевіряє embed та
  remaining venvs. PR CI має незалежні fast/extended matrix jobs із точним main manifest,
  без Drive dependencies, catalog secrets, login sessions чи deployment.
- У disposable Docker test stage додано COPY public-validation.yml для нового workflow
  contract test. Попередні Docker workflow fixtures збережено; production stage не змінено.

## Before / after

Публічні aggregate результати baseline наведено нижче. Source ok — source outcome,
а не успішний GitHub job. Fresh означає record published_at, не discovery time.

| Platform | Sources | Source ok | Records / dated | Fresh records 5/30/60 хв | Source outcomes | Live after |
|---|---:|---:|---:|---:|---|---|
| facebook | 4 | 3 | 3 / 3 | 0/0/0 | login_required=1, ok=3 | NOT_VERIFIED |
| instagram | 4 | 0 | 0 / 0 | 0/0/0 | dependency_missing=3, http_429=1 | NOT_VERIFIED |
| mailru | 3 | 0 | 0 / 0 | 0/0/0 | dependency_missing=2, robots_denied=1 | NOT_VERIFIED |
| ok | 4 | 0 | 0 / 0 | 0/0/0 | dependency_missing=4 | NOT_VERIFIED |
| rss | 4 | 0 | 0 / 0 | 0/0/0 | dependency_missing=1, robots_challenge_required=2, unverified_markup=1 | NOT_VERIFIED |
| rutube | 4 | 0 | 0 / 0 | 0/0/0 | dependency_missing=4 | NOT_VERIFIED |
| telegram | 4 | 0 | 17 / 17 | 0/1/2 | dependency_missing=3, partial_telegram_cards=1 | NOT_VERIFIED |
| threads | 4 | 0 | 0 / 0 | 0/0/0 | dependency_missing=4 | NOT_VERIFIED |
| tiktok | 4 | 0 | 0 / 0 | 0/0/0 | dependency_missing=3, unverified_markup=1 | NOT_VERIFIED |
| vk | 4 | 0 | 0 / 0 | 0/0/0 | login_required=1, timeout=1, unverified_markup=2 | NOT_VERIFIED |
| x | 4 | 0 | 0 / 0 | 0/0/0 | robots_denied=4 | NOT_VERIFIED |
| youtube | 4 | 0 | 0 / 0 | 0/0/0 | dependency_missing=3, http_404=1 | NOT_VERIFIED |

Контрольний synthetic before/after на восьми sources з навмисно недоступним requests:
baseline 89d57a1 — 8 dependency_missing, 0 records / 0 dated; patch — 8 ok, 8 records /
8 dated. Це fixtures для RSS, Mail.ru, YouTube, Telegram, Threads, Instagram, OK, RuTube,
НЕ реальні публікації. Вони проходять реальні normalization, merge та report paths;
network collectors/thread dispatch замокані тільки в тестах. Durable cursor paths відсутні.

## Валідація та блокери

20 selected collector suites: 158 tests виконано, 145 PASS, 13 existing hosted-only SKIP,
0 failures/errors/timeouts. Нові import regression tests: 9 PASS, 0 SKIP. Security policy:
10 PASS у system Python. JSON/YAML parsing і git diff --check проходять.
13 skips — наявні pinned/browser adapters без встановлених upstream tools у cached
environment: cloud_social 3, embed 4, remaining 6. Їх не вимикали й не рахували як PASS.
Попередні дев’ять sandbox async/thread timeout suites та real-engine/full Docker gates
цим targeted прогоном не вважаються завершеними. Fresh main-manifest installation та
справжні Extended venv smoke перевірки на GitHub ще NOT_VERIFIED.

Новий CI і live pilot потребують push до draft branch та Actions dispatch. Локальний
git fetch заблокований DNS; gh auth status повідомляє про недійсний default token.
GitHub connector дозволив read-only baseline diagnostics; remote branch на час перевірки
залишається 89d57a1. Спроба create-tree для готового patch повернула HTTP 403
Resource not accessible by integration. Не запускаємо старий commit повторно як перевірку patch.

Після успішної публікації corrected SHA авторизований manual run:

```bash
gh workflow run argus-public-collection.yml --repo cutebigmonkey-team/ArgusPRO-Public --ref recovery/runtime-phase1
```

Порівняти новий head SHA, усі 8 shard summaries, source outcomes, accepted/dated records,
freshness 5/30/60 хв і import diagnostics; job completion не є collection health.
Поки нового run немає, усі live after значення — NOT_VERIFIED, а не нулі.

## Незмінні обмеження

Approved source admission, robots restrictions, deadlines, pacing, rate limits та lease/
checkpoint safety збережені. Не обходили X robots, Facebook/VK login walls, Instagram
HTTP 429 чи RSS robots challenges. Немає нових accounts, cookies, paid services або
infrastructure. Archive delivery disabled; durable cursors не просуваються. PR #6
не merge, Hostless не deploy. Жодна platform не оголошена recovered без live evidence.
