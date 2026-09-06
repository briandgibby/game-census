# Game Census: P2 storage execution brief

## Mission and reference inputs

Implement the [plan](implementation_plan.md) against the canonical [PRD](../../PRD-game-census.md) and [PRS](../../PRS-game-census.md); maintain the [checklist](task_checklist.md) and write a completion walkthrough with commands and unedited output.

## Workflow instructions

Preserve the existing uncommitted reliable-collection increment. All 203 existing task files were backed up and restored with matching hashes before this packet. Use only unique scratch schemas for tests. Do not change live storage until the shipped backup has been restored and verified against the exact source state. A migration must preserve global capture/attempt identity and every retained canonical fact. No upstream network requests or unattended schedules from tests.

Coordinate bounded ownership recorded in the plan. Root integrates CLI/config/docs. Values belong in configuration with code-owned bounds. Every operation has a direct interface; no manual state setup. All failure paths must be actionable and credential-safe. New parser/metric versions and cache identity must have one owner. Any bug starts with a failing reproduction and retains before/after output.

## Verification and deliverables

Read the implementation before modifying it. State why legacy tables/constraints exist before replacing them. Preserve prior evidence byte-for-byte. Use pinned dependencies; no install/update drift. Run useful targeted checks, then the container suite and installed-state preservation checks. Do not publish, expand the live cohort, or acknowledge the user's watched run on their behalf. The 24-hour canary remains pending until the real window has elapsed and been measured.
