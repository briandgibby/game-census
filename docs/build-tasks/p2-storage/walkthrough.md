# Game Census: P2 storage walkthrough

Dated P2 record, preserved from the pre-main-update stash. See [current integration handoff](../p2-integration/walkthrough.md) for the combined codebase. Run and monitor states below describe the recorded event, not current live state.

## Outcome and scope

The storage increment is implemented and installed on the original local instance and the separate three-game canary instance. Monthly partitions, verified backup/restore, protected legacy cutover and persisted exact history rollups are exercised with real retained Steam captures. The final application suite reports 322 passed. P2 remains open: the first 24-hour canary was interrupted and did not meet the elapsed-run requirement. The operator-confirmed activation and subsequent interruption are recorded below. Both instance schedules are now disabled; the hourly monitoring task is paused. No automatic restart or extension was performed.

The [PRD](../../PRD-game-census.md) owns product requirements and the freshness target; the [PRS](../../PRS-game-census.md) owns technical contracts. This record reports evidence for the [storage plan](implementation_plan.md) and [checklist](task_checklist.md), following the [reliable collection increment](../p2-reliable-collection/walkthrough.md). Steam is the only external game-data source. Synthetic tests do not supply displayed live counts.

## Verification

Every `.txt` linked below contains unedited command output. The adjacent `.txt.json` records the exact argument array, working directory and exit status; these records distinguish source-mounted intermediate checks from final installed-container checks. Commands run from the repository root unless their metadata says otherwise.

| Check | Command | Actual result and raw output |
|---|---|---|
| Final application suite | `python tools/dev.py test` | [322 passed, 2 existing dependency deprecation warnings, 49.98 seconds](evidence/container-suite-public-final.txt) |
| Extended synthetic workload | `python tools/dev.py test --capacity tests/test_capacity.py` | [1 passed; 25 apps, 90 days, 648,000 observations, zero Steam requests](evidence/capacity-extended-final.txt) |
| Reproducible artifact | `python tools/repro_build.py` | [Two identical wheels](evidence/reproducible-build-public-final.txt); SHA256 `23a785940164d712be799462e8d830477d426733ab487dbeb0659c9da3aef4e8` |
| Pin consistency | `python tools/sync_toolchain.py --check` | [Generated inputs current](evidence/toolchain-check.txt) |
| Original instance browser | `.venv/Scripts/python.exe -B tools/browser_check.py --url http://127.0.0.1:8000 --output-dir work/p2-storage-browser-local-verified` | [Successful desktop/mobile journey](evidence/browser-local-verified.txt) |
| Canary instance browser | `.venv/Scripts/python.exe -B tools/browser_check.py --url http://127.0.0.1:8002 --output-dir work/p2-storage-browser-canary-verified` | [Successful desktop/mobile journey](evidence/browser-canary-verified.txt) |
| Power plan | `powercfg /query SCHEME_CURRENT SUB_SLEEP` | [Active AC sleep and hibernate timers zero](evidence/power-settings.txt); user changed them to Never |
| Local retained state | `python tools/dev.py app report` | [Database OK, 4 captures, 2 player samples, zero uncertain attempts, disabled schedule](evidence/local-status-final.txt) |
| Canary retained state | `python tools/dev.py --instance p2-canary app report` | [Database OK, 9 captures, 6 player samples, zero uncertain attempts, disabled schedule](evidence/canary-status-final.txt) |

The extended workload measured 42.768 seconds to load its fixture, 2.049 seconds for cold history, 0.158 seconds warm median, 0.180 seconds warm p95 (the largest of ten measured requests), and 0.880 seconds for the cohort summary. Table/index storage was 749,608,960 bytes. Checksums, independently specified integrals, extrema, coverage, point bounds and identical cold/warm responses passed. These are local sequential query measurements, not a 20-request/second production benchmark.

Desktop and mobile screenshots were visually inspected. The final pages show the recorded counts, stale labels when appropriate, gaps, methodology and responsive layouts. Keyboard navigation, chart/table access, search, unknown-game handling and absence of external requests or collection during browsing are included in the executable journey.

## Live recovery and migration

