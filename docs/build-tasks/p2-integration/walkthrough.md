# P2 integration walkthrough

## Outcome and scope

The preserved P2 scheduler, partitioned storage, exact history rollups and verified recovery now coexist with the catalog, dashboards and game profiles from main. The working branch is `develop` in an isolated checkout. [Requirements](PRD.md), [plan](implementation_plan.md), [checklist](task_checklist.md), [brief](agent_prompt.md).

Inputs: main `f6c4aaa3f568759472d8ccc1a3420bbd0cd37e35` and preserved stash `89a88af73e6b62ee0472155ea24aba011d16a3c5`, including its untracked third parent. [Setup output](evidence/setup-evidence.txt) and [restore proof](evidence/restore-proof.json) record byte comparisons of 116 main files and 504 P2 files. The stash is retained. The original checkout, live images, databases, configuration, schedules and canary were not modified by this integration.

The code and tests are committed on develop as `eb883ba` ([exact staging and commit output](evidence/code-commit-final.txt)). The phase records and evidence form the following documentation commit. [Pre-commit fetch](evidence/git-precommit.txt) found no remote activity and reports `no_upstream` for the new local develop branch; no remote branch was set or published. Initial staging checks stopped before committing because Git recognized the old discovery schema as a rename into its exact historical fixture. The final check compares explicit paths with rename detection disabled.

This is local integration evidence. It does not complete P2's 24-hour canary, explain the earlier worker interruption, establish production capacity or approve P3/P4/P5 release acceptance. The existing scheduler's generic interruption diagnostic remains a follow-up; this integration does not speculate about that incident's cause.

## Actual design and decisions

- Manual collection keeps watched-run attestation; catalog/profile collection keeps its existing operations. Both share durable quota/cooldown admission and absolute HTTP deadlines. Authenticated catalog headers survive the HTTP merge.
- `006_discovery.sql` replaces the original main `003_discovery.sql` placement. The original migration supplied nullable global captures and discovery/catalog projections. Its replacement preserves those capabilities after P2's scheduler/storage/rollup migrations and supports both existing layouts. Existing populated storage remains legacy until a source-matching verified restore authorizes explicit cutover.
- Global captures retain null tracked-app IDs. The partitioned ledger permits them without enrolling catalog apps. Discovery foreign keys follow `capture_identity` after cutover; the previous foreign key's purpose, global capture existence, remains enforced.
- `projections.verify` owns the canonical comparison of parsed values, source/version, timestamp, request parameters and catalog entries. Database replay and scratch recovery call that same verifier. This replaces two divergent checks whose purpose was proving stored projections match their retained captures.
- Frozen legacy copies retain their original purpose and bytes. Their verification command now rebuilds discovery/catalog projections in its isolated scratch schema as well.
- Catalog/profile UI and P2 history/status coexist. The browser journey retains HTTP-200, content and keyboard checks; its mobile heading now matches the merged dashboard. The new fixture runner exercises one synthetic game with collection blocked and artwork fulfilled locally.

## Verification evidence

Commands below ran from the isolated integration repository after the relevant change. Every raw output has an adjacent `.txt.json` containing the exact argument array, working directory and exit code. Docker tests used only the dedicated `p2-integration` database and synthetic responses.

| Command | Result and unedited output |
|---|---|
| `python tools/dev.py --instance p2-integration test tests/test_discovery.py tests/test_details.py` | Before: [3 failed, 22 passed](evidence/discovery-before.txt). Same command after: [25 passed](evidence/discovery-after.txt) |
| `python tools/dev.py --instance p2-integration test tests/test_p2_integration.py` | Before: [6 failed](evidence/integration-before.txt). Same command after: [6 passed](evidence/integration-after.txt) |
| `python tools/dev.py --instance p2-integration test` | [356 passed, 2 dependency deprecation warnings, 51.51 seconds](evidence/suite-first.txt) |
| `.venv/Scripts/python.exe tools/browser_fixture_check.py` | Before: [mobile heading assertion failed](evidence/browser-before.txt). Same command after: [desktop/mobile journey passed; zero external network requests](evidence/browser-after.txt) |
| `python tools/repro_build.py` | [Two byte-identical wheels](evidence/reproducible-build.txt); SHA256 `61066c9fde97990aaecc6949925f60e90f170ecdf748852b9a34f680aa5447d4` |
| `python tools/sync_toolchain.py --check` | [Pinned generated inputs match](evidence/toolchain-final.txt) |
| `python tools/export_plan.py --check` | [Canonical phase documents and local links pass](evidence/phase-validation.txt) |

