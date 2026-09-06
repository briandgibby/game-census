# Game Census: reliable collection checklist

Dated P2 record, preserved from the pre-main-update stash. See [current integration handoff](../p2-integration/walkthrough.md) for the combined codebase. Run and monitor states below describe the recorded event, not current live state.

## Scope and overview

This file owns completion for the bounded P2 increment in the [plan](implementation_plan.md). Canonical [requirements](PRD.md), [execution brief](agent_prompt.md), and the full [roadmap](../initial-release/task_checklist.md) retain their separate purposes.

Compatibility checks retain FR-02, FR-06 and name-only FR-07. Expansion of FR-05, FR-08 and FR-09 stays in later phases; its absence is not a failed P2 acceptance check.

## Implementation tasks

- [x] Implement and verify durable scheduler migration/plan/acknowledgment/activation/jobs/leases, conservative capacity and cancellation. A1–A3, FR-04, FR-10, NFR-01–NFR-03. `scheduler.py`, `003_scheduler.sql`, `test_scheduler.py`.
- [x] Unify manual/scheduled/probe admission and plan evidence; prove failure, retry, uncertain-send and canonical capture handling. A4, FR-04, FR-10. `collector.py`, database admission/persistence methods, `sources/http.py`, `tools/probe_sources.py`, `test_p2_collection.py`, `schedule_coverage.py`, `test_schedule_coverage.py`.
- [x] Implement and verify exact slots, growth, full-window coverage and peak-preserving auto history/rollups. A5, FR-03, NFR-02. `metrics.py`, database history methods, `test_history.py`, `test_p2_history.py`. Evidence: `evidence/container-tests-carry.txt` (242 passing final checks), carry-limit regression and history golden outputs.
- [x] Wire configuration, CLI, public contracts, read-only UI/status and focused interface tests. A1, A2, A5, A6. `config.py`, `cli.py`, `contracts.py`, `web.py`, affected templates, `test_p2_interfaces.py`.

## Verification and completion

- [x] Run container integration, fresh/upgrade preservation/replay and bounded three-app direct-Steam manual canary; inspect browser chart/table/status. A3–A7, FR-01, FR-03, FR-04, FR-10, NFR-01–NFR-04.
- [x] Reconcile README/roadmap, record every changed file and criterion with commands and raw output in walkthrough, validate packet and reproducible artifact. A1–A7, NFR-04.

The first five items are evidenced by the [walkthrough](walkthrough.md), the 242-test final output, real six-request canary, upgrade manifests and browser checks.

## File summary and marker legend

The [file inventory](implementation_plan.md#file-changes-and-integration) includes all registrations, callers, consumers and test paths. `[ ]` means pending, `[/]` in progress (one per executing agent), and `[x]` complete only after command/output evidence. Full P2 partition migration, persisted rollup cache and broader-cohort canaries stay pending in the roadmap. Automatic process startup and live schedule enablement are outside this implementation's completion boundary.