Before this increment changed existing files, 203 task files were backed up and restored into a separate scratch tree. A later [read-only verification](evidence/prechange-and-read-incident.txt) checks both copies against manifest SHA256 `bb66a80006dd34e1ebc8508bd544a5b70c5de22b1a97febe84b7c3561c11c832`. Generated credential configuration stayed outside the export. The canary configuration change separately restored its prior bytes before replacement; [configuration evidence](evidence/canary-profile.txt) records hashes and the admitted bounded plan.

| Operation | Original instance evidence | Canary instance evidence |
|---|---|---|
| Build matching native backup clients and application | [Build](evidence/build-public-fix.txt) | [Build](evidence/canary-build-public-fix.txt) |
| Create immutable backup | [Backup](evidence/local-backup.txt) | [Backup](evidence/canary-backup.txt) |
| Restore into a new protected scratch database | [Verified restore](evidence/local-restore-after.txt) | [Verified restore](evidence/canary-restore.txt) |
| Revalidate proof and migrate legacy history | [Cutover](evidence/local-migrate-after.txt) | [Cutover](evidence/canary-migrate.txt) |
| Regenerate and compare frozen migration copies | [Legacy verification](evidence/local-legacy-verify-after.txt) | [Legacy verification](evidence/canary-legacy-verify.txt) |
| Maintain receipt-month partitions | [Maintenance](evidence/local-maintain-after.txt) | [Maintenance](evidence/canary-maintain.txt) |
| Install and start the read service | [Start](evidence/local-start.txt) | [Start](evidence/canary-start.txt) |
| Replay canonical captures and rebuild caches | [4 captures verified](evidence/local-replay-after.txt) | [9 captures verified after the manual run](evidence/canary-replay-after-manual.txt) |

The command sequence is `backup create`, `backup restore --latest-backup`, `storage migrate --proof-id PATH`, `storage legacy-verify`, `storage maintain`, and `aggregate rebuild`, through `python tools/dev.py app` (or the canary instance selector). Returned proof paths are selectable outputs, not source-only facts. Exact commands and proof IDs accompany each raw output.

Cutover compared full rows before and after: all four original captures/two samples and all six pre-existing canary captures/three samples matched their source hashes. It preserved IDs, attempts, tracking and audit relationships. The original instance remains at four captures. Three new official player responses increased the canary to nine captures afterward. No canonical history was deleted. Frozen legacy tables retain a stated migration-snapshot purpose and have a tested regeneration command.

## Reproductions and corrections

Failures are retained with their subsequent checks; an intermediate failure is not final acceptance evidence.

| Reproduced problem | Before | Same reproduction after correction |
|---|---|---|
| PostgreSQL utility statement parameterization during bootstrap | [Failure](evidence/storage-bootstrap-first.txt) | [Pass](evidence/storage-bootstrap-after.txt) |
| Partition creation and cache lock ordering could deadlock | [Failure](evidence/storage-cache-deadlock-before.txt) | [Pass](evidence/storage-cache-deadlock-after.txt) |
| No-op projection replay invalidated warm caches through insert triggers | [Failure](evidence/storage-replay-cache-before.txt) | [Pass](evidence/storage-replay-cache-after.txt) |
| Failed scratch restore protection and backup/schedule lock ordering | [Failures](evidence/recovery-guard-lock-before.txt) | [Passes](evidence/recovery-guard-lock-after.txt) |
| Empty explicit backup selector silently selected latest | [Failures](evidence/recovery-empty-selector-before.txt) | [Passes](evidence/recovery-empty-selector-after.txt) |
| Inconsistent configured partition bounds | [Failure](evidence/config-range-before.txt) | [Pass](evidence/config-range-after.txt) |
| Rebuild scope omitted total replay count and per-app cache bound | [Failure](evidence/aggregate-scope-before.txt) | [Pass](evidence/aggregate-scope-after.txt) |
| Default `public` namespace already existed in a fresh restore destination | [Regression failure](evidence/recovery-public-schema-before.txt) | [Pass](evidence/recovery-public-schema-after.txt) |
| Browser journey accepted an HTTP 503 mobile page | [Injected-response failure](evidence/browser-guard-before.txt) | [Pass](evidence/browser-guard-after.txt) |

