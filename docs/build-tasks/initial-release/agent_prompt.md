# Game Census: execution brief

P2 acceptance is recorded in the [P2 closeout](walkthrough.md#p2-closeout--2026-09-07), combining preserved live-canary evidence with current integration and diagnostic tests. Continue P3/P4 on `codex/feat-p2-p4`, based on `develop`; no PR. The [P2 integration packet](../p2-integration/implementation_plan.md) and older collection/storage packets remain dated evidence.

Catalog, global charts and game profiles were merged separately and are retained by this integration. Their presence does not complete the remaining P3/P4 acceptance tasks. The first-usable contract below is historical.

## Historical first-usable mission and authorization boundary

Current authorization supersedes the historical mission below: finish P2, then P3 and P4 if no blockers arise. Work on `codex/feat-p2-p4` from `develop`; retain the no-PR instruction. The checklist owns progress and the implementation plan's authorized continuation owns new file-level work. P2 starts with safe interruption diagnostics and evidence reconciliation. P3/P4 still require their full product acceptance, including real bounded source checks; passing synthetic tests cannot replace those checks. Preserve dated walkthroughs and identify image/commit provenance for live evidence.

The active goal is **Build the first usable version**. Implement and verify the P0–P1 local slice: generated setup, one real game's current-player count, durable captures, bounded recorded history, CLI, read API and website. Name-only optional Store metadata provides game identity. Manual collection remains bounded; no scheduler is enabled. The full P2–P5 roadmap, external accounts/keys, contact, commits/pushes, public hosting and distribution are outside this build's completion claim.

Build a self-hostable service for observed Steam concurrency and public game statistics using deterministic component interfaces. Start with one real game and one collection cycle, then durable history and UI. The end product uses separate CLI collector/server/metric/recovery commands over a shared Python package and PostgreSQL. No language model connects components.

## References and canonical inputs

Read the [PRD](../../PRD-game-census.md) for product intent, capabilities, success targets and scope. Read the [PRS](../../PRS-game-census.md) for source classifications, metric definitions, interfaces and configuration policy. Read [implementation_plan.md](implementation_plan.md) for exact planned file inventory, proposed commands, integrations, tests and effort. Maintain [task_checklist.md](task_checklist.md) as the implementation status owner. [BACKLOG.md](../../BACKLOG.md) owns deferred decisions. [Source-probes.json](evidence/source-probes.json) is a dated three-request planning observation, not a performance or availability guarantee. [Planning evidence](evidence/planning-evidence.txt) records checks actually run for the original packet.

The first usable application source, pinned dependencies, migrations and tests are implemented and locally verified. The checklist owns status; the completion walkthrough owns observed build evidence. The source remote is [briandgibby/game-census](https://github.com/briandgibby/game-census). Consult Git for the current commit/synchronization state. No active schedules exist. Inspect current files before relying on a proposed future command.

## Required workflow and constraints

1. Execute only the user's authorized scope. Before each step mark its checklist item `[/]`; at most one active item per executing agent. Close it only when actual command/output evidence supports completion. Save failed evidence as well as the passing rerun.
2. Build from empty state. Initialization, configuration generation, local secrets, schema/partitions and initial cohort must come from shipped commands. No manually placed file, database insert, or source-only hidden value. External facts such as API keys arrive only through configuration and missing facts are named by the program.
3. All operator values are validated configuration; code owns bounds and accepts no unsafe file/URL input. Keep source registries, schemas, metric definitions and field names canonical. Derived docs/exports/projections must have a reproducible generator.
4. Use the documented public Steam host. Start keyless with concurrency only. Enabling catalog/schema requires an operator-provided eligible Web API key. All production requests share durable quota/host accounting; no key/IP/account rotation or page-triggered bypass.
5. Preserve source observations as canonical acquired history. They cannot be fetched again after the moment passes. Use explicit source versions, UTC receipt times, cohort/cadence history and query filters. Successful zero, missing, unsupported, stale and error are distinct.
6. Follow PRS definitions exactly: observed peaks since tracking began, coverage-aware weighted averages, average-CCU growth, query-qualified review counts and country/currency-qualified prices. Do not promise all-time Steam records, full-catalog freshness, DAU/MAU, owners, sales or revenue.
7. State targets and maximum work before side effects. Use dry-runs, then a watched bounded manual run before a schedule or expanded scope. The effective collection-plan hash excludes enable/disable state and includes collection scope/policy. A test or an agent's acknowledgment is not a person's watched-run attestation.
8. Record every failure with redacted context and next action; stop required failed components. Explicit partial run results are nonzero and cannot masquerade as whole success. If persistence is unavailable, report on stderr instead of silently dropping diagnostics.
9. For every bug fix, first produce a failing reproduction. If it cannot be reproduced, instrument it instead of speculating. Change and name one diagnostic variable per run. After the actual fix, repeat the same command and retain both outputs.
10. Before removing any code/check/column/setting, explain its purpose; prove dead code with an actual reference/caller/data check if using that exception. Before overwrite/drop/delete, test restore into scratch and confirm contents. Derived-data regeneration is a restore path only after the regeneration command has actually succeeded.
11. Pin exact runtimes, transitive packages, installer/build tools, container digests, actions and frontend assets. No `latest` or ambient upgrades. Same inputs must reproduce the canonical artifact. Record deliberate upgrades separately.
12. Each component exposes CLI/HTTP/GUI and inspectable typed data. Use one name for each entity and refer to external-to-internal mappings from the repository README. Explain every modified file and why it changed before accepting the change.

## Phase instructions

The P3 read increment is verified in the walkthrough: shared bounded UTC comparison/ranking services, typed paginated catalog search, stored-data pages and polling lifecycle. Group E records its file trace and the checklist links the commands/output. Group D's cohort implementation is verified by the 468-test suite and browser evidence in the walkthrough. Schema 008 adds adoption/tracking ends and a derived name view; runtime, disabled-policy reconciliation, quota reserves, stopped/reopened cache coverage and native scratch restore are covered. Next are the separately listed authenticated catalog and watched bounded-cohort acceptance gates. Keep the dedicated integration instance static on app 570 with scheduling disabled until that bounded live work is explicitly prepared. Authenticated catalog validation still requires the key through configuration. P4 source acceptance remains pending. Synthetic evidence does not complete live cohort or source gates.

For the active P3 catalog increment, follow the Group D current increment before its historical proposed inventory. Keep the retained v1 parser; use the new v2 source for collection, with capture-derived policy/checkpoints. Verify `python tools/dev.py --instance p2-integration test tests/test_catalog.py tests/test_discovery.py tests/test_p2_integration.py` after a pinned image build. The CLI dry-run must name page/attempt/write bounds without a Steam request or database write. Live source validation remains pending the configured key.

Implement A and the source subset of B first for a real CLI result. Finish B's persistent web slice before expanding functionality. Implement C for trustworthy history and scheduling; then D's catalog and E's read product for alpha. Add D/E enrichments one at a time after their source probes and fixtures. Finish H for recovery, measured capacity, pinned release, docs and evidence. Keep P6 as research unless separately authorized.

Parallel work may cover independent UI/adapters after shared contracts stabilize; one owner coordinates migrations, registry/config and integrated commands. Do not substitute a mocked end-to-end demo for the required real-source slice. Deterministic mocks belong in automated testing and must remain visibly separate from real observations.

## Verification commands and deliverables

All commands run from the repository root. **Currently available planning command:** `python tools/probe_sources.py` (three public GETs, no retries; avoid repeating without a source-recheck need). **Planning artifact command:** `python tools/export_plan.py --output-dir outputs` (creates deterministic derived files). Use the checked-in evidence to see what was actually run.

The README and implementation plan identify shipped first-slice commands; the walkthrough records their outputs. The next authorized phase starts from this running slice. Future scheduler, catalog, enrichment, full backup/restore and performance commands remain acceptance contracts. Verify actual help and tests before claiming a command exists.

Before calling the first usable version complete, map its FR-01/FR-02 acceptance and the implemented portions of FR-03/FR-06/FR-10/NFR-01/NFR-02/NFR-04 to observed evidence. Preserve the uncompleted full-product criteria. Create `walkthrough.md` beside this brief with exact commands, unedited output, changed-file purposes, limitations and reproduction instructions. Unexecuted checks remain explicitly unexecuted. Public release and external actions require their own authorization.
