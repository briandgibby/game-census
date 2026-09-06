# P2 integration implementation plan

## Goal

Implement [INT-01–INT-06](PRD.md) in [checklist](task_checklist.md) order. [Execution brief](agent_prompt.md). Working directory is the integration checkout on `develop`.

## File inventory and integration trace

| Action | Files | Purpose and verification |
|---|---|---|
| MODIFY | `src/game_census/collector.py`, `sources/http.py`, `cli.py`, `config.py`, `contracts.py` | Join manual/scheduled and discovery entry points, credentials, limits and registrations; source/scheduler/CLI tests |
| MODIFY | `src/game_census/db.py`, `projections.py`, `storage.py`, `recovery.py` | Share capture transactions, partition identity and canonical projection verification across all sources; mixed-source upgrade and recovery tests |
| MOVE/MODIFY | `src/game_census/migrations/003_discovery.sql` → `006_discovery.sql` | Preserve discovery capability after P2 migration sequence and select the appropriate capture identity reference for each layout |
| NEW from P2 | `scheduler.py`, `schedule_coverage.py`, `cache.py`, migrations `003_scheduler.sql`, `004_storage.sql`, `005_rollups.sql` under `src/game_census/` | Restore bounded scheduling, coverage, partition and rollup contracts; original P2 suites |
| MODIFY | `metrics.py`, `sources/players.py`, `sources/store.py` under `src/game_census/` | Preserve P2 bounds and exact peak/coverage calculations; history/source suites |
| MODIFY | `src/game_census/web.py`, templates `_chart.html`, `methodology.html`, `status.html`; `tools/browser_check.py`, new `tools/browser_fixture_check.py` | Preserve profile/catalog routes while exposing scheduled status and auto-resolution history; HTTP/template/browser checks |
| MODIFY/NEW | `Dockerfile`, `build/postgres_client.sh`, `build/stage_postgres_client.sh`, `tools/dev.py`, `tools/probe_sources.py` | Pinned backup clients, isolated instance test/build interface and admitted probes |
| NEW/MODIFY | `tests/test_p2_integration.py`, restored P2 tests and existing touched API/config/recovery/source tests | Reproduce compatibility gaps before fixing them; verify fresh, main and P2 upgrades with synthetic input |
| MODIFY/NEW | `README.md`, `docs/PRS-game-census.md`, initial-release phase records, restored P2 packets, this packet and evidence | Describe final shipped contracts and retain historical evidence; exact final inventory in walkthrough |

Shipped migrations remain idempotent. Preserve existing canonical rows and scheduled evidence. Rename the discovery migration because its purpose is global capture and catalog persistence and it must follow storage initialization; preserve that purpose in version 6. Retarget foreign keys only during the existing restore-protected cutover. Do not migrate either live instance.

## Verification commands

Acceptance mapping: INT-01 uses archive restore comparisons; INT-02 uses both upgrade layouts and fresh bootstrap; INT-03 uses source/deadline/quota/scheduler cases; INT-04 uses mixed-source replay, corruption rejection and native recovery; INT-05 uses the full suite, browser journey and reproducible build; INT-06 uses the file-purpose inventory, packet validation and develop commit evidence.

Use `python tools/dev.py --instance p2-integration build`, then generated configuration and `start` on a separate port, with no collection. Run `python tools/dev.py --instance p2-integration test tests/test_p2_integration.py` before and after compatibility fixes, then the complete `python tools/dev.py --instance p2-integration test`. These commands are supplied by `tools/dev.py`. Use `python tools/repro_build.py` and `python tools/sync_toolchain.py --check` for reproducibility. Record exact commands/output and all environment constraints. Browser verification must use isolated synthetic data, identified as test data; no live profile collection.

Read-only Git fetch confirmed main unchanged. Commit only the verified integration on `develop`; user authorized develop commits, not remote publication. No additional dependency versions are planned.

The browser fixture command is `.venv/Scripts/python.exe tools/browser_fixture_check.py`; it serves one synthetic in-memory game, blocks collection and fulfills allowlisted artwork locally. The browser check retains HTTP-200, heading, table and keyboard assertions. `tests/fixtures/main_discovery.sql` is a historical test contract derived from `git show f6c4aaa:src/game_census/migrations/003_discovery.sql`; it allows the main-layout upgrade to be tested without borrowing the new migration.
