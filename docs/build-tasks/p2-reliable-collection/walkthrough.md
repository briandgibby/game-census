# Game Census: reliable collection walkthrough

Dated P2 record, preserved from the pre-main-update stash. See [current integration handoff](../p2-integration/walkthrough.md) for the combined codebase. Run and monitor states below describe the recorded event, not current live state.

## Outcome and acceptance boundary

The first P2 increment is implemented and locally verified. It adds durable, explicitly activated scheduled collection, shared probe/manual/scheduler accounting, and peak-preserving history with qualified growth. It remains a bounded 1–25-app, single-concurrent-collector implementation. Monthly capture/sample partition conversion, persisted rollup caching and larger-cohort canaries remain P2 follow-up work. The full P2 roadmap is not marked complete.

The existing app is available at [localhost:8000](http://127.0.0.1:8000). A separate three-game canary is at [localhost:8002](http://127.0.0.1:8002), with three real player observations and three Steam Store name captures. Neither instance has an enabled schedule. There are no fabricated historical samples or new non-Steam data sources. New source changes remain local; this implementation did not commit or push them.

References: [requirements](PRD.md), [plan](implementation_plan.md), [checklist](task_checklist.md), [execution brief](agent_prompt.md). These own requirements, intended integration and execution state; this walkthrough owns actual results and deviations.

Requirement traceability: this increment implements bounded FR-03, FR-04 and FR-10 behavior under NFR-01, NFR-02, NFR-03 and NFR-04. It preserves installation FR-01, direct player sourcing FR-02, read interfaces FR-06 and name-only FR-07. Catalog FR-05, reviews FR-08 and achievements/news FR-09 remain later-phase scope.

## Actual design and decisions

The adapter's `parameters(app_id)` owns request identity, consumed by both fetch and canonical plan hashing. `Database.reserve_attempt`, `record_capture` and `record_failure` can join the scheduler's transaction so quota, occurrence fencing, capture, projection and request outcome stay coordinated. The PostgreSQL collector lock admits one concurrent collector across manual and scheduled processes. Separate attempts remain charged after uncertain dispatch; no claim of exactly-once network delivery is made.

`003_scheduler.sql` adds control/attestation/acknowledgment events, durable occurrences, attempt association, leases and adapter-stop state. Schedule activation requires a matching successful full-plan manual run plus explicit `acknowledge --watched`. A changed effective plan invalidates activation; secrets and runtime enablement are outside the hash. `enable` starts no process. `run` defaults to one cycle and is bounded by runtime configuration. The CLI rechecks configuration during collection, handles cancellation and emits safe failure diagnostics. Expired jobs become missed; restart favors current occurrences and exposes bounded catch-up work. Repeated schema/authentication failures stop the affected adapter until renewed manual evidence and acknowledgment.

Capacity admission accounts for serialized work, rolling-window boundary bursts, reserved attempts, host spacing and a conservative four-timeout transport occupation envelope. A late response is rejected by the total acceptance deadline. This is a conservative admission calculation, not a measured Steam throughput promise. The existing one-game plan is admitted; the default three-game canary's 15-second HTTP policy yields a 369-second worst-case cycle and therefore cannot promise a 300-second cadence. An operator can increase cadence or deliberately change timeout, then run and acknowledge the new plan. The canary proves one manual cycle, not schedule admission or sustained freshness.

`schedule_coverage` derives enabled app-time and expected player slots from exact control-event timestamps. Downtime without materialized jobs and games without observations remain in the denominator. Manual observations can contribute freshness; only accepted scheduled captures count as observed scheduled occurrences. Enrolled-cadence expectations returned in history are distinct from actual scheduled expectations.

History retains raw mode compatibility and adds explicit auto resolution. UTC rollups keep original first/last/minimum/maximum samples and outage boundaries. Full-window integrals and growth use the retained series; rollup averages are never averaged again. Both adjacent growth windows and the pre-window carry-in sample count against the configured source-read bound. Inadequate coverage and zero baselines have explicit unavailable-percentage states. There is no persisted rollup cache or canonical history replacement in this increment.

## Changed files and purpose

| Action / file | Actual purpose |
|---|---|
| MODIFY `Dockerfile` | Includes the accounted probe command in the tested container |
| MODIFY `README.md` | Documents real schedule/history commands, activation, limits and phase status |
| MODIFY `docs/PRS-game-census.md` | Records this bounded P2 implementation and explicit storage follow-ups |
| MODIFY `docs/build-tasks/initial-release/implementation_plan.md` | Points current execution to the P2 increment while preserving the first-slice record |
| MODIFY `docs/build-tasks/initial-release/task_checklist.md` | Keeps full-roadmap status and links increment-owned completion |
| MODIFY `docs/build-tasks/initial-release/agent_prompt.md` | Routes continuation to the current increment |
| MODIFY `src/game_census/cli.py` | Registers schedule commands, watched acknowledgment, bounded/cancellable runs, config rechecks and history resolution; manual announcement no longer asserts schedule state |
| MODIFY `src/game_census/collector.py` | Shares deadline/accounting logic, retains plan/transport evidence and records successful full-cohort manual attestation |
| MODIFY `src/game_census/config.py` | Owns bounded scheduler settings, growth coverage threshold and maximum retained sample reads; old profiles receive defaults |
| MODIFY `src/game_census/contracts.py` | Preserves rollups, growth, extrema, integrals, tracked coverage and schedule state through API serialization |
| MODIFY `src/game_census/db.py` | Adds caller-owned transactional admission/persistence, scheduler status and bounded snapshot history reads |
| MODIFY `src/game_census/metrics.py` | Computes exact cadence slots, integral/extrema timestamps, UTC rollups, peak-preserving reduction and qualified growth |
| NEW `src/game_census/migrations/003_scheduler.sql` | Adds scheduler state and ledger tables without rewriting old captures |
| NEW `src/game_census/scheduler.py` | Owns plan admission, manual evidence, activation, durable jobs/leases, fencing, retries and lifecycle reports |
| NEW `src/game_census/schedule_coverage.py` | Rebuilds actual schedule denominators and freshness from control events and retained samples |
| MODIFY `src/game_census/sources/http.py` | Checks request deadline/cancellation and bounds HTTP waits while preserving official-host and redirect rules |
| MODIFY `src/game_census/sources/players.py` | Exposes canonical current-player request parameters to fetch and planning |
| MODIFY `src/game_census/sources/store.py` | Exposes canonical Steam Store name parameters to fetch and planning |
| MODIFY `src/game_census/templates/_chart.html` | Discloses reduction, displays qualified growth and links the matching auto-resolution JSON |
| MODIFY `src/game_census/templates/methodology.html` | Explains rollups, growth, request limits and explicit scheduling |
| MODIFY `src/game_census/templates/status.html` | Displays actual schedule admission without claiming a worker is running |
| MODIFY `src/game_census/web.py` | Uses auto-resolution chart reads, registers resolution input and invalidates displayed activation when plan configuration differs |
| MODIFY `tools/probe_sources.py` | Replaces independent planning GETs with the shipped, durably accounted collector interface; old evidence is retained |
| MODIFY `tests/test_api.py` | Updates the database fixture to accept explicit resolution |
| MODIFY `tests/test_config_cli.py` | Updates the exact manual announcement contract |
| MODIFY `tests/test_recovery.py` | Updates the shared-collector lock diagnostic expectation |
| MODIFY `tests/test_sources.py` | Uses typed settings and the shared reservation signature in existing fixtures |
| NEW `tests/test_scheduler.py` | Sixteen planner/DB proofs for activation, duplicate prevention, retry, quota, uncertain delivery, cancellation, missed slots and adapter stop/recovery |
| NEW `tests/test_schedule_coverage.py` | Nineteen independent app-time/slot, downtime, snapshot and bound cases |
| NEW `tests/test_p2_collection.py` | Thirteen probe, deadline, retry, interruption and caller-transaction cases |
| NEW `tests/test_p2_history.py` | Twenty-four golden/boundary and DB history cases, including the carry-in read limit |
| NEW `tests/test_p2_interfaces.py` | Twenty-eight configuration, CLI, public contract and presentation cases |
| NEW this packet's `PRD.md`, `implementation_plan.md`, `task_checklist.md`, `agent_prompt.md`, `walkthrough.md` | Own increment navigation, integration, execution and actual evidence without duplicating product requirements |
| NEW `evidence/*.txt` and adjacent `.txt.json` | Unedited command outputs and their exact command/cwd/exit metadata; copied evidence preserves source bytes |

No production dependency, lock version, endpoint, code license, public deployment or scheduled service changed. Existing files were backed up and restored into a separate scratch tree with matching SHA256 before edits. Removing the old probe implementation was necessary because its original purpose—three-request planning evidence—belonged to the completed planning pass; its independent network path bypassed current production accounting. The original captured evidence remains unchanged.

## Verification evidence and acceptance matrix

Every linked raw output has adjacent command metadata. Unless stated in that metadata, commands ran from the repository root. Test records are synthetic and stored in isolated scratch schemas, never inserted into the live interface.

| Criterion | Result and evidence |
|---|---|
| A1: truthful dry-run and feasibility | One-game plan admitted with two jobs and 123-second conservative cycle; [actual plan](evidence/schedule-plan-local.txt). Planner tests reject quota/cadence infeasibility |
| A2: explicit watched activation | Scheduler tests reject absent/wrong-plan manual evidence and acknowledge/enable only matching evidence; [scheduler tests](evidence/scheduler-tests.txt). Real canary [status](evidence/schedule-status-canary.txt) has no acknowledgment, enabled=false |
| A3: jobs, leases, uncertainty and no backfill | Scheduler tests exercise quota sharing, same-slot repeat, retry recovery, disabled in-flight fence, missed slots, lease theft, cancellation, bounded downtime, Retry-After and exhausted uncertain jobs |
| A4: shared request boundaries | [Targeted PostgreSQL suite](evidence/database-after-probe.txt): 48 passed. Probe delegation and transactional rollback covered. All six [live canary requests](evidence/three-app-canary.txt) succeeded using only supported Steam sources |
| A5: exact history and UI | History golden/DB and public contract cases included in the final suite; [browser canary](evidence/browser-canary.txt) and [final local browser check](evidence/browser-local-final.txt) confirm keyboard, table, windows, mobile width, no external browser requests and no collection from reads |
| A6: upgrade and reproducibility | [Upgrade](evidence/upgrade-start.txt) applied schema 3 without enabling scheduling. [Before](evidence/replay-before.txt) and [after](evidence/replay-after.txt) replay both retain 4 captures/4 projections and manifest `fb24e28952ab9ba0e4c585075648d9354d1bc5206736dd0e4190c16517451965`. [Two isolated wheels](evidence/reproducible-build.txt) match SHA256 `2de3b2279718460e16ea2cf901e86bddd5797d850763a21c1bf71210c712e35f`; [toolchain check](evidence/toolchain-check.txt) passed |
| A7: bounded real canary | Fresh isolated initialization reported zero captures, enrolled 570/730/440 and collected one manual cycle with maximum=actual=6 requests. Run `9ae7cac8-de26-4cfb-a529-a124ed665871` succeeded; [unedited record](evidence/three-app-canary.txt). No real schedule enabled |

Final whole-suite command:

```powershell
python tools/dev.py test
```

The [unedited final output](evidence/container-tests-carry.txt) records `242 passed, 2 warnings in 8.44s`. The two warnings are existing pinned Starlette/httpx test-client deprecations. Docker also emits its existing `InvalidDefaultArgInFrom` warning because the required image argument has no floating default; the wrapper supplied the pinned digest and the build succeeded. These are documented warnings, not failed checks.

Defect reproductions retained before and after the same check:

- [Enrolled cadence alignment before](evidence/history-occurrence-before.txt) / [after](evidence/history-occurrence-after.txt).
- [Probe bypass before](evidence/collection-probe-before.txt) / [after](evidence/collection-probe-after.txt).
- [Container missing probe before](evidence/database-first.txt) / [after](evidence/database-after-probe.txt).
- [Displayed changed-plan state before](evidence/status-plan-before.txt) / [after](evidence/status-plan-after.txt).
- [Manual announcement before](evidence/manual-mode-before.txt) / [after](evidence/manual-mode-after.txt).
- [Carry-in read limit before](evidence/history-carry-guard-before.txt) / [after](evidence/history-carry-guard-after.txt).

## Operational steps, known limitations and follow-up

Existing profiles need no manual schema edits or new credentials. `python tools/dev.py build` then `python tools/dev.py start` applies additive migrations and restarts the website. The final image is installed on both local and canary instances. State is retained when stopped. For schedule use, run `schedule plan`, watch a successful full-cohort `collect --once`, then explicitly `schedule acknowledge --watched` and `schedule enable`. Launch only the desired bounded `schedule run --max-cycles N`; see README for complete commands. The agent did not supply an operator acknowledgment on the user's behalf.

The six-request canary establishes current source compatibility and end-to-end operation. It does not establish a 72-hour freshness target, 100/2,500-game capacity, full-size restore, public data distribution, or unattended-service reliability. Partition conversion and persistent rollup caching need their own measured workload and restore/cutover work; P3 catalog and P4 enrichments are unchanged. All source-provided timestamps and local observation scope remain visible.

## Reviewer quick check

```powershell
python tools/dev.py app schedule plan
python tools/dev.py app schedule status
python tools/dev.py app history --app-id 570 --hours 24 --resolution auto
python tools/dev.py test
python tools/export_plan.py --check
```

Read-only review: [local app](http://127.0.0.1:8000), [three-game canary](http://127.0.0.1:8002), [canary methodology](http://127.0.0.1:8002/methodology), [canary status](http://127.0.0.1:8002/status). Page requests must not change the attempt/capture ledger. Inspection of desktop and mobile screenshots confirmed the new growth explanation fits the existing layout; incomplete windows show unavailable growth with their coverage rather than a misleading percentage.
