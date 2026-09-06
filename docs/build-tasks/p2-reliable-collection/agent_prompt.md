# Game Census: reliable collection execution brief

## Mission and references

Implement the bounded P2 increment in the [plan](implementation_plan.md), using the [canonical requirements](../../PRD-game-census.md) and updating the [checklist](task_checklist.md). The user authorized the next phase after publishing the first usable slice. Stop after local implementation, verification and evidence; do not infer authorization to publish the new work or start an unattended collector.

Preserve direct official Steam sourcing, zero/error distinctions, canonical captures, frozen dependencies and existing local profiles. The current rollout remains 1–25 configured games and one concurrent collector. No API key, catalog expansion or hosting change is needed. Reads and dry-runs make no Steam calls. All live probes use the collector's durable ledger. Watch one bounded manual run before any real activation; only an explicit Operator acknowledgment can authorize schedule enablement.

## Workflow instructions

Sequence: scheduler schema and lifecycle, shared request admission, exact history calculations, CLI/config/API/UI wiring, then full integration and bounded real-input run. Parallel agents may own independent files/methods; keep one active checklist marker per agent. Record discovered bug reproduction before fixes, repeat the same command after, and retain raw output. Back up existing files and restore/verify scratch before edits. Never delete canonical data or run a destructive downgrade. Reconcile the plan before knowingly deviating.

## Verification and deliverables

From the repository root, run the [plan's exact verification commands](implementation_plan.md#verification-and-safe-migration). Inspect all failures and follow targeted tests with `python tools/dev.py test`. `python tools/dev.py app schedule plan` must work without network requests; `schedule enable` must refuse missing or changed attestation. Build/start must leave scheduling inactive. Use an isolated instance for the three-app live canary and retain evidence. Create `walkthrough.md` only after implementation; map criteria A1–A7, file purposes, command/output and remaining phase work. Validate this packet with the canonical PRD selected from `docs` and the build-task validator using `--strict --max-in-progress 4`, then `--require-walkthrough` at completion.