The first actual original-instance restore also [failed safely](evidence/local-restore.txt); the following migration guard refused cutover. The fix verifies that the new scratch `public` namespace owns no objects and derives a restore selection omitting only its redundant creation entry. It preserves the archive and every data/constraint entry. The [same original restore command then succeeded](evidence/local-restore-after.txt) before cutover was retried. No existing database was dropped or overwritten to bypass that failure.

The first local browser command [reported success](evidence/browser-local-final.txt), but visual inspection found an error page in its mobile screenshot. The [application log](evidence/prechange-and-read-incident.txt) recorded history read incident `552cd65c9443`. This coincided with the large scratch workload; scratch schemas share database resources and advisory locks. That earlier output is not valid mobile acceptance evidence. The preserved [fault injection script](evidence/browser_guard_reproduction.py) reproduces the checker defect against the actual browser journey. The checker now requires HTTP 200 and real game content; the final browser checks ran after the workload completed.

## File purposes

The following inventory covers files changed since the restored pre-storage baseline; prior P2 changes retain their earlier walkthrough. The [final audit](evidence/export-audit-final.txt) checks this inventory, preserved evidence, whitespace and export exclusions.

| File(s) | What they now do and why they changed |
|---|---|
| `src/game_census/recovery.py` | Immutable application snapshot, validated selection, new scratch restore, integrity/replay verification and proof revalidation before cutover |
| `src/game_census/storage.py` | Bounded monthly maintenance, global identity integration, protected legacy migration and frozen-copy regeneration |
| `src/game_census/migrations/004_storage.sql` | Partitioned capture/sample structures, identity registry, storage state, transition membership and integrity constraints |
| `src/game_census/cache.py` | Exact versioned complete UTC buckets, validation, rebuilding, invalidation and bounded cached history with raw partial edges |
| `src/game_census/migrations/005_rollups.sql` | Persisted derived cache and shared locks/invalidation triggers for source/tracking changes |
| `src/game_census/db.py` | Connect initialization, capture receipt and history reads to storage/cache interfaces |
| `src/game_census/projections.py` | Preserve canonical replay while avoiding invalidation from an already-present projection |
| `src/game_census/metrics.py` | Combine exact bucket state using the canonical metric formula, retaining integrals and gap geometry |
| `src/game_census/config.py` | Validate backup, migration, partition and cache bounds; reject inconsistent cross-setting ranges |
| `src/game_census/cli.py` | Expose backup/restore/storage/rebuild commands and explicit bounded scope before writes |
| `Dockerfile` | Include matching pinned PostgreSQL native clients with private runtime libraries |
| `build/stage_postgres_client.sh` | Stage required native binaries, loader and dependency libraries from the pinned database image |
| `build/postgres_client.sh` | Launch the native client using its private loader/library path |
| `tools/dev.py` | Supply the locked database image during build and expose the bounded capacity workload |
| `tools/browser_check.py` | Reject failed mobile navigation and require actual game content before reporting success |
| `tests/test_storage_recovery.py` | Verify snapshots, scratch guards, safe failure paths, source-matching proofs and public-schema restoration |
| `tests/test_storage.py` | Verify empty initialization, legacy cutover, global deduplication, UTC boundaries and contention handling |
| `tests/test_rollup_cache.py` | Compare exact raw/cache results, mutation invalidation, corruption handling and regeneration |
| `tests/test_storage_interfaces.py` | Validate new settings, selectors, CLI scope and actionable errors |
| `tests/test_capacity.py` | Generate bounded synthetic cohorts and verify exact history, integrity, query time and storage |
| `README.md`, `docs/PRS-game-census.md` | Document shipped commands and actual contracts, preserving the distinction between local evidence and future release targets |
| `docs/build-tasks/initial-release/implementation_plan.md`, `task_checklist.md`, `agent_prompt.md` | Point ongoing work to this packet, update evidenced P2 status and reserve migration 006 for future enrichment |
| `docs/build-tasks/p2-storage/PRD.md`, `implementation_plan.md`, `task_checklist.md`, `agent_prompt.md`, `walkthrough.md`, `evidence/*` | Link canonical requirements, record bounded work and retain raw commands, reproductions, results and remaining acceptance gates |

## Requirement traceability

