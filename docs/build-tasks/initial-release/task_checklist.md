# Game Census: implementation checklist

P2 acceptance is recorded in the [P2 closeout](walkthrough.md#p2-closeout--2026-09-07), combining preserved live-canary evidence with current integration and diagnostic tests. Continue P3/P4 on `codex/feat-p2-p4`, based on `develop`; no PR. The [P2 integration packet](../p2-integration/implementation_plan.md) and older collection/storage packets remain dated evidence.

Catalog, global charts and game profiles were merged separately and are retained by this integration. Their presence does not complete the remaining P3/P4 acceptance tasks. The first-usable contract below is historical.

## Scope and status

The **first usable version is locally verified**, authorized by the active goal “Build the first usable version.” Its acceptance boundary is P0–P1 plus the recorded-history, name-only metadata and read-only UI needed to use that slice. P2–P5 remain the full-product roadmap. Preparing a document or writing untested code completes no item. References: [PRD](../../PRD-game-census.md), [PRS](../../PRS-game-census.md), [implementation plan](implementation_plan.md), [execution brief](agent_prompt.md). Requirement meanings and success targets belong to the PRD. Technical contracts belong to the PRS. Exact file/symbol/command definitions belong to the implementation plan.

## Phase P0: prove the smallest real interface

- [x] Verify repository instructions/state; establish exact tested dependency/runtime/container pins and local assets, install and build without floating resolution. Public-release CI actions remain P5. Inventory A. NFR-04.
- [x] Implement typed configuration, generation/help, prerequisite diagnosis and CLI registration. Reject unknown/out-of-range settings and redact credentials. Inventory A. FR-01, NFR-01.
- [x] Implement source protocol/registry/shared HTTP/current-player adapter and a one-app live probe that prints real validated input/output; capture both success and failure evidence. Inventory B source files. FR-02, NFR-01.
- [x] Run `test_config_cli.py` and the core current-player cases in `test_sources.py`; show zero/error distinction and bounded request behavior. Inventory A/B. FR-01, FR-02, NFR-01.

## Phase P1: complete a persistent vertical slice

- [x] Implement migration locking, app identity/capture/run/audit schema, projections and source persistence. Inventory B. FR-02, NFR-02.
- [x] Connect wrapper → config → initialization → collector → database → read API → base/game template; expose one real observation and last-attempt state. Inventory A/B. FR-01, FR-02.
- [x] Run `test_bootstrap.py` from empty scratch volumes; repeat initialization and prove unchanged history. Watch one real one-game run end to end and save the actual URL/commands/output. FR-01, FR-02, FR-10.

## First usable additions pulled forward

- [x] Implement bounded half-open history with coverage-aware averages, cadence caps and gaps; verify analytic boundary datasets and canonical replay. `tests/test_history.py`, `tests/test_bootstrap.py`, `tests/test_recovery.py`; walkthrough evidence. FR-03, NFR-02.
- [x] Implement manual-run durable quota admission, uncertain-attempt accounting and Retry-After cooldown; record partial failures and retain source attempts. `tests/test_sources.py`, `tests/test_recovery.py`. This does not complete P2 scheduling. FR-04, FR-10, NFR-01.
- [x] Implement and verify name-only metadata, enrolled-game search/detail, read API, methodology/status, native chart and data table; inspect desktop/mobile and keyboard behavior. `tests/test_api.py`, `tests/test_ui.py`, `tools/browser_check.py`. This does not complete P3/P4. FR-06, FR-07.
- [x] Reconcile this local-slice packet and README, install the planned Apache-2.0 code default, verify identical wheel builds, export the source and evidence, and write the completion walkthrough. Full public-release/license review and P5 gates remain future. NFR-04.

## Phase P2: make history and scheduling trustworthy

P2 is accepted for the bounded three-app scope. [Closeout evidence](walkthrough.md#p2-closeout--2026-09-07) combines the full elapsed canary with the current scheduler, storage, recovery and diagnostic suite; it does not establish a larger cohort or P5 capacity.

- [x] Record safe scheduler exception context and stderr evidence if report persistence fails. `tests/test_scheduler_diagnostics.py`: [4 failed before](evidence/p2-diagnostics-before.txt), [4 passed after](evidence/p2-diagnostics-after.txt). The original canary incident remains unexplained; the change instruments future failures.

- [x] Add job/attempt/quota/tracking/partition migrations, scheduler leases/fencing, atomic attempt accounting and configuration-plan hashing. [Collection evidence](../p2-reliable-collection/walkthrough.md), [storage evidence](../p2-storage/walkthrough.md). Inventory C. FR-04, NFR-01, NFR-02.
- [x] Wire every production source/probe request through quota admission and host policy; implement retry/backoff, cancellation, missed-slot expiry and explicit partial-run exit. [Collection evidence](../p2-reliable-collection/walkthrough.md), [final suite](../p2-storage/evidence/container-suite-public-final.txt). Inventory B/C. FR-04, FR-10.
- [x] Implement metric definitions, peak-preserving history, rollups, coverage, average-CCU growth and deterministic replay. [Storage evidence](../p2-storage/walkthrough.md). Inventory C and B projections. FR-03, NFR-02.
- [x] Pass `test_history.py` with independent golden integrals, zero baseline, variable cadence, pre-window carry, UTC boundaries, no observations, duplicates and gaps. [Final suite](../p2-storage/evidence/container-suite-public-final.txt). FR-03, NFR-02.
- [x] Pass scheduler multi-worker/restart/uncertain-send cases and virtual-day coverage cases; prove accounting ceiling and actual bounded dispatch capacity with the full 24-hour canary. The denominator includes missed/failed app-time. [360-test suite](evidence/p2-closeout-suite.txt), [fixed-window coverage](evidence/p2-canary-final-fixed-coverage-v2.txt), [864-attempt run report](evidence/p2-canary-final-report.txt). FR-04, FR-10, NFR-03.
- [x] Implement dry-run, acknowledgment, enable/disable/run commands; verify changed effective collection plan invalidates acknowledgment and enablement state alone does not. Watch a bounded three-app plan before enabling its schedule. [Tests and activation evidence](../p2-storage/walkthrough.md#canary-activation-handoff). FR-04, FR-10.

## Phase P3: deliver player-statistics alpha

- [ ] Apply `007_enrichment.sql` at the start of P3 for catalog checkpoints/source policies and empty enrichment projections; implement catalog adapter/service, full/incremental reconciliation and bounded cohort policy. Inventory D. FR-05, NFR-01.
- [ ] Pass `test_catalog.py` including repeat/nonadvancing cursor, failed page, restart, stale watermark, reappearing app and no deletion after partial scans. FR-05, NFR-02.
- [ ] Implement query services, registered API paths/contracts, home/search/game/compare/methodology/status pages and bounded browser chart modules. Inventory E plus B web/contracts/templates. FR-06, NFR-01.
- [ ] Pass player/search/compare/status cases in `test_api.py`; page views produce no Steam requests and rankings disclose cohort and stale exclusions. FR-05, FR-06, NFR-03.
- [ ] Pass `test_ui.py` and manual keyboard/mobile/chart-table flow; record fresh/stale/new-app/unsupported/partial-source/failed states. FR-06.
- [ ] Watch the next bounded cohort and report actual freshness across all tracked app-time, upstream outcomes and resource use before expanding. FR-04, NFR-03.

## Phase P4: add one enrichment at a time

- [ ] Extend the source policies/projections introduced by P3's `007_enrichment.sql` with the optional Store adapter; validate candidate metadata fields and a paid/free/unavailable one-country sample. Inventory D. FR-07.
- [ ] Add Store source fixtures/projections/API/panel; test missing prices, currency switches, source drift, country identity and separate host budget. Inventory B/D/E integration. FR-07, NFR-01, NFR-02.
- [ ] Implement query-qualified summary-only review adapter; validate source scope and score denominator; wire snapshots/net deltas/API/panel with no reviewer persistence. Inventory D/E. FR-08, NFR-01.
- [ ] Implement achievement/schema and linked-news adapters, registry/config/schedules/projections, corresponding API/panels; show unsupported states and sanitize external content. Inventory D/E. FR-09, NFR-01.
- [ ] Extend `test_sources.py`/`test_api.py`/`test_ui.py` for FR-07, FR-08, FR-09; watch each adapter's bounded first run separately, then run a combined quota dry-run and canary. FR-04, NFR-03.

## Phase P5: operations, recovery and release verification

- [ ] Implement operations reports, readiness/liveness/source status and bounded metrics; logs name failures/next actions and redact secrets. Inventory H plus E status/B web. FR-10, NFR-01.
- [ ] Implement backup/new-scratch restore/verification and safe archive ownership transitions; keep canonical captures until lossless restore/replay succeeds. Inventory C/H. FR-10, NFR-02.
- [ ] Pass `test_recovery.py` with representative full-size dataset, migration failure and replay; record manifests, content equality and recovery times before any destructive operation. FR-10, NFR-02.
- [ ] Pass `test_capacity.py` with documented machine/90-day data/20rps profile; report p95 queries, failures, physical storage, backlog and tracked-app-time freshness. NFR-03.
- [ ] Install selected code license, contribution/security policies, and immutable CI actions; update README with only actual shipped commands, capabilities and limits. Inventory H. NFR-04.
- [ ] Run two isolated release builds from the same pinned inputs and compare canonical artifact hashes; test a fresh install and upgrade using those artifacts. NFR-04.
- [ ] Resolve public distribution/branding/privacy/domain/hosting scope with owner before release; show the concrete release candidate and data presentation for review. FR-06, NFR-01, NFR-04.
- [ ] Reconcile every modified file and acceptance criterion against commands and unedited output; write the actual walkthrough, validate the packet, and report remaining limitations. Inventory H walkthrough. FR-01, FR-02, FR-03, FR-04, FR-05, FR-06, FR-07, FR-08, FR-09, FR-10, NFR-01, NFR-02, NFR-03, NFR-04.

## File summary and marker legend

The generated export contains the exact file inventory from the implementation plan. Inventory references above include registrations, callers, consumers, configuration and tests, not just leaf modules:

| Group | Canonical file summary |
|---|---|
| A | [Reproducible bootstrap and interfaces](implementation_plan.md#group-a-reproducible-bootstrap-and-interfaces--p0) |
| B | [Source contract, persistence and first page](implementation_plan.md#group-b-source-contract-persistence-and-first-page--p0-p1) |
| C | [History and reliable scheduling](implementation_plan.md#group-c-history-and-reliable-scheduling--p2) |
| D | [Catalog and source expansion](implementation_plan.md#group-d-catalog-and-source-expansion--p3-p4) |
| E | [Complete read product](implementation_plan.md#group-e-complete-read-product--p3-p4) |
| H | [Operations, release and evidence](implementation_plan.md#group-h-operations-release-and-evidence--p5) |

The first-slice actual file inventory and changes are recorded in the walkthrough. Uncompleted phase inventories remain proposed; a component pulled forward does not complete its broader phase task. Implementation status is only the markers above. Use `[ ]` for not started, `[/]` for in progress, and `[x]` for completed with command/output evidence. Keep at most one `[/]` item per executing agent. No implementation item is completed by writing a document or by writing untested code. P6 research requires a separately scoped checklist.
