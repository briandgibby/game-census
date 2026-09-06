# Game Census: P2 storage implementation plan

## Goal and evidence

Complete P2 storage after the [reliable collection increment](../p2-reliable-collection/walkthrough.md): recoverable monthly partitions, exact rebuildable persisted rollups, and bounded synthetic validation. Prepare the PRD's 24-hour live canary; activate only after the operator has watched and acknowledged the exact current collection plan. The local PC is the selected host; plugged-in sleep and hibernate timers now read zero (Never). No new Steam adapter, authenticated source, public deployment or remote publication is in scope.

The current working tree contains the prior increment's uncommitted changes. Preserve them. Before edits, 203 tracked and untracked task files were copied to backup and restored into a separate scratch tree; all SHA256 values matched. The manifest is recorded in the walkthrough evidence. The initial Git commit remains `143ffc92688345c36d50269c3aa40e08d08f9b89`; Git is the owner of current branch and publication state.

Pre-change baseline: capture UUID and attempt uniqueness had global foreign-key consumers; `Database.initialize` reran idempotent SQL; `Database.history` read bounded raw samples and computed rollups; `storage.backup_path` was configured but unused. Docker used pinned PostgreSQL 18 and Psycopg. The live local and three-app instances contained four and six captures respectively, with schedules disabled.

## Acceptance criteria

| ID | Outcome | Requirements | Verification |
|---|---|---|---|
| S1 | Shipped backup command creates an immutable complete application snapshot; shipped restore command creates a distinct scratch destination, verifies contents/checksums and replay, and reports failures safely | FR-01, FR-10, NFR-01, NFR-02 | Recovery tests; actual local backup/restore output |
| S2 | Empty initialization and explicit legacy migration produce monthly capture/sample partitions without loss, broken references or weakened global uniqueness; migration requires a fresh tested restore | FR-01, FR-02, FR-04, NFR-02 | Empty/upgrade, duplicate, late arrival, contention and restore equivalence tests |
| S3 | Shipped partition plan/maintenance commands create required partitions idempotently and retain canonical history indefinitely | FR-03, FR-10, NFR-02 | Dry-run versus actual catalog, repeated maintenance, read/write boundary tests |
| S4 | Persisted UTC rollups are rebuildable from captures/projections, consumed by history, invalidate on changed inputs/policy, and preserve exact integrals/extrema/gaps/carry at range boundaries | FR-03, FR-06, NFR-02, NFR-03 | Golden raw/cache equivalence; late arrivals, cadence and policy changes; regenerate proof |
| S5 | Existing suite and larger synthetic workloads pass within measured bounds; installed live state remains readable and source-preserving | FR-01, FR-02, FR-03, FR-04, FR-06, FR-10, NFR-01–NFR-04 | Container suite, synthetic capacity evidence, local upgrade and browser checks, reproducible wheels |
| S6 | Exact bounded three-app plan is shown and manually exercised; operator acknowledgment precedes schedule enablement; actual 24-hour freshness is measured when elapsed | FR-04, FR-10, NFR-03 | Plan and manual output, user attestation, worker/coverage report; pending until actual observation |

Compatibility covers name-only FR-07. Catalog FR-05, full Store FR-07, reviews FR-08 and achievements/news FR-09 remain later phases. Representative production-size RPO/RTO and full public-release performance gates remain P5; report only measured synthetic capacity.

## File inventory and component integration

| File | Responsibility and reason |
|---|---|
| NEW `src/game_census/recovery.py`, `tests/test_storage_recovery.py` | Complete snapshot/restore interface, safe scratch identity, input integrity and replay verification; enables a tested partition cutover |
| NEW `src/game_census/storage.py`, `migrations/004_storage.sql`, `tests/test_storage.py` | Partition identity/maintenance/migration, durable integrity, empty bootstrap and legacy safety |
| NEW `src/game_census/cache.py`, `migrations/005_rollups.sql`, `tests/test_rollup_cache.py` | Versioned derived rollups, rebuild/invalidation and exact history integration |
| MODIFY `src/game_census/db.py`, `projections.py` | Connect initialization, accepted observations, replay and history to storage/cache interfaces while preserving transactions |
| MODIFY `src/game_census/metrics.py` | Combine adjacent exact metric pieces and apply the canonical growth calculation to composed windows |
| MODIFY `src/game_census/config.py`, `cli.py` | Validated bounds and direct commands for backup/restore, partition planning/maintenance/migration and rollup rebuilding; print scope before writes |
| MODIFY `Dockerfile`; NEW `build/stage_postgres_client.sh`, `build/postgres_client.sh` | Package PostgreSQL backup/restore clients and their private loader/libraries from the pinned PostgreSQL image |
| MODIFY `tools/dev.py` | Pass both pinned base-image build arguments and expose the bounded synthetic capacity workload through `test --capacity` |
| MODIFY `tools/browser_check.py` | Require the mobile response to be HTTP 200 with its heading and tracked rows, preventing the reproduced false-positive success on an HTTP 503 page |
| NEW `tests/test_storage_interfaces.py`, `tests/test_capacity.py` | CLI/config/error paths and bounded synthetic workloads with no Steam traffic |
| MODIFY `README.md`, `docs/PRS-game-census.md`, roadmap navigation | Describe actual shipped behavior, operational procedures and limits; preserve earlier raw evidence |
| NEW this packet, `walkthrough.md`, `evidence/*` | Track implementation and provide exact commands, unedited output, changed-file purposes and requirement coverage |

Runtime flow: CLI → validated settings → immutable backup → new scratch restore/verification → explicit storage migration → partition maintenance → atomic collection → canonical capture → player projection → versioned rollup rebuild → bounded history/API/UI. No language model connects components. Keep default start free of collection and unattended maintenance.

## Safety and verification

Perform synthetic work only in unique scratch schemas and print cohort/time/row bounds. Keep live schedules disabled during migration. State the purpose of any legacy table before removal, and prove restore using the actual snapshot while excluding writers through the cutover. Never discard canonical payloads, IDs, attempts, schedule occurrences or tracking evidence. Derived caches may be replaced only after their exact regeneration path has been exercised. Reject untrusted paths/configuration and never print database credentials.

Implementation agents have exclusive ownership: recovery module/tests; storage module/migration/initialization/projection sections; cache module/migration/history section. Root owns CLI/config/docs and final integration. Coordinate shared-file edits by function, not broad replacement. Each active agent has one checklist item in progress.

Run targeted fixture/DB tests first, then `python tools/dev.py build` and `python tools/dev.py test`. Preserve actual failing reproductions and repeat the exact command after each bug fix. Record a real backup/restore/upgrade, `python tools/repro_build.py`, and browser smoke checks when installed. New CLI names in this plan describe work to implement; the final walkthrough owns the actual command evidence. Follow [PostgreSQL 18 partitioning](https://www.postgresql.org/docs/18/ddl-partitioning.html) and [backup guidance](https://www.postgresql.org/docs/18/backup.html).