| Requirement | Evidence boundary |
|---|---|
| FR-01 | Empty bootstrap, validated config and direct storage/recovery CLI: final suite, live restore/migration |
| FR-02 | Real official player captures retained across cutover: manifests and three-request manual run |
| FR-03 | Exact integrals, extrema, gaps and replay: history/cache tests and synthetic analytic fixture |
| FR-04 | Existing scheduling/admission tests remain passing; live watched activation and 24-hour observation are pending |
| FR-05 | Catalog expansion remains out of scope |
| FR-06 | Current history API and read UI remain compatible: API/UI suite and corrected browser journey |
| FR-07 | Name-only Store compatibility preserved in live snapshots; full Store enrichment remains future |
| FR-08 | Reviews remain out of scope |
| FR-09 | Achievements/news remain out of scope |
| FR-10 | Direct backup/restore, maintenance, replay, reports and retained failure evidence exercised |
| NFR-01 | Bounded validated settings, safe diagnostics, fixed Steam origins and export secret audit |
| NFR-02 | Canonical captures, global identities, source-matching restore/cutover, exact derived cache regeneration |
| NFR-03 | Bounded local capacity measured; full 24-hour freshness and production concurrent-load gates remain open |
| NFR-04 | Locked inputs, identical wheel output, changed-file inventory and unedited evidence; no remote publication in this increment |

## Known limitations and next action

The [exact three-game plan](evidence/canary-plan.txt) uses Steam's current-player endpoint only, every five minutes, capped at 1,000 Web API requests per rolling day. Its conservative admission calculation reserves boundary capacity. Store-name polling is disabled for this canary; previously captured official names remain readable.

The [manual run](evidence/canary-manual.txt), ID `af9d079c-5df8-49b9-a06e-bdf115a93951`, made exactly three successful requests at 2026-09-05 05:07:54–56 UTC: Dota 2 469,913; Counter-Strike 2 572,559; Team Fortress 2 47,900. The operator explicitly confirmed watching that run end to end and authorized activation. The resulting acknowledgment records that user attestation. The full elapsed freshness measurement is still required before marking S6 complete.

No production RPO/RTO, off-site backup policy, public-load performance, broader catalog or P3–P5 release completion is claimed. Scratch tests retain their data and can contend with readers on the same database; run large workloads sequentially or on a separate instance. The restored database is protected by its default read-only setting and is not a separately hardened security boundary against an administrator. Local deployment still includes a test toolchain and one database role.

This increment did not commit or push changes. The [final local Git preflight](evidence/git-preflight-final.txt) reports a dirty main branch at `143ffc926883` and refuses to certify remote freshness without a new fetch. Cached tracking counts alone are not remote synchronization evidence.


## Canary activation handoff

The user confirmed: “Yes, I watched the run. I am looking at the page. We can start the canary.” The shipped acknowledgment and enable commands recorded this authorization; [first scheduled cycle output](evidence/canary-first-scheduled-cycle.txt) reports three successful requests, zero failed and zero uncertain. No configuration or application code changed for activation.

Activation time is `2026-09-05T05:34:20.160072Z`; the fixed measurement window ends `2026-09-06T05:34:20.160072Z` (September 6, 1:34 a.m. Eastern). [Activation status](evidence/canary-activation-status.txt) owns the epoch and anchor. The background command is `python -u tools/dev.py --instance p2-canary app schedule run --max-cycles 288`. [Launch metadata](evidence/canary-worker-launch.json) records its PID and log paths. It started within the first five-minute slot, so that completed foreground slot counts toward its 288-slot bound; completed jobs are not sent again. The worker has the configured 86,400-second duration ceiling from its own start. Its normal exit after the final slot, around 23h55 after activation, does not prove 24 hours of freshness or disable the schedule.

