# Game Census: P2 storage checklist

Dated P2 record, preserved from the pre-main-update stash. See [current integration handoff](../p2-integration/walkthrough.md) for the combined codebase. Run and monitor states below describe the recorded event, not current live state.

## Scope overview

Canonical [PRD](../../PRD-game-census.md), [PRS](../../PRS-game-census.md), [plan](implementation_plan.md), [brief](agent_prompt.md). Earlier increment status remains in its own packet. Evidence is required before a completed marker.

Preserve official current-player sourcing FR-02 and name-only FR-07 compatibility. Catalog FR-05, expanded Store FR-07, reviews FR-08 and achievements/news FR-09 remain later phases; this packet makes no completion claim for them.

## Implementation tasks

- [x] S1 — Recovery agent: implement immutable backup and verified new-scratch restore, integrity and safe failures; test complete canonical replay. [Live evidence](walkthrough.md#live-recovery-and-migration), [final suite](evidence/container-suite-public-final.txt). FR-01, FR-10, NFR-01, NFR-02.
- [x] S2/S3 — Storage agent: implement partition identity/maintenance and protected legacy migration; test empty init, boundaries, deduplication and unchanged contents. [Cutover](evidence/local-migrate-after.txt), [regeneration](evidence/local-legacy-verify-after.txt), [final suite](evidence/container-suite-public-final.txt). FR-01–FR-04, FR-10, NFR-02.
- [x] S4 — History agent: implement persisted exact rollups, consumed cache, invalidation/rebuild and raw-boundary equivalence. [Final suite](evidence/container-suite-public-final.txt), [replay](evidence/canary-replay-after-manual.txt). FR-03, FR-06, NFR-02, NFR-03.
- [x] S5 — Root: integrate validated CLI/config, measure synthetic workloads, verify installed upgrade/full suite/reproducible artifacts, document each modified file. [Verification](walkthrough.md#verification), [file purposes](walkthrough.md#file-purposes). FR-01–FR-04, FR-06, FR-10, NFR-01–NFR-04.
- [ ] S6 — Show and exercise exact three-app plan; receive watched acknowledgment before enabling; measure the full 24-hour freshness window before claiming the canary target. FR-04, FR-10, NFR-03.

S6 preparation is evidenced by the [plan](evidence/canary-plan.txt) and [successful three-request manual run](evidence/canary-manual.txt). The operator confirmed watching the run and authorized activation. [Acknowledgment](evidence/canary-acknowledgment.txt), [enablement](evidence/canary-enable.txt), [first scheduled cycle](evidence/canary-first-scheduled-cycle.txt) and [background status](evidence/canary-background-status.txt) are recorded. The first canary was interrupted at 2026-09-05T16:48:06.856752Z, before its planned 24-hour end. The worker was not restarted; the schedule and hourly check are disabled/paused. S6 is incomplete pending diagnosis and a new authorized full run. See the [interruption record](walkthrough.md#canary-interruption).

## Verification and completion

Use `[ ]` pending, `[/]` in progress, `[x]` completed with exact command/output evidence. At most one in-progress item per executing agent. No writing-only completion and no canary completion before elapsed measurement.
