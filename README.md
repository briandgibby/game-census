# Game Census

A local Steam player-statistics application. The first usable version collects real current-player counts, retains its own observations in PostgreSQL, and provides game pages, recorded history, Steam catalog search and global most-played, top-selling and player-growth dashboards, methodology, source state and a read API.

P2 adds explicitly activated, bounded scheduled collection, peak-preserving history, monthly storage partitions, verified scratch restore and persisted exact rollups to the [product plan](docs/PRD-game-census.md). Installation never launches a scheduler. The merged catalog, dashboards and game profiles remain available; historical coverage starts with the first retained observation. Full phase acceptance remains tracked in the build checklist.

Feature work branches from `develop`; the current P2–P4 continuation uses `codex/feat-p2-p4`. The [P2 closeout](docs/build-tasks/initial-release/walkthrough.md#p2-closeout--2026-09-07) records the bounded canary and integration evidence. P3/P4 and public-release readiness retain their separate acceptance gates.

The [direct-source requirement](docs/PRD-game-census.md#1-purpose--vision) requires Valve-operated Steam origins for all external game data. Player counts come from the documented Steam Web API; optional game names come from Steam's own Store appdetails endpoint, which is undocumented. SteamDB supplies no data, history or fallback. Peaks and averages are local calculations over retained Steam observations. The production collector admits only its two fixed Steam HTTPS hosts and does not follow redirects.

## Run it

Prerequisites: Python 3.11 or newer for the standard-library wrapper, Docker Desktop with a running Linux engine, and internet access to the pinned image/package registries and the two Steam source hosts. Application Python and PostgreSQL run in pinned containers; no host PostgreSQL, Node.js or Steam API key is needed.

From this repository:

```powershell
python tools/dev.py doctor
python tools/dev.py quickstart --once --app-id 570
```

The second command prints what it will touch, builds the image, generates configuration and a local database secret, initializes storage, collects one game once and starts the website. The default URL is [localhost:8000](http://127.0.0.1:8000). It never enables a schedule. A partial source result exits nonzero and retains both its successes and failures for inspection.

The default profile requests a current-player count and an optional Store app name. It makes at most two upstream requests with the default settings. Subsequent reads of the website/API use stored data only.

## Daily commands

```powershell
python tools/dev.py app collect --once
python tools/dev.py app report --last-run
python tools/dev.py app apps
python tools/dev.py app history --app-id 570 --hours 24 --resolution auto
python tools/dev.py app aggregate rebuild
python tools/dev.py status
python tools/dev.py stop
python tools/dev.py start
```

`stop` retains configuration and the database volume. `start` applies additive schema changes and starts the existing instance without collecting. Empty installations create partitioned storage automatically. Existing populated legacy storage remains usable until explicitly migrated through the verified restore procedure below. `aggregate rebuild` replays retained captures, inserts missing projections, verifies contents/checksums and regenerates exact rollup caches. It stops on conflicts or an excessive rebuild range; use `--app-id` and `--hours` to bound cache rebuilding.

Run `python tools/dev.py build` followed by `python tools/dev.py start` after a source change. No command deletes, purges or resets canonical history. Scheduled workers are explicitly bounded and are never started by the website.

## Backup, restore and storage

The pinned application image includes matching PostgreSQL backup and restore clients. The configured database role needs permission to create a new database for scratch restoration; the generated local role has it. The backup contains application schema/data, including captures, tracking, attempts, outcomes and schedule evidence. Database credentials and cluster-wide roles are configuration facts, outside this application backup.

```powershell
python tools/dev.py app backup plan
python tools/dev.py app backup create
python tools/dev.py app backup plan --latest-backup
python tools/dev.py app backup restore --latest-backup
python tools/dev.py app storage plan
python tools/dev.py app storage migrate --latest-backup
python tools/dev.py app storage legacy-verify
python tools/dev.py app storage maintain
```

Backups contain immutable archive and manifest files under `storage.backup_path`. The local wrapper mounts `/state`; the default `/state/backups` maps to `data/instances/local/state/backups`. Each restore adds a new verification record. Restore creates a new database protected as read-only, imports through private write connections, and verifies table contents, sequences, payload checksums and projection replay. A failed restore also retains that protection. For the default `public` schema, restore verifies the new namespace is empty and reuses it; all data and constraint entries remain in the restore. It never overwrites an existing database. The returned `proof_id` can also be selected explicitly using `storage migrate --proof-id PATH`. A restore failure retains diagnostic records and incomplete output for inspection.

Migration rechecks the actual scratch copy and current source before converting legacy history. If the source changed since backup, take and verify a new backup. Keep collectors stopped for this operation. Legacy tables retain frozen migration copies; current queries use the partitioned tables. Migration membership is recorded so `storage legacy-verify` can regenerate those copies in scratch from their canonical owners and compare full contents. Monthly maintenance creates missing partitions for the configured lookahead and never detaches or deletes history. `--start-month YYYY-MM --months N` selects a bounded maintenance range. A late observation creates its required receipt-month partition through the same storage interface.

`storage.backup_max_bytes`, `storage.recovery_timeout_seconds`, `storage.migration_max_captures`, and the partition lookahead/range settings bound operational work. These are manual commands; no backup timer or retention deletion is installed. Tested local scratch restoration does not establish an off-site recovery policy or the full production RPO/RTO targets.

## Scheduled collection

Inspect the effective collection plan before activation:

```powershell
python tools/dev.py app schedule plan
python tools/dev.py app collect --once
python tools/dev.py app report --last-run
```

The dry-run prints cohort, sources, retries, rolling-day ceilings and conservative serial duration. An infeasible plan exits nonzero and names its settings. The current implementation admits one concurrent collector; extra worker processes cannot increase throughput. HTTP waits and spacing are included in the capacity estimate. For larger cohorts, adjust cadence or timeout through configuration and inspect the plan again.

After you have personally watched a successful full-cohort manual run end to end, acknowledge that run and enable its exact plan:

```powershell
python tools/dev.py app schedule acknowledge --watched
python tools/dev.py app schedule enable
python tools/dev.py app schedule run --max-cycles 1
python tools/dev.py app schedule status
python tools/dev.py app schedule disable
```

`acknowledge` uses the latest matching successful manual run; `--run-id` selects a specific run. Tests and scheduled runs do not establish an operator's acknowledgment. `enable` changes admission state only. `run` processes current occurrences, defaults to one cycle, and is bounded by `scheduler.max_run_seconds`; Ctrl+C cancels it. Restarting the website starts no worker. Changing the effective collection plan requires another successful manual run and acknowledgment. The CLI rechecks configuration during a worker run and stops when the plan changes.

Jobs have durable occurrence identity, leases and fencing. Every retry and uncertain dispatch stays charged in the same ledger as manual runs and probes. Expired current-player occurrences are recorded as missed; they cannot be backfilled. Schedule status reports freshness over enabled app-time, including apps with no observations and nonmaterialized slots during downtime. The enrolled-cadence expectation in history remains separate from this actual schedule denominator.

## Steam discovery and charts

```powershell
python tools/dev.py app charts collect --once
python tools/dev.py app catalog search "Portal"
python tools/dev.py app catalog search "Portal" --page 2
python tools/dev.py app catalog plan
python tools/dev.py app catalog status
python tools/dev.py app catalog sync --once --dry-run --max-pages 1
python tools/dev.py app catalog sync --once --max-pages 20
```

Public charts and Store search require no key. The header Search form opens `/search` with stored catalog matches; that page’s explicit **Search Steam** button imports one US / English game-search page (up to 50 results). **Refresh Steam charts** captures both global charts. These same-origin POST forms share the durable request ledger, quotas, cooldowns and collection lock with CLI commands. GET pages and APIs never call Steam. Each discovery request is attempted once; a failed run can be retried explicitly after any cooldown.

For the documented full game catalog, set `sources.catalog_api_key` to your Steam Web API key in the private local configuration file. Keep the key out of chat, source control and command arguments. It is sent only in the `x-webapi-key` header; settings inspection redacts it and captures do not retain it. Restart the web service after changing configuration. `catalog plan` and sync's `--dry-run` report the next bounded operation without collection or database writes. Page size, per-run page limit, included app types and refresh intervals are validated `catalog` settings; `config describe --schema` lists their bounds.

Each page, source policy and scan checkpoint commit atomically. A bounded unfinished scan reports `partial` and exits nonzero; resume it with another `catalog sync --once`. Failed pages leave the checkpoint and completed watermark unchanged. After completion, ordinary sync waits for `catalog.sync_interval_seconds`, then starts an incremental scan with `catalog.overlap_seconds` around the completed scan-start watermark. `catalog.full_scan_interval_seconds` forces periodic full reconciliation. `--restart` explicitly starts a new full scan while retaining prior discoveries; a changed request policy during a partial scan requires this explicit restart. Missing or reappearing apps never delete prior identities. Discovery does not enroll apps for player tracking. The old catalog parser remains available to replay earlier captures.

Top sellers are current global **revenue ranks**, not copies sold or sales amounts. Steam weights trailing 24-hour revenue, especially the latest three hours. Most-played charts retain concurrent players and Steam Peak Today, separate from local sampled peaks. Both show app entries from Steam's top 100 and preserve original ranks when packages or hardware are excluded. Trending requires two distinct-time most-played captures and ranks positive absolute growth among apps appearing in both. It is not a Steam-wide growth estimate. Latest errors do not replace successful snapshots.

`/api/v1/apps?scope=catalog&q=Portal&page=1&page_size=25` provides bounded stored catalog search with typed player availability and separate catalog provenance. Page size is limited by `web.max_page_size` (default 100; configuration bounds 1–500). `/api/v1/catalog` retains the discovery response for existing clients. `/api/v1/dashboard` exposes chart timestamps, source links, source age, latest independent attempt outcomes, trend scope and scan completion. The default `/api/v1/apps` list retains enrolled player tracking. A known untracked app returns an explicit `not_tracked` summary with null player/tracking fields; an unknown ID remains 404. Discovery does not enroll games.

Sources: [Steam catalog API](https://partner.steamgames.com/doc/webapi/IStoreService), [API authentication](https://partner.steamgames.com/doc/webapi_overview/auth), [top sellers definition](https://partner.steamgames.com/doc/store/top_sellers), [most played](https://store.steampowered.com/charts/mostplayed), [global top sellers](https://store.steampowered.com/charts/topselling/global). Public HTML adapters may require updates when Steam changes its page contract; malformed or empty charts fail visibly.

## Configuration

### Adding your Steam API key

[`config/sources.example.json`](config/sources.example.json) contains a blank API-key setting. It is a configuration fragment, not a complete configuration file.

1. Run the quickstart above to generate `data/instances/local/config/local.json` and its database credentials.
2. In that generated file, replace `sources.catalog_api_key` with your own key as a quoted JSON string (for example, `"catalog_api_key": "YOUR_KEY"`). Preserve all other settings, especially the database URL; do not replace the file with the example.
3. Run `python tools/dev.py start` to reload configuration, then `python tools/dev.py app catalog sync --once --max-pages 20`. Repeat the sync until `/api/v1/dashboard` reports `catalog_sync.complete: true`.

Keep the committed example blank. Put your real key only in the generated local file, which is excluded by the `data/` Git ignore rule. Public charts, Store search and game-detail APIs work without a key; full catalog synchronization requires one.


The generated file is `data/instances/local/config/local.json`. This file contains a local database password and is excluded from Git and exports. Inspect safe effective values or the canonical schema:

```powershell
python tools/dev.py app config describe
python tools/dev.py app config describe --schema
```

`src/game_census/config.py` owns names, defaults and bounds. Edit the generated configuration to change operator values; no rebuild is required. Run `start` to apply web settings and enroll any new `tracking.app_ids`. That list determines future manual collection targets. Previously enrolled games and their retained history remain visible even when removed from the collection list.

For Docker, keep `web.bind` at `0.0.0.0` inside the container; Compose publishes it only on `127.0.0.1` on your computer. Choose `web.port` in configuration, or use `--port` on the first quickstart. An existing profile is preserved; rerunning quickstart with a different `--app-id` or `--port` does not rewrite it. The generated database URL describes the managed local database; changing its password does not automatically change an existing PostgreSQL role.

`--instance NAME` creates an isolated configuration directory, Compose project and database volume. For example, `python tools/dev.py --instance demo quickstart --once --app-id 570 --port 8001`. Choose a free port per simultaneously running instance. Configuration errors name the offending setting; unknown settings and unsafe values fail before collection.

## Read interfaces

Database initialization applies `006_discovery.sql` after P2 storage and rollups. This supersedes the original main branch's `003_discovery.sql` without deleting discovery rows. Global discovery/profile captures keep null tracked-app IDs, and their references follow the partition identity registry. A populated legacy database still requires the shipped backup, verified scratch restore and explicit storage migration commands; `start` alone does not perform that cutover.

| Interface | Purpose |
|---|---|
| `/` | Most-played and top-selling charts, player growth and tracked games |
| `/search?q=Portal&page=1` | Paginated stored catalog search and explicit public Store search |
| `/apps/570` | Compact profile for any known game; Steam details, artwork, reviews, prices and player observations |
| `/api/v1/apps/570/details` | Saved details with source timestamps, capture IDs and local price history |
| `/methodology` | Measurement limits, gaps, freshness and averaging definitions |
| `/status` | Observation state and collection outcome |
| `/api/v1/apps` | Enrolled cohort; optional `q` filter |
| `/api/v1/apps?scope=catalog&q=Portal&page=1&page_size=25` | Paginated known-app search; catalog time is separate from player observation time |
| `/rankings` and `/api/v1/rankings` | Fresh configured-cohort ranking, with stale/missing/unsupported exclusions and deterministic ties |
| `/compare?app_ids=570,620&hours=24` and `/api/v1/compare` | Common UTC window, shared chart scales, per-game availability, coverage and data tables |
| `/api/v1/apps/570` and `/api/v1/apps/570/players` | Last valid count and separate last-attempt state |
| `/api/v1/apps/570/history?hours=24` | Raw half-open UTC history, coverage, growth and gaps; add `resolution=auto` for bounded peak-preserving buckets |
| `/api/v1/status` | Safe stored-data status |
| `/health/live` and `/health/ready` | Process and database readiness |
| `/openapi.json` | Generated API contract |

Freshness describes the time the page was read. An open page announces changed stored data or freshness without replacing your chart position or keyboard focus. Polling permits one request at a time, cancels on hidden/left pages, and uses `web.refresh_seconds` as its request deadline. Successful zero, no observations, stale data and failed source attempts are separate states. Raw windows exceeding configured point limits are rejected. The chart uses explicit auto resolution to retain original first/last/minimum/maximum samples and gap boundaries. Full-window metrics combine exact persisted UTC buckets with raw partial edges. Source reads, including the preceding growth window, are bounded by `web.max_history_samples`; requested buckets are bounded by `cache.rebuild_max_buckets`. Cold reads build missing derived buckets; warm reads reuse them. Changes to observations or cadence invalidate affected caches, and metric/parser policy changes select a separate cache identity. Integral and covered duration remain separate; bucket averages are never averaged together. Growth requires `metrics.min_coverage_ratio` coverage in both windows; a zero baseline has no percentage growth.

History and comparison APIs accept `hours` or an explicit pair of ISO 8601 `from`/`to` timestamps with UTC offsets. They reject mixed, reversed, future or excessive ranges. Comparisons accept comma-separated or repeated `app_ids`, limited by `web.max_compare_apps`; `web.max_compare_points` bounds the combined result. Unsupported and known untracked games remain visible, while unknown IDs fail the whole request. Local rankings include only fresh observations in the configured cohort; equal counts use ascending app ID. These rankings differ from Steam's global chart snapshots.

## Development and evidence

```powershell
python tools/dev.py test
python tools/dev.py test --capacity tests/test_capacity.py
uv sync --frozen
uv run --frozen playwright install chromium
uv run --frozen python tools/browser_check.py
python tools/sync_toolchain.py --check
python tools/repro_build.py
```

The first command runs unit, HTTP/template and PostgreSQL integration tests in the container. Integration fixtures create uniquely named scratch schemas, retain them for inspection, and make no Steam requests. Browser checks exercise a running local instance and save screenshots under `work/browser-check`. Install the uv version in `build/toolchain.lock.json` for host development commands. The lockfile pins transitive dependencies and hashes. `tools/sync_toolchain.py` regenerates the Python selector and pip requirements from their owners; `--check` detects drift. `tools/repro_build.py` compares two independently built wheel files, including schema, templates and assets. Docker provenance metadata is not the canonical artifact being compared.

The optional capacity command creates a new scratch schema with 25 synthetic apps, 90 days and 648,000 observations, without Steam requests. It reports measured query time and storage rather than certifying production performance. Scratch schemas share database resources and advisory locks, so run capacity tests sequentially with browser checks or use a separate instance. Recovery tests also create separate read-only scratch databases and retain them for inspection.

See the [initial completion walkthrough](docs/build-tasks/initial-release/walkthrough.md), [reliable collection packet](docs/build-tasks/p2-reliable-collection/implementation_plan.md) and [P2 storage packet](docs/build-tasks/p2-storage/implementation_plan.md) for commands and unedited output, acceptance boundaries, file purposes and known limitations. Representative production-size recovery, concurrent public-load performance, CI/release hardening and public hosting remain future gates. The local image currently includes the test toolchain and uses one database role; it is a development deployment.

## Project documents and license

The [PRD](docs/PRD-game-census.md) owns product requirements. The [PRS](docs/PRS-game-census.md) owns technical contracts. The [implementation plan](docs/build-tasks/initial-release/implementation_plan.md) owns delivery sequencing and file/command integration. The [roadmap checklist](docs/build-tasks/initial-release/task_checklist.md) owns phase status. The [P2 storage checklist](docs/build-tasks/p2-storage/task_checklist.md) and [execution brief](docs/build-tasks/p2-storage/agent_prompt.md) own the current increment. Open decisions remain in the [backlog](docs/BACKLOG.md).

`python tools/export_plan.py --output-dir outputs/snapshot` creates rebuildable documentation and a source repository ZIP, excluding Git metadata, credentials, installed dependencies and database state. Use a new directory when the source changes. `tools/probe_sources.py` delegates to `collect --once` with the supplied configuration and supported source adapters. Run it through the pinned application environment; it shares the durable request ledger and redirect/host policy. Original planning captures remain immutable historical evidence. The PRD's full 24-hour live canary requires actual elapsed collection evidence before its freshness target can be claimed.

The canonical product name is **Game Census**; `game-census` is its filesystem/package slug. Steam's external `appid` maps to internal `app_id`. The application is independent of Valve and SteamDB. Code uses [Apache-2.0](LICENSE), the default proposed in the original plan. Steam data, trademarks and third-party dependencies have their own applicable rights. The source repository is [briandgibby/game-census](https://github.com/briandgibby/game-census). No hosted application deployment has been published.


## Game profiles and detail refresh

Every known game has the same profile. Opening it reads stored data, including on a cold page with no completed detail attempt. **Refresh details** explicitly sends a same-origin `POST /apps/{app_id}/refresh` and makes at most four admitted requests: Store `api/appdetails` (US, English), [review summary](https://partner.steamgames.com/doc/store/getreviews) (all languages and purchase types, no review text), [community announcements](https://partner.steamgames.com/doc/webapi/ISteamNews) (latest five, bounded excerpts), and [current players](https://partner.steamgames.com/doc/webapi/ISteamUserStats#GetNumberOfCurrentPlayers). These share the durable quotas, cooldowns and collection lock. Legacy `automatic=true` submissions are rejected. Opening a profile does not enroll the app or start a scheduler. Unknown app IDs remain 404. Partial refreshes preserve successful captures and show source errors.

Fields appear only when supplied by these endpoints: developer/publisher, app type, platforms and release-date text, descriptions, genres/features, languages and requirements, controller/DRM information, website, screenshots, price and purchase packages, DLC/demo/base-game relationships, achievement count/highlights, review positive/negative totals, and announcements. Expandable sections keep long content compact. Review percentage is the positive fraction, not SteamDB's rating. Local price history begins with acquired captures (latest 100); collection history shows the latest 20 captures. Recorded peaks cover acquired samples, not Steam all-time records. Existing chart Peak Today remains separately named and timestamped. Franchise, changenumbers, depot manifests, launch configuration, cloud paths, Twitch and follower history are omitted.

Expanded APIs use independent versioned sources, preserving the original name-only parser. An identity envelope retains the exact UTF-8 response body and requested app ID; checksums and replay verify snapshots. Source descriptions become escaped plain text. Profile images use API-returned URLs on fixed Steam media hosts. Dashboard artwork uses already-retained chart HTML without changing canonical chart projections. Images load directly under a narrow CSP; there is no image proxy or bulk per-game crawl.
