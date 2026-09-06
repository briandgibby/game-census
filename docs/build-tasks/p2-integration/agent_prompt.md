# P2 integration execution brief

## Mission and references

Integrate the preserved P2 state with current catalog/profile main on the user-requested `develop` branch. Follow [requirements](PRD.md), [implementation plan](implementation_plan.md), [checklist](task_checklist.md). Scope ends with locally verified code, evidence, reconciled phase records and develop commits. Remote publication and live deployment are outside scope.

## Workflow instructions

Work in the isolated integration checkout. Preserve the original main checkout, stash, tested archive restores and running canary. No live database tests, migrations, backups, schedules or source calls. Use a distinct `p2-integration` Docker instance and generated configuration. Keep unknown credentials in configuration and never print them.

Resolve imports/registrations first, reproduce mixed-source persistence gaps, fix migration/partition/replay compatibility, verify shared admission, then run API/UI and full suite. Every bug fix needs the same failing and passing command. State purpose before replacing code or constraints, verify the restore path first, keep failures visible and use pinned dependencies.

## Verification and deliverables

From the integration repository run `python tools/dev.py --instance p2-integration build` and the scoped/full `test` commands in the plan. Mark exactly one checklist item active; complete only with unedited output. Update all packet facts when evidence changes the plan. Finish with walkthrough, exact modified-file purposes and strict packet validation. P2 canary acceptance remains pending independently.
