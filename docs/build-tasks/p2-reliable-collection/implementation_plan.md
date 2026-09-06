# Game Census: reliable collection implementation plan

## Goal, scope and current evidence

Implement the next P2 increment: a durable, explicitly activated scheduler for the current 1–25-app cohort, shared request accounting across all collection interfaces, and coverage-aware long-window history. The user authorized implementation after initial source publication. No unattended process, public hosting, new source adapter, dependency update or remote publication is part of this increment.

Canonical requirements: [PRD](../../PRD-game-census.md), [PRS](../../PRS-game-census.md). Execution state: [checklist](task_checklist.md). Handoff: [brief](agent_prompt.md). Git owns branch/commit state. The starting commit is `143ffc92688345c36d50269c3aa40e08d08f9b89`; the fetched preflight reported a clean matching `main`.

Verified starting facts: `collector.collect_once` serializes manual runs; `Database.reserve_attempt` charges rolling-day attempts and host cooldowns; `Database.record_capture` atomically stores captures, projections and outcomes. `metrics.calculate` computes a capped integral with gaps. `Database.history` rejects excess points. `cli.parser` exposes no scheduler. Existing migrations are `001_initial.sql` and `002_source_cooldown.sql`. The historical `tools/probe_sources.py` has a separate network path. `config.Settings` accepts old profiles without scheduler settings. Versions remain owned by `uv.lock` and `build/toolchain.lock.json`.

## Outcomes, constraints and acceptance criteria

| Criterion | Required result | Requirement | Verification |
|---|---|---|---|
| A1 | Dry-run has no writes or upstream calls; shows effective hash, cohort, sources, rolling-day worst attempts, serial duration and feasibility rejection | FR-04, NFR-01, NFR-03 | Plan unit tests, CLI dry-run |
| A2 | Enable requires a successful full-plan manual run and explicit watched acknowledgment; any collection-plan change invalidates it; enable state and secrets are excluded from the hash | FR-04, FR-10 | Scheduler DB and CLI tests |
| A3 | Durable occurrences, expiring leases and fencing admit at most one sample per job; uncertain requests stay charged; missed slots are never fetched as historical counts | FR-04, FR-10, NFR-02 | Restart, concurrent-worker, expiry and uncertain-dispatch tests |
| A4 | Manual, scheduled and supported probe collection share host policy/quota; every retry consumes an attempt; cancellation/partial failure is reportable and nonzero | FR-04, FR-10, NFR-01 | Collection and scheduler integration tests |
| A5 | Raw API remains compatible; explicit auto resolution preserves original extrema and returns UTC rollups, full-window integral, coverage and qualified growth | FR-03, NFR-02 | Independent golden history cases and API/template tests |
| A6 | Existing state upgrades additively; empty initialization works; canonical replay matches; default restart leaves scheduler inactive | FR-01, FR-10, NFR-02, NFR-04 | Container fresh/upgrade tests and replay |
| A7 | One watched manual three-app run uses direct Steam sources, emits bounds first and records outcomes; no real schedule is enabled by test evidence | FR-04, NFR-01 | Bounded isolated canary and raw output |

Single concurrent collector is an explicit capacity policy for this bounded increment. Multiple worker processes may contend safely, but do not increase admitted throughput. Serial worst-case network duration, host spacing, retry backoff and reserves must fit cadence and rolling-day ceilings. Defaults remain configuration-owned with code-owned bounds.

Compatibility retains current-player sourcing FR-02, read interfaces FR-06 and name-only FR-07. Catalog FR-05, review FR-08 and achievement/news FR-09 expansion remain outside this increment.

P2 storage partition conversion and persisted rollup caching remain separate follow-up tasks. Existing UUID primary-key relationships make partition conversion a data migration requiring a tested restore and cutover; it is unnecessary to introduce that change into four retained live captures. This increment computes deterministic rollups from bounded retained samples; no cache becomes another fact owner. Do not mark the full P2 roadmap complete based on this increment.

## File changes and integration

