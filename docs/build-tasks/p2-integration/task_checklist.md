# P2 integration checklist

## Scope overview

[Requirements](PRD.md), [plan](implementation_plan.md), [brief](agent_prompt.md).

## Integration tasks

- [x] INT-01 — Restore and compare both preserved inputs; create isolated develop checkout. Evidence: setup command/archive manifest outside the repository, to be included in final evidence.
- [x] INT-02 — Resolve textual conflicts; reproduce and repair global-capture partition/upgrade compatibility. Six focused cases pass in integration-after.txt.
- [x] INT-03 — Integrate shared bounded admission, catalog credentials and existing manual/scheduler interfaces; discovery-after.txt and suite-first.txt cover deadlines, quotas and scheduler contracts.
- [x] INT-04 — Verify all-source replay, backup/restore and rebuildable migration copies; integration-after.txt covers mixed captures, protected cutover and corrupted projections.
- [x] INT-05 — Run complete application suite, isolated browser flow, pinned-input checks and reproducible builds; verify catalog/profile/history/status consumers.
- [x] INT-06 — Reconcile phase records and file purposes, validate packet with walkthrough, and commit the verified code to develop. Code commit `eb883ba` is recorded in [unedited output](evidence/code-commit-final.txt); this documentation/evidence unit is the subsequent handoff commit. Main, stash and live canary remain preserved.

## Verification and completion

Raw command output and adjacent command metadata are retained under `evidence/`. See the walkthrough for acceptance mapping.

## File summary and markers

The [plan inventory](implementation_plan.md#file-inventory-and-integration-trace) owns proposed integration files. Walkthrough will record the actual diff. `[ ]` pending, `[/]` active, `[x]` complete with command/output evidence; at most one active item. Integration completion does not complete the separate 24-hour canary requirement.
