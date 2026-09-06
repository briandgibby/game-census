# P2 integration requirements

Integrate preserved P2 scheduling, storage and recovery with the catalog, dashboard and profile implementation on `develop`. [Plan](implementation_plan.md), [checklist](task_checklist.md), [brief](agent_prompt.md). Product meanings remain in [the product PRD](../../PRD-game-census.md); this packet owns integration acceptance only.

## Verified state and scope

Main input is `f6c4aaa3f568759472d8ccc1a3420bbd0cd37e35`; preserved P2 input is stash `89a88af73e6b62ee0472155ea24aba011d16a3c5`, including its third-parent untracked tree. Restored archives matched 116 main files and 504 P2 files before integration. Nine textual conflicts exist. Current `003_discovery.sql` and P2 `003_scheduler.sql` both record schema version 3. P2 recovery recognizes only player/name projections; discovery requires additional canonical replay checks. Global captures have null app IDs and must survive partition conversion.

In scope: integration, compatible fresh initialization and upgrades from both inputs, shared bounded admission, exact replay and restore, phase documentation, tests, reproducible builds and commits on `develop`. No publication, live deployment, canary changes, new source capability or P3 completion claim. The running canary remains on its existing image and database.

## Requirements and acceptance criteria

| ID | Requirement | Verification |
|---|---|---|
| INT-01 | Retain both inputs with a tested restore path; preserve main and stash | Setup archive comparisons; final Git state |
| INT-02 | Both existing layouts and an empty database support tracked and global captures, preserving identity, references and history | `tests/test_p2_integration.py`, bootstrap/storage suites |
| INT-03 | Scheduler, catalog and profile requests share durable quotas and bounded HTTP deadlines, including authenticated catalog headers | Scheduler/source/discovery/details suites and integration cases |
| INT-04 | Canonical replay and verified scratch restore cover every registered source and catalog projection | Mixed-source replay/recovery and corruption checks |
| INT-05 | Existing catalog/profile UI and P2 history/status contracts coexist | API/UI/browser verification; complete suite; reproducible wheels |
| INT-06 | Develop owns the verified integration and its file-purpose/evidence record; canary acceptance remains pending | Final Git inspection, checklist and walkthrough |

## Flow, constraints and risks

CLI or existing same-origin collection form → validated settings → durable admission → bounded Steam adapter → canonical capture → partition routing → deterministic projections → history/catalog/profile reads. Backup → new scratch restore → content/replay proof → explicit protected storage migration.

Compatibility and persistence are the principal risks: discovery's foreign key must follow capture identity after partition conversion, global captures cannot acquire tracked-game identity, and historical migration copies must rebuild independently. Keep configuration untrusted, credentials redacted and all failures visible. Test only a dedicated integration instance with simulated sources; no large capacity workload during the canary.