[Packet validation](evidence/packet-validation.txt) uses the installed build-packet validator with `--strict --require-walkthrough`. The [content review](evidence/content-review.txt) found no remaining merge markers or unexpected credential candidates. One historical diagnostic output is non-UTF-8 and was retained byte-for-byte as unedited evidence.

The browser output identifies its in-memory records as synthetic. Desktop and mobile profile screenshots were visually inspected: the dashboard, zero count, profile, chart/table and narrow layout were legible without horizontal overflow. This check does not validate live Steam artwork availability or upstream profile responses. API/source tests cover those captured contracts independently.

The dedicated instance was built from pinned images, generated through `config init` and started on port 8003 with zero collection. The full suite includes its default small capacity fixture; the optional larger capacity workload was not run. The canary uses a different image/database/instance.

## Acceptance matrix

| Criterion | Evidence and status |
|---|---|
| INT-01 | Archive restore comparison; original stash retained; source snapshots identified above |
| INT-02 | Fresh partitioned initialization, exact main-layout fixture and P2-layout fixture; protected populated conversion preserves captures and catalog entries |
| INT-03 | Shared deadline/quota test plus catalog-key, source, scheduler and CLI cases in the full suite |
| INT-04 | Eight mixed captures spanning players, names, charts and four profile sources survive native backup/restore; corruption rejection and legacy regeneration pass |
| INT-05 | Full suite, synthetic browser journey and identical wheels; existing dependency warnings remain |
| INT-06 | Current navigation and migration names reconciled; exact file inventory and final branch/commit evidence accompany handoff |

## Changed files and their purposes

The [generated change inventory](evidence/change-inventory.json) enumerates every path against the pinned main input, its action, purpose and reason. The [inventory generator](evidence/generate_inventory.py) derives the list from Git and file contents; it includes the restored historical command output and metadata without rewriting them. The new integration packet owns this reconciliation, while the original P2 packets retain their dated implementation and incident records.

The test fixture `tests/fixtures/main_discovery.sql` is an exact historical schema snapshot from `git show f6c4aaa:src/game_census/migrations/003_discovery.sql`. The original commit is its owner; regenerate the fixture from that command. It deliberately differs from the new migration so upgrade tests cannot accidentally test only the new schema.

## Operational steps and remaining limitations

Continue development on `develop`; this task does not publish to a remote or deploy the integrated image onto either live instance. After deployment is separately chosen, build the target image and use the shipped initialization/start interface. For a populated legacy layout, run `storage plan`, `backup create`, `backup restore --latest-backup`, then `storage migrate --latest-backup` while collection is disabled. Existing configuration and source history must be preserved. The original P2 storage walkthrough documents those direct interfaces and their earlier live proof.

Full elapsed canary freshness remains pending independently. Earlier P2 packets describe their original run and interruption, not current monitoring state. The repository's broad PRD/PRS P3/P4 acceptance contracts still need reconciliation with the separately merged profiles before those phases are claimed complete.

## Reviewer quick check

Read the six focused integration cases, migration 006, shared projection verifier and storage reference transition. Repeat the scoped suite on an isolated configured instance, then the full suite. Use `python tools/repro_build.py` for the pinned host environment and `.venv/Scripts/python.exe tools/browser_fixture_check.py` for the offline browser flow. The walkthrough's exact outputs support only the tested local workload.
