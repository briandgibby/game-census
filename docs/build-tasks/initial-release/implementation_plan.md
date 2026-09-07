# Game Census: implementation plan

P2 acceptance is recorded in the [P2 closeout](walkthrough.md#p2-closeout--2026-09-07), combining preserved live-canary evidence with current integration and diagnostic tests. Continue P3/P4 on `codex/feat-p2-p4`, based on `develop`; no PR. The [P2 integration packet](../p2-integration/implementation_plan.md) and older collection/storage packets remain dated evidence.

Catalog, global charts and game profiles were merged separately and are retained by this integration. Their presence does not complete the remaining P3/P4 acceptance tasks. The first-usable contract below is historical.

Status: **first usable P0–P1 build verified locally; broader roadmap remains proposed**. References: [PRD](../../PRD-game-census.md), [PRS](../../PRS-game-census.md), [checklist](task_checklist.md), [execution brief](agent_prompt.md).

## Historical first usable build contract

The authorized outcome is a local, manual one-game installation with real counts, persisted history, a read API and a usable website. `python tools/dev.py quickstart --once --app-id 570` creates configuration/secrets, initializes PostgreSQL, runs one bounded collection and serves the result. `python tools/dev.py app collect --once` records the next manual observation. Scheduling is absent. Initial settings accept 1–25 configured game IDs and default to one. Existing configuration is preserved on rerun. The app is published only on loopback.

This slice uses the planned Python/FastAPI/PostgreSQL stack. The registry lives in `sources/__init__.py`; adapters expose module-level `fetch`/`parse`, and projections expose `project`. `db.py` owns durable attempt accounting and insert-only replay. Two additive migrations contain the bounded slice's tables and source cooldown events; partitioning, scheduler jobs, rollups and catalog reconciliation remain future work. These are deliberate simpler contracts for the first running version.

The website uses Jinja templates (`index.html`, `game.html`, `status.html`, `methodology.html` and shared partials), local CSS/JavaScript and native SVG with a data-table alternative. An external chart library is unnecessary for the initial bounded series. Optional Store integration retains only an allowlisted app name and has its own request budget; full Store/price/review/achievement/news enrichment remains P4. Last-valid observations and last-attempt errors are separate. Query limits reject excess points rather than silently truncating them.

Actual command inventory is exposed by `python tools/dev.py --help` and `uv run --frozen game-census --help`. Application commands include `config init`, `config describe --schema`, `initialize`, `collect --once`, `apps`, `history --app-id`, `report`, `aggregate rebuild`, `doctor` and `serve`. The wrapper provides `build`, `start`, `stop`, `status` and `test`. Tests use synthetic fixtures and isolated scratch schemas, never sample rows inserted into the live UI. Commands listed in the phase inventories below that are absent from help remain future acceptance contracts.

`uv.lock` owns resolved dependencies; `requirements.lock` is derived with `uv export --frozen --all-groups --no-emit-project --output-file requirements.lock`. `build/toolchain.lock.json` owns runtime image digests and the build-tool version. Docker receives those values through the shipped wrapper. Application build evidence and all acceptance limitations belong in the completion walkthrough.

## Goal and decision points

### Authorized P2–P4 continuation

Finish P2 acceptance, then P3 and P4 while their required source/configuration gates permit progress. Use feature branch `codex/feat-p2-p4` from `develop`; no PR or public deployment. The successful fixed-window canary ran on its preserved P2 image, so retain that provenance separately from current integration tests. Reconcile the existing catalog/profile implementation with each remaining requirement before designing replacements.

First increment (FR-10, NFR-01): MODIFY `src/game_census/scheduler.py` to preserve bounded exception types, SQLSTATE and application frame locations without exception messages, locals, source lines or credentials. Emit the failure on stderr before report finalization; if finalization itself fails, emit an explicitly unpersisted record and stop. NEW `tests/test_scheduler_diagnostics.py` reproduces loss of interruption context, uncertain dispatch and report-write failure with synthetic responses in retained scratch schemas. Trace: `cli.main` schedule run → `scheduler.run` → durable report → `report --last-run`; unavailable persistence → stderr plus nonzero CLI result. No schema or source-request behavior changes.

Verification from repository root: `python tools/dev.py --instance p2-integration build`, `python tools/dev.py --instance p2-integration start`, then `python tools/dev.py --instance p2-integration test tests/test_scheduler_diagnostics.py`. Retain failing output before the code change and passing output afterward; follow with scheduler/coverage tests and the full suite. The dedicated instance uses synthetic sources only. Its existing configuration is generated by the shipped configuration interface.

Implement the PRD's P0-P5 release through a working one-game slice, reliable history, searchable statistics, optional enrichments, and demonstrated recovery. Each phase keeps a command that runs the entire implemented system against real input. P6 is a research backlog, not part of the initial release commitment.

Use the PRS's proposed Python/PostgreSQL/server-rendered website design during later authorized implementation. Deferred decisions and their gates live in [BACKLOG.md](../../BACKLOG.md). An API key is an external configuration fact when catalog/achievement-schema capabilities are enabled. Do not search local files for a credential or request one for the initial keyless slice. No model service is required.

## Current evidence versus proposed commands

`python tools/probe_sources.py` exists and was run once during planning. The [unedited capture](evidence/source-probes.json) contains three successful bounded source checks. The old planning evidence remains historical. The source remote is [briandgibby/game-census](https://github.com/briandgibby/game-census); Git owns current commit and synchronization state. The first usable build contract above describes the locally verified implementation; the checklist and walkthrough distinguish tested behavior from proposals.

The remaining phase inventories are **full-product acceptance contracts**. Use the first usable build contract and command help for the available subset. Product test commands were not run during planning; implementation evidence is recorded separately. Exact pins are now selected in the toolchain and dependency locks.

## Proposed file changes

The phase inventories below are the **full-product target inventory**. The first usable build contract and walkthrough identify the implemented subset and exact current names. Other paths remain NEW, planned; README is modified. Package `__init__.py` files are created where required. No existing application code is removed. File group identifiers are inventory references, not separate requirement IDs.

### Group A: reproducible bootstrap and interfaces — P0

| File | Responsibility / proposed symbols | Integration and verification |
|---|---|---|
| `pyproject.toml` | Package/CLI metadata and exact direct dependencies; test extras | Pinned resolution, package build and smoke command |
| `uv.lock` | Fully resolved dependency graph with hashes | Frozen install; no resolution during release build |
| `.python-version` | Exact tested interpreter release | Same version in local tooling/container lock |
| `build/toolchain.lock.json` | Exact runtime/platform, installer, image digests, external asset and CI-tool versions | Canonical toolchain owner; generated Docker inputs/action references checked against it |
| `Dockerfile` | Build/package from pinned inputs; deterministic artifact stage | No floating tags, network dependency resolution or wall-clock metadata in release artifact |
| `compose.yaml` | App and PostgreSQL services/volumes with scheduler off | Generated local configuration supplied by `tools/dev.py`; DB health dependency |
| `tools/dev.py` | `main()` commands `doctor`, `quickstart`, `app`, `sync-toolchain`, `test`, `bench`, `repro-build` | Standard-library host wrapper; validates prerequisites and invokes pinned container commands without shell interpolation |
| `src/game_census/__init__.py` | Package identity/version | Version source consumed by CLI and API |
| `src/game_census/__main__.py` | `main()` package entry point | Calls `cli.main()`; preserves exit status |
| `src/game_census/cli.py` | Command registration, typed arguments, exit/status convention | Wires all phases; no hidden operator action in README |
| `src/game_census/config.py` | `Settings`, `load_settings()`, `generate_profile()`, `describe_settings()` | Sole name/default/bounds schema; produces config and its docs; rejects unsafe inputs |
| `tests/test_config_cli.py` | Boundary and empty-state CLI checks | FR-01, NFR-01, NFR-04; secrets never in error/help/export output |

P0 pins a candidate toolchain, checks dependency licenses, builds it, and records those exact inputs. `python tools/dev.py sync-toolchain` derives `.python-version`, container/asset/action pin references from `build/toolchain.lock.json`; `--check` detects drift without writing. Package requirements and `uv.lock` own package resolution rather than duplicating it in the toolchain lock. Frozen locks alone do not prove byte-identical artifacts: normalize timestamps (`SOURCE_DATE_EPOCH` from the release source input), ordering, locale, platform and archive metadata; keep provenance signatures/timestamps outside the canonical artifact comparison. Mirror required build dependencies when offline builds are a release requirement. Docker engine and Python host prerequisites must be named by `doctor` with installation guidance; no manual database/file/secret initialization may be required. The container is the reference runtime across host operating systems.

`quickstart --once` creates missing local directories, generated config and local DB secret, starts the DB, runs initialization, collects the configured one-game profile exactly once, starts the web service, and prints its actual URL plus a run report. It performs no schedule enablement. Missing external prerequisites stop with a named action. Existing state is reused after validation, never overwritten. A later run against existing config cannot silently reset watchlists or credentials.

### Group B: source contract, persistence and first page — P0-P1

| File | Responsibility / proposed symbols | Integration and verification |
|---|---|---|
| `src/game_census/sources/base.py` | `SourceRequest`, `SourceResult`, `SourceAdapter` protocol | Typed outcome/provenance used by collector and probe CLI |
| `src/game_census/sources/registry.py` | Registered source IDs, capability metadata, adapter factory | No dynamic untrusted module loading; config enumerates known sources |
| `src/game_census/sources/http.py` | Bounded HTTP client, allowed hosts, header redaction, response limits | Shared by every adapter; no adapter may bypass accounting hook |
| `src/game_census/sources/players.py` | `CurrentPlayersAdapter.fetch()` and parser | Provider result and zero validation; one app/request |
| `src/game_census/db.py` | Transactions, connection roles, migration locking | Explicit SQL through Psycopg; DB URL read from validated config |
| `src/game_census/migrations/001_initial.sql` | App/capture/run/audit schema and migration version | Reentrant initializer and UTC constraints |
| `src/game_census/collector.py` | `collect_once()` and capture commit/projection invocation | Source → ledger → sample → run report; no aggregate success on partial failure |
| `src/game_census/projections.py` | `project_capture()` / `rebuild_projections()` | Deterministic parser-versioned projections, replay idempotency |
| `src/game_census/web.py` | `create_app()`, routes, templates, static mounts, read repository calls | Initializes read-only serving role; no collection on request |
| `src/game_census/contracts.py` | API schemas, availability/error enums | OpenAPI and JSON serialization own the shared browser/API contract |
| `src/game_census/templates/base.html` | Common layout and source/methodology links | Escaped rendering, usable keyboard navigation |
| `src/game_census/templates/app.html` | Player panel and initial history view | One point and no-history cases; last-attempt state distinct from last valid sample |
| `tests/test_sources.py` | Deterministic adapter fixtures; later enriched adapters extend it | FR-02, FR-07, FR-08, FR-09, NFR-01; HTTP/schema failures and privacy filtering |
| `tests/test_bootstrap.py` | Empty-volume integration through CLI/API/browser | FR-01, FR-02, NFR-02; rerun preserves observations |

P0 first demonstrates live `probe`; P1 extends it to persistent capture and a web page. Initialization applies shipped migrations, creates partitions and required service state, registers source capabilities, and installs only the generated bounded tracking profile. No SQL inserts or hand-placed configuration files are part of setup.

Golden fixtures for adapters must include both source-like synthetic edge cases and sanitized licensed captures with provenance. Live responses are evidence, not expected-value fixtures for exact current counts. Do not put test sample observations into a real installation and present them as live Steam data.

### Group C: history and reliable scheduling — P2

| File | Responsibility / proposed symbols | Integration and verification |
|---|---|---|
| `src/game_census/migrations/002_history_jobs.sql` | Jobs/attempts/quota/tracking intervals, time partitions, occurrence dedupe registry, rollup tables | No global unique constraint that omits a required partition key |
| `src/game_census/scheduler.py` | `plan_schedule()`, `claim_jobs()`, `run_scheduler()`, `acknowledge_run()` | Dry-run target report, bounded materialization, leases/fencing, watched-run effective collection-plan hash |
| `src/game_census/quota.py` | `reserve_attempt()`, `mark_dispatched()`, rolling-window admission | Persistent atomic ledger, per-host/global scopes, retries and uncertain sends charged |
| `src/game_census/metrics.py` | `player_history()`, `observed_peak()`, `weighted_average()`, `coverage()` | Implements PRS definitions; supports raw/archive reads at range edges |
| `src/game_census/maintenance.py` | Partition creation, rollup rebuild and archive preparation | Safe CLI entry points; canonical pruning remains prohibited until group H proof |
| `tests/test_history.py` | Independent analytic datasets | FR-03, NFR-02; compare direct calculation to rollups/replay |
| `tests/test_scheduler.py` | Clock-controlled 24h simulation, multi-worker and crash tests | FR-04, FR-10, NFR-01; request budget and missing-period accounting |

Use fixtures with analytically known integrals and peaks, including 0, equal adjacent values, missing last observation, a pre-window point, a UTC midnight boundary, late arrivals, changing cadence, duplicate job delivery and a prolonged outage. Assert both numerator and denominator of averages, and rank exclusion of no-observation/stale apps. No wall-clock waits or mass Steam calls in scheduler tests.

At-least-once jobs may acquire one accepted canonical observation per occurrence; retry attempts are retained separately. A fencing token must prevent expired workers from finalizing somebody else's lease. Mark missed player jobs without executing stale historical work after restart. Every planned call, including operator probes routed through the application, goes through the same limiter. The separate historical planning probe is not shipped as a production quota bypass.

### Group D: catalog and source expansion — P3-P4

Current P3 catalog increment (FR-05, NFR-01/NFR-02): NEW `sources/catalog.py` registers `steam_catalog_v2`; keep `sources/discovery.py`'s v1 parser available for old captures. NEW `catalog.py` owns full/incremental/resume planning and bounded collection. MODIFY `config.py` with typed `catalog` page/interval/overlap/type policy, `cli.py` with catalog plan/status and sync dry-run, `collector.py` to delegate keyed catalog collection, `sources/__init__.py` to register v2, `db.py` to expose checkpoint state, and `projections.py` to project/verify capture-derived scan checkpoints. NEW additive `007_enrichment.sql` creates capture-derived `source_policy` and `catalog_checkpoint`; existing discovery snapshots already provide empty generic enrichment projections, so no duplicate price/review tables are needed before their contracts are implemented. MODIFY exact schema-version assertions in integration/storage tests to require version 7, retaining their upgrade assertions. NEW `tests/test_catalog.py` exercises full/partial/restart/incremental scans, stale complete watermark, repeated or unordered pages, reappearing apps, policy changes and scratch replay/restore. Existing UI/catalog consumers retain their response fields.

Page data and checkpoint commit in the same capture/projection transaction. A failed first page cannot replace the last successful scan; a failed later page cannot advance its cursor. Completed watermark uses the scan start, with configured overlap on the next incremental scan; incomplete scans never advance it. Periodic full scans retain previously discovered IDs. A bounded unfinished scan is explicitly partial and exits nonzero. Full source contract: [Valve IStoreService](https://partner.steamgames.com/doc/webapi/IStoreService), inspected 2026-09-07; requires `input_json`, ordered app IDs, continuation cursor, type flags and optional `if_modified_since`. Authenticated live validation awaits `sources.catalog_api_key`; synthetic results do not satisfy that gate.

The old keyed collection branch in `collector.py` owned latest-page cursor resume and terminal no-op behavior; it is replaced by the `catalog.sync_catalog` delegation, not removed from replay. MODIFY `storage.py` to create the two new derived tables when regenerating frozen legacy copies. `tests/test_catalog.py` verifies v1 captures plus v2 checkpoints through historical-main upgrade, backup/restore, protected cutover and legacy regeneration. MODIFY `tests/test_discovery.py` to inject the newly explicit CLI plan when testing argument delegation. MODIFY `README.md` and the PRS to describe the shipped commands and versioned capture contract; the walkthrough owns actual evidence and limitations.

| File | Responsibility / proposed symbols | Integration and verification |
|---|---|---|
| `src/game_census/sources/catalog.py` | `CatalogAdapter`, paginated/incremental discovery | Public-host key/header, explicit flags, stable cursor/progress checkpoints |
| `src/game_census/catalog.py` | `sync_catalog()`, `search_apps()`, `reconcile_cohort()` | App type/coverage, retention of old IDs, quota-preserving enrollment policy |
| `src/game_census/sources/store.py` | Optional metadata/price adapter and field allowlist | Country/currency identity, price state, app type, bounded Store budget |
| `src/game_census/sources/reviews.py` | Summary-only adapter and query identity | Fixed filter registry; strip personal/review records before persistence |
| `src/game_census/sources/achievements.py` | Schema and percentage adapters | Independent source IDs and schedules; join by source key |
| `src/game_census/sources/news.py` | News-link adapter, bounded count and dedupe | Source IDs/URLs/date/feed, no raw HTML rendering |
| `src/game_census/migrations/007_enrichment.sql` | Source policy versions, catalog checkpoints and enriched projection tables | Apply at the START of P3 before catalog work; P4 later consumes the empty enrichment tables; expand-only schema |
| `tests/test_catalog.py` | Pagination, resume, full reconciliation and cohort changes | FR-05, NFR-02; failed/partial scans never delete absent apps |

Wire each adapter through registry → config → planner → shared HTTP client → capture → projection → read API → page panel. One adapter is enabled at a time for its first bounded watched run. Extend `test_sources.py` with contract and failure fixtures before enabling it. Enrichment frequency is constrained by the full planner, not copied blindly across every tracked app.

A review snapshot records language, purchase type, review type, off-topic policy and source filter parameters; separate query identities produce separate series. For `filter=all`, fetch only the summary-bearing first page, request the smallest practical review page and never retain its reviewer records. Exact summary semantics must be checked before using “lifetime” or “recent” labels. News/schema/auth failures cannot suppress valid player history.

### Group E: complete read product — P3-P4

Current P3 read increment: NEW `src/game_census/queries.py` owns bounded UTC windows, configured comparison selection, aligned series and fresh active-cohort rankings. MODIFY `db.py` with `history_range()` over the existing exact cache; keep `history(hours=...)` as the compatibility wrapper. MODIFY `config.py` with `web.max_compare_apps` and `web.max_compare_points`; MODIFY `contracts.py` with typed ranking/comparison responses. MODIFY `web.py` to register `/api/v1/rankings`, `/api/v1/compare`, `/rankings`, `/compare` and explicit history bounds. Existing search/game/status/methodology remain consumers. NEW `templates/compare.html` and `templates/rankings.html`; MODIFY `base.html`, `index.html`, `game.html`, `_chart.html`, `static/app.js` and `static/app.css` for navigation, scope, aligned chart/table views, unique chart labels and explicit stored-data loading.

The same increment extends `/api/v1/apps` with opt-in `scope=catalog` and bounded `page`/`page_size`, retaining the enrolled-list default for existing polling clients. NEW query functions `app_summary()` and `catalog_search()` use stored catalog rows and player projections, keeping catalog receipt time separate from player observation time. MODIFY `contracts.py` to represent known untracked apps with null player/tracking fields and explicit catalog provenance. MODIFY `config.py` with `web.max_page_size` (default 100, code bounds 1–500); MODIFY `db.py` to enforce the absolute 500-row search bound. Unknown IDs remain 404. MODIFY `tests/test_discovery.py` to replace the old enrolled-only detail expectation with known/untracked 200; its purpose was to prove discovery never enrolls an app, which remains asserted through null tracking/count fields and storage checks. MODIFY `tests/test_ui.py` for unique chart labels. Browser polling gains one in-flight request, a refresh-interval deadline and hidden/left-page cancellation; reproduce overlap with `tools/browser_read_check.py --poll-only` before changing it.

The profile auto-POST currently requests Steam details on a cold page load, contrary to FR-06. Reproduce it with a browser command before changing production code; preserve the explicit Refresh details POST. Its existing automatic-fetch block and 15-minute gate were intended to populate cold profiles and avoid refresh loops; replace that behavior with stored reads and explicit operator collection. NEW `tools/browser_read_check.py` and `tests/test_p3_reads.py` verify cold page loads, common windows, stale/zero/unsupported/untracked states, malformed/oversized queries, storage failure and desktop/mobile keyboard/data-table navigation. MODIFY `tests/test_details.py` to replace its prior automatic-refresh expectation with the authorized read contract. Source-data capture and collection commands are not invoked by any page load. Keep partial source failures distinct from missing samples.

Verification from repository root: `.venv/Scripts/python.exe tools/browser_read_check.py --profile-only` before and after the page-load change; after a pinned image build, `python tools/dev.py --instance p2-integration test tests/test_p3_reads.py tests/test_api.py tests/test_details.py tests/test_ui.py`; then full tests and the browser command without `--profile-only`. All browser state is synthetic and all external assets are intercepted. No UI library or dependency upgrade is required; extend the existing native SVG chart and table.

| File | Responsibility / proposed symbols | Integration and verification |
|---|---|---|
| `src/game_census/queries.py` | Bounded search/ranking/history/comparison SQL services | Freshness scope and pagination; web calls these services directly |
| `src/game_census/templates/index.html` | Tracked cohort rankings and search | Banner with cohort, source, generated time and stale exclusions |
| `src/game_census/templates/compare.html` | Up to configured limit of games with aligned UTC windows | Per-series tracking/coverage; no forced interpolation across gaps |
| `src/game_census/templates/methodology.html` | Human explanation generated from canonical metric/source labels | Terms/attribution, observed peaks and query-qualified enrichment |
| `src/game_census/templates/status.html` | Sanitized instance capability and freshness state | No secrets or admin write controls |
| `src/game_census/static/app.js` | Chart/compare loading, cursor navigation and visibility-aware refresh | Reads versioned APIs; cancels superseded requests; safe text rendering |
| `src/game_census/static/app.css` | Responsive layout, focus and contrast styles | Mobile, keyboard and reduced-motion checks |
| `src/game_census/static/vendor/echarts.min.js` | Exact vetted/pinned asset and license notice | Derived from checksum-pinned asset lock; no live CDN requirement |
| `tests/test_api.py` | JSON contracts, query bounds, status codes and injection cases | FR-06, FR-07, FR-08, FR-09, NFR-01, NFR-03 |
| `tests/test_ui.py` | Browser flow, accessibility and state scenarios | FR-06; rendering against recorded data, never triggers real upstream collection |

Modify the new `web.py`, `contracts.py`, `app.html` and `base.html` as this phase extends the vertical slice; they remain part of the same inventory. Register these exact routes with bounded GET parameters:

| Route | Service / visible consumer |
|---|---|
| `/api/v1/apps` | `search_apps()` → search/rankings navigation |
| `/api/v1/apps/{app_id}` | App/source capability summary → game page |
| `/api/v1/apps/{app_id}/players` | Latest valid sample + current availability → count panel |
| `/api/v1/apps/{app_id}/history` | `player_history()` with `from`, `to`, `resolution` → chart/table |
| `/api/v1/rankings` | Fresh tracked-cohort ranking snapshot → home |
| `/api/v1/compare` | Bounded `app_ids`, common window → comparison |
| `/api/v1/apps/{app_id}/prices` | Country/currency-qualified observations → price panel |
| `/api/v1/apps/{app_id}/reviews` | Query-qualified summary series → reviews panel |
| `/api/v1/apps/{app_id}/achievements` | Supported percentage/schema data → achievement table |
| `/api/v1/apps/{app_id}/news` | Bounded linked entries → news list/markers |
| `/api/v1/status`, `/health/live`, `/health/ready` | Health definitions from PRS → status/operations |
| `/`, `/apps/{app_id}`, `/compare`, `/methodology`, `/status` | Escaped HTML templates, GET-only |

Generated OpenAPI is derived from contracts/routes. A public `query` string accepts bounded plain text, no user SQL. History queries must distinguish no data, disabled tracking and out-of-range archive availability. Do not discard unsupported points from a comparison silently.

### Group H: operations, release and evidence — P5

| File | Responsibility / proposed symbols | Integration and verification |
|---|---|---|
| `src/game_census/operations.py` | `doctor()`, `report_run()`, `backup()`, `restore_scratch()`, `verify_restore()` | CLI reports/exit codes; safe destination validation; backup manifests |
| `tests/test_recovery.py` | Backup/restore/upgrade/replay and archive owner transition | FR-10, NFR-02; counts, hashes, representative queries and timing |
| `tests/test_capacity.py` | Synthetic 90-day load, percentile/size measurements | NFR-03; no live Steam load; full tracked-time freshness denominator |
| `LICENSE` | Proposed Apache-2.0 code license after owner decision | Does not apply a new license to Steam data/artwork |
| `CONTRIBUTING.md` | Reproduction-first fixes, local commands, review/evidence protocol | Generated command references; pin upgrades deliberate |
| `SECURITY.md` | Vulnerability reporting and supported release policy | Avoid publishing credentials/PII in issue reports |
| `.github/workflows/ci.yml` | Locked tests/build/license/secret/reproducibility checks | Immutable action SHAs; offline fixture tests by default |
| `README.md` — MODIFY | Replace plan-only entry point with actual install/run/limitations and links | Every setup action implemented by CLI; no hand-authored state |
| `docs/build-tasks/initial-release/walkthrough.md` | Actual changed-file and acceptance evidence only after implementation | Unedited commands/output, deviations, limitations and recovery proof |

Backup manifests include source ranges, checksums, parser/schema versions and restore commands. Audit pruning separately from backup creation. Full restore never defaults to the live database. Phase completion requires actual failing/passing evidence for every bug fixed, and a per-file explanation of final changes.

## Integration traces and dependency order

| Capability | Complete path | Inventory groups |
|---|---|---|
| Empty-state run | `tools/dev.py` → compose/build → CLI/config → DB migration → source registry → collector → projections → web/contracts/templates | A → B |
| Reliable player history | CLI scheduler plan/ack → job/lease/quota → HTTP/players → capture → projections/metrics → queries → history API → app.js/chart/table | A/B → C → E |
| Catalog and cohort | CLI/config → catalog adapter/shared HTTP → atomic cursor/capture → catalog projection → cohort scheduler/search → apps API/index | A/B/C → D → E |
| Enrichment | Registry/config/planner → relevant adapter → capture allowlist → enriched projection → query/API → game panel | A/B/C → D → E |
| Health/recovery | Runs/attempts → operations/queries → CLI/status/health → backup → scratch restore → projection replay → verified archive transition | B/C/D/E → H |

Every registration, caller, consumer and migration in these traces is inventoried above and mirrored by file group in the checklist. Work on contracts before parallel consumers; migrations and registry/config integration have one owner to avoid conflicting edits. P2 must pass before broad P3/P4 collection. UI construction may run alongside offline adapter tests after contracts are stable.

## Proposed CLI and verification contracts

Working directory for all commands: repository root. Wrapper commands run the pinned environment. `python tools/dev.py app -- COMMAND` dispatches the following argument list to the pinned application container with the generated configuration and storage mounts, with no activation step. The concrete commands below use this wrapper; `python -m game_census` remains the container's internal entry point. Commands below are **not run during planning** and are acceptance contracts for the files named above.

| Command | Purpose / verification source to add |
|---|---|
| `python tools/dev.py doctor` | Check named host/container prerequisites and pins; `tools/dev.py` |
| `python tools/dev.py sync-toolchain --check` | Read-only check that generated toolchain pin references match their lock owner; `tools/dev.py` |
| `python tools/dev.py quickstart --once` | Entire first-run flow from empty state, one default game; `tools/dev.py` + `test_bootstrap.py` |
| `python tools/dev.py app -- config init --profile local` | Generate config, dirs and local values without overwriting existing state; `config.py` |
| `python tools/dev.py app -- config describe` | Emit names/bounds/defaults/redacted effective config; `config.py` |
| `python tools/dev.py app -- init` | Idempotent schema/state initialization; `db.py`/migrations |
| `python tools/dev.py app -- probe --source steam_current_players_v1 --app-id 570` | Bounded one-source capability check through shared quota accounting; `sources/registry.py`, `quota.py` after P2 |
| `python tools/dev.py app -- collect --once` | One configured bounded run, explicit report and exit code; `collector.py` |
| `python tools/dev.py app -- serve` | Serve current stored scope using configured bind/port; `web.py` |
| `python tools/dev.py app -- schedule plan --dry-run` | Print bounded targets/writes/attempt cap/capacity and config hash; `scheduler.py` |
| `python tools/dev.py app -- schedule acknowledge --last-run` | Print candidate run/config and require a person to attest it was watched; refuses unsuccessful or changed-config runs; `scheduler.py` |
| `python tools/dev.py app -- schedule enable` | Validate stored acknowledgment and enable exactly that plan; `scheduler.py` |
| `python tools/dev.py app -- schedule run` | Run enabled profile with leases/quotas; cannot self-enable; `scheduler.py` |
| `python tools/dev.py app -- schedule disable` | Stop issuing new jobs, drain/cancel and report active attempts; `scheduler.py` |
| `python tools/dev.py app -- catalog sync --max-pages 1` | Bounded first authenticated catalog run; `catalog.py` |
| `python tools/dev.py app -- aggregate --all-recorded` | Deterministic projection/rollup rebuild of existing captures; `projections.py`, `metrics.py` |
| `python tools/dev.py app -- report --last-run` | Safe run outcomes, budgets, failures and next actions; `operations.py` |
| `python tools/dev.py app -- backup --once` | Write new backup with manifest; destination configured; `operations.py` |
| `python tools/dev.py app -- restore --latest-backup --scratch` | Restore to new generated scratch destination and verify contents; `operations.py` |
| `python tools/dev.py app -- archive plan --dry-run` | State source ownership, targets and recovery evidence required before moving; `maintenance.py` |
| `python tools/dev.py test --suite core` | Config/bootstrap/sources/history/scheduler/catalog/API suites; inventory tests |
| `python tools/dev.py test --suite ui` | Browser/accessibility states; `test_ui.py` |
| `python tools/dev.py test --suite recovery` | Upgrade/restore/replay checks; `test_recovery.py` |
| `python tools/dev.py bench --profile beta` | Defined synthetic dataset/20rps/hardware report; `test_capacity.py` |
| `python tools/dev.py repro-build --copies 2` | Two isolated builds of identical source inputs and canonical hash comparison; toolchain lock |

The literal `--last-run` and `--latest-backup` flags are deterministic selectors the CLI must implement and display before state change; they are not placeholders requiring hidden IDs. Explicit run/backup IDs may be added as alternatives. A command must report ambiguity instead of choosing an unrelated run/backup. Changing scope invalidates schedule acknowledgment. After P0, `--help` and dry-runs are the executable command-contract evidence, and this plan is reconciled with them.

## Verification and review plan

| Requirement | Independent proof / negative cases | Phase |
|---|---|---|
| FR-01 | New scratch volumes → quickstart → real persisted sample; second init is harmless; missing configured key identified | P0-P1 |
| FR-02 | Nonnegative real integer sample/API/page; 0 preserved; HTTP 200 with bad provider result rejected | P1 |
| FR-03 | Hand-computed irregular time series equals direct queries and rollups; gap and pre-window boundary tests | P2 |
| FR-04 | Multi-worker virtual-day plan stays within global and host budgets including crash uncertainty/retries | P2 |
| FR-05 | Paged catalog resumes; failed page never advances completed watermark; cohort is explicit and quota-bound | P3 |
| FR-06 | Search → game → compare → methodology; accessible no-data/stale/error paths; API no upstream network side effects | P3 |
| FR-07 | Paid/free/unavailable/region/currency fixtures; schema drift visible; new price series after currency switch | P4 |
| FR-08 | Summary/filter identity and removal/net-negative deltas; no user fields in capture/logs | P4 |
| FR-09 | Missing schemas, unsupported achievements, malformed URLs and duplicate/news updates | P4 |
| FR-10 | Fault injection + accurate nonzero report; complete scratch restore/replay and measured recovery | P2-P5 |
| NFR-01 | Settings/input type/range/path/URL checks; secret redaction and SQL/HTML injection resistance | Every phase |
| NFR-02 | Capture manifest/rebuild equality, archive ownership transition and destructive-operation guard | P1-P5 |
| NFR-03 | Realistic stored load, p95 latency and tracked-app-time freshness including missing data | P3-P5 |
| NFR-04 | Locked clean build, two matching artifact hashes, install/upgrade, code/dependency license report | P0-P5 |

Do not add tests merely mirroring implementation. Use independent expected results at critical boundaries. Unit/integration CI uses deterministic fixtures, and live adapter checks are separately tagged and bounded. First-run manual observation includes commands, actual URL, expected/observed page behavior, source requests/outcomes, and the person watching. Automated browser success alone does not authorize an unattended schedule.

Before a bug fix, record the failing command/output. Name the single diagnostic variable per experiment. Before deleting any code/check/column/setting, state its purpose and prove a restore path or independently runnable dead-code check. Do not remove a failing test to obtain a green run. After change, re-run the same reproduction plus relevant suites and save unedited output. Broader tests are justified by risk or new changes, not repeated without cause.

## Compatibility, migration and recovery

There is no legacy product contract. `/api/v1` becomes the first compatibility boundary. Maintain fixtures for every upstream response shape and schema evolution. Forward-compatible unknown optional fields may be ignored explicitly; missing required fields are errors, never defaults. Keep parser versions on captures/projections to replay historical data without mutating source evidence.

Migrations are transactional where PostgreSQL allows; separate index operations with different transaction requirements and record them. Add columns/tables before switching readers. Backfill derives from captures, not new current-count API queries. Full dataset archive/cutover must have a watched bounded run, manifest, successful scratch restore and explicit target validation. If an upgrade fails, stop with the current schema/application state; restore into scratch before considering live rollback. Do not execute a downgrade that drops acquired historical data.

## Effort, staffing and capacity budget

These are planning estimates for one experienced full-stack developer with part-time review/operations support, not a fixed quote. Overlap UI/offline adapter work only after shared contracts are settled.

| Phase | Focus | Person-weeks | Gate / dependency |
|---|---|---:|---|
| P0 | Feasibility, exact locks, configuration, live CLI | 0.5-1 | Keyless real source and reproducible base |
| P1 | Empty-state persistent website slice | 1-1.5 | Real data visible and preserved |
| P2 | Metrics, scheduling, quotas and fault tests | 1.5-2.5 | Reliable history before expansion |
| P3 | Catalog, search, ranks, comparisons and API | 1.5-2.5 | Player-statistics alpha |
| P4 | Optional enrichments with contract gates | 1.5-3 | Review/price/achievement/news beta |
| P5 | Restore, security, load, reproducibility and release docs | 1-2 | Operational release candidate |
| Total | Initial release | 7-12.5 | Add 25-40% reserve for source/access surprises |

Practical elapsed planning range: roughly 9-17.5 weeks with reserve for one primary developer. Two developers may shorten independent UI/adapter work but do not halve the data-correctness/recovery critical path. The first visible real-data slice should arrive in the first 1.5-2.5 person-weeks. P6 is separately estimated only after its source spikes.

No current hosting price is asserted. Start capacity modeling at the PRD's section 6 reference workload. Treat it as a benchmark target, not a sizing guarantee. Annual player observations follow the PRS sample schedule. Example sensitivity only: 0.5-2 KiB of database+index space per observation would imply roughly 12.5-50.1 GiB/year, excluding attempts/enrichment/WAL/backups. Replace this with measured `pg_total_relation_size` and actual archive compression. Hosting budget = measured compute + primary storage + retention copies + backup/archive storage + outbound traffic. Price that exact deployment with the chosen provider at release time.

## P6 extension research backlog

Run separate bounded spikes before promising: official chart extraction and source window/time-zone semantics; optional daily-player rankings without numerical DAU claims; monthly hardware survey normalization; Steam-wide online/download/support dashboards; tags/Deck compatibility/Workshop counts; public SteamKit2 app/build metadata via an explicit JSONL process contract. Do not acquire game licenses, create Steam bots, traverse private data, or run a long-lived protocol client during planning. Record source/rights/auth/rate evidence, commands, fixture contract and effort before adding an extension to the PRD.

## Handoff completion

This planning task ends with internally reviewed documents, source evidence, validation output and deterministic export. Product checklist items remain unchecked. A future authorized implementation must keep the packet synchronized, add exact resolved versions, replace proposed-command status with actual command evidence, and create the walkthrough from the final diff and observed outcomes.