| Action / file | Responsibility and integration |
|---|---|
| MODIFY `src/game_census/config.py` | Add bounded Scheduler settings, metric growth coverage threshold and source-read limit; old JSON profiles inherit safe defaults |
| NEW `src/game_census/migrations/003_scheduler.sql` | Add scheduler plan/run acknowledgment, durable job/lease/attempt associations and operational records without replacing captures |
| NEW `src/game_census/scheduler.py` | `plan`, manual-run attestation, `acknowledge`, `enable`, `disable`, `status`, bounded `run`; shared database admission and fenced outcomes |
| MODIFY `src/game_census/db.py` | Reuse admission/capture transactions for manual and scheduler callers; expose schedule state; extend `history` with explicit raw/auto resolution |
| MODIFY `src/game_census/collector.py` | Bind manual evidence to effective plan, preserve retries and safe reports, share request boundaries |
| MODIFY `src/game_census/sources/http.py` | Preserve fixed official Steam HTTPS hosts, redirect refusal and bounded requests; enforce shared lifecycle context where required |
| MODIFY `src/game_census/sources/players.py`, `store.py` | Expose canonical `parameters(app_id)` to both adapter fetch and plan identity; prevent duplicated request policy from drifting |
| MODIFY `tools/probe_sources.py` | Route supported probes through the shipped collector and its ledger; retain old planning evidence unchanged |
| MODIFY `Dockerfile` | Include the accounted probe command in the tested container artifact; container regression exposed its absence |
| NEW `src/game_census/schedule_coverage.py`, `tests/test_schedule_coverage.py` | Derive scheduled app-time and expected slots from exact activation events; include downtime and apps without observations in the freshness denominator |
| MODIFY `src/game_census/metrics.py` | Exact cadence slots, integral/extrema timestamps, bounded peak-preserving UTC rollups, adjacent-window growth and policy identity |
| MODIFY `src/game_census/cli.py` | Register schedule plan/acknowledge/enable/disable/status/run and history resolution; nonzero failure exit and bounds announcement |
| MODIFY `src/game_census/contracts.py` | Carry new metrics/coverage/growth/rollup and public scheduling states through API serialization |
| MODIFY `src/game_census/web.py` | Explicit API resolution and auto chart reads; read-only scheduling status and metric display data |
| MODIFY `src/game_census/templates/_chart.html`, `methodology.html`, `status.html` | Disclose growth/coverage and reduction, render retained extrema, accurate scheduler state; preserve keyboard and data-table access |
| NEW `tests/test_scheduler.py`, `tests/test_p2_collection.py`, `tests/test_p2_history.py`, `tests/test_p2_interfaces.py` | Plan, DB concurrency/recovery/accounting, metric golden cases, CLI/API/UI boundaries |
| MODIFY `tests/test_api.py`, `test_config_cli.py`, `test_recovery.py`, `test_sources.py` | Adapt existing fixtures to explicit resolution, typed settings, shared transaction signatures and truthful manual/lock diagnostics; original history tests remain unchanged |
| MODIFY `README.md` and canonical roadmap navigation | Shipped commands and current capability limits; keep prior walkthrough immutable |
| NEW packet files and `walkthrough.md` | This increment's plan, checklist, handoff and actual acceptance evidence |

Collection trace: CLI/wrapper → settings → plan/activation → durable job or manual run → shared quota/host admission → fixed Steam adapter → atomic capture/projection/outcome → reports → read API/status page. History trace: CLI/API/page → bounded retained sample and tracking reads → exact metric/growth/rollup calculations → response models → chart/table and methodology.

## Verification and safe migration

Working directory for every command below is the repository root. The wrapper, pytest and lock commands were inspected in `tools/dev.py` and `pyproject.toml`. New CLI forms below are to implement, not claims of existing commands.

```powershell
.venv/Scripts/python.exe -B -m pytest -q -p no:cacheprovider tests/test_history.py tests/test_p2_history.py tests/test_config_cli.py tests/test_p2_interfaces.py
python tools/dev.py build
python tools/dev.py start
python tools/dev.py test
python tools/dev.py app schedule plan
python tools/dev.py app schedule status
python tools/dev.py app aggregate rebuild
python tools/export_plan.py --check
python tools/repro_build.py
```

Run targeted tests before the container suite. Live tests use an isolated instance and three configured app IDs, one manual run only, after printing scope. No network load test is authorized. For synthetic tests use uniquely named scratch schemas; retain them for inspection. Copy every pre-change tracked file to scratch, restore into a second scratch tree and verify SHA256 before edits. Additive migrations must not delete/rewrite old captures; count/hash replay before and after upgrade is the preservation check. Preserve failing bug reproductions and unedited post-change output.

Risk gates triggered: untrusted configuration/HTTP inputs, external Steam contracts, background/concurrent lifecycle, persistence, API/config compatibility and interactive UI. No authenticated Steam sources or personal player data are added. PostgreSQL transactions and advisory locking follow the [PostgreSQL 18 locking contract](https://www.postgresql.org/docs/18/explicit-locking.html); current counts retain the [official Steam method](https://partner.steamgames.com/doc/webapi/ISteamUserStats#GetNumberOfCurrentPlayers). No model connects runtime components.