At activation, the hourly task `Game Census canary check` was enabled in this conversation to check at minute 35. It was paused after the interruption recorded below. It reads status and worker logs, remains quiet during healthy progress, reports meaningful failure, and disables collection on the first check after the fixed deadline before assessing the result. It must not restart or extend a failed worker, alter this plan, or run database tests during collection. Keep the PC, Docker and the Codex desktop app running for collection and follow-up. [Official scheduled-task guidance](https://learn.chatgpt.com/docs/automations?surface=app) describes the local app requirement.

The authoritative result must calculate the fixed half-open window from retained control events and player observations through `schedule_coverage.calculate`. A later `schedule status` command reports a moving window and must not be mislabeled as this fixed window. Three apps continuously scheduled for 24 hours mean 259,200 tracked app-seconds and 864 expected player occurrences. Include all missed, failed and unmaterialized time. Compare against the canonical PRD's 95% freshness target only after the window has elapsed. Record stop, worker result and exact-window coverage, update S6 with evidence, export a new result and pause the hourly task afterward.

The native worker's hard time limit is roughly 110 seconds later than the fixed-window end because it starts after the foreground rehearsal. Normally its cycle bound ends it earlier; after skipped slots, the follow-up's explicit disable closes admission. This activation record does not claim an exact-to-the-second automatic stop at the fixed-window boundary.


## Canary interruption

The hourly check at 2026-09-05 17:36 UTC found the background worker had ended at `2026-09-05T16:48:06.856752Z` (12:48 p.m. Eastern). The retained run was `partial` with `schedule_interrupted`, after 135 counted cycles and 402 successful background requests. Together with the three-request foreground cycle, 405 scheduled observations were accepted. Every recorded dispatched request in this run succeeded; there were zero failed or uncertain attempts. The last hourly check before the interruption had recorded 399 successes and the original worker identity.

| Evidence | Direct interface and observed result |
|---|---|
| [Detection status](evidence/canary-check-20260905T173632-status.txt) | `python tools/dev.py --instance p2-canary app schedule status`: 405 observed of 435 expected scheduled occurrences at detection, enabled plan unchanged |
| [Complete retained report](evidence/canary-check-20260905T173632-report.txt) | `python tools/dev.py --instance p2-canary app report`: partial background run, finish time, all 402 individual successful outcomes and generic interruption error |
| [Worker check](evidence/canary-check-20260905T173632-worker.txt) | Original PID absent; durable wrapper stderr records exit 1 |
| [Full worker stdout](evidence/canary-interrupted-worker.stdout.txt), [stderr](evidence/canary-interrupted-worker.stderr.txt) | Byte-identical preserved logs, including startup command scope and final run result |
| [Bounded diagnostics](evidence/canary-interrupted-diagnostics.txt) | Docker database/web healthy; database had not restarted; database logs from 16:40–16:55 UTC contain checkpoints and no reported error; worker log hashes and compact run summary |
| [Disable](evidence/canary-interrupted-disable.txt), [disabled status](evidence/canary-interrupted-disabled-status.txt) | Shipped `schedule disable` and `schedule status` confirm collection admission is disabled and no uncertain attempts remain |
| [Handoff and monitor state](evidence/canary-interrupted-handoff.txt) | Prior documents restored into scratch before update; existing hourly task confirmed paused after the incident was reported |

The disabled-state report exposes its actual rolling window ending `2026-09-05T17:38:16.921697Z`. Scheduled app-time accumulated before disable was 130,146.586041 seconds, of which 122,374.323334 seconds was fresh (94.028%). Thirty expected scheduled observations had not occurred when monitoring detected the stopped worker. Those missed occurrences and the time between interruption and disable remain in the report. This is an interrupted-run measurement, not the planned full 24-hour result. The original full target remains 259,200 tracked app-seconds and 864 expected occurrences over `[2026-09-05T05:34:20.160072Z, 2026-09-06T05:34:20.160072Z)`; it was not shortened to claim success. No future-window measurement or 95% acceptance claim is made.

The root cause is unresolved. Read-only code review found that the scheduler's outer exception handler records only `schedule_interrupted`, without exception type, stack or operation; the database wrapper also hides PostgreSQL exception detail. The preserved worker output therefore cannot identify the triggering exception. Healthy container state and checkpoint-only database logs do not prove that no transient failure occurred. No speculative source fix, database test, migration, backup, restart or plan change was performed during this incident response.

The next implementation step is credential-safe diagnostic logging that identifies the failed scheduler operation and exception class, with safe PostgreSQL error classification where available, before attempting a new canary. Reproduction and logging verification belong to that next change. S6 remains incomplete. The hourly task is paused; the original canary will not automatically resume or extend. Existing Steam captures and all earlier evidence remain preserved, and no commit or push was performed.
