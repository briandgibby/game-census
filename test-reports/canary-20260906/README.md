# Game Census 24-hour canary: September 6–7, 2026

<!-- doc-governance: essential; canonical: self; checked: 2026-09-07 -->

This directory preserves the completed P2 canary's raw evidence. It is an archival export of retained application records and command outputs; it does not define the collection plan or product requirements.

The fixed UTC window was `2026-09-06T03:35:23.553903Z` through `2026-09-07T03:35:23.553903Z`, for apps 440, 570, and 730 at a 300-second cadence.

- [Run report](canary-final-report.txt): 288 cycles, 864 successful requests, zero failed or uncertain attempts. The worker finished at `2026-09-07T03:30:27.154434Z`.
- [Fixed-window coverage](canary-final-fixed-coverage-v2.txt): 864 of 864 occurrences; 259,146.698226 fresh app-seconds out of the full 259,200 denominator, or 99.979436%, exceeding the 95% target. The 53.301774 stale app-seconds remain included.
- [Disabled status](canary-final-status.txt): collection disabled after the full window elapsed. Its moving-window coverage is not the fixed-window result.
- [Coverage calculation script](canary-final-coverage-command.py): invokes the deployed `schedule_coverage.calculate` interface through Docker using a read-only database transaction. Requires the original canary instance and retained records; does not bootstrap a new instance. Run with `python test-reports/canary-20260906/canary-final-coverage-command.py` from the repository root.

The `.txt.json` sidecars preserve the exact commands, original working directories, and exit codes. Host paths describe the original execution environment. Hourly checks and launch logs are retained unchanged. The initial `canary-final-fixed-coverage.txt` records a failed diagnostic invocation; the `-v2` result is the successful corrected invocation. Historical evidence is not rewritten when a later invocation succeeds.

The original Windows launcher disappeared during the run, but the collector continued in its Docker container, as the subsequent container checks and final run report show. The container's inherited HTTP readiness check did not apply to its scheduler command.

This result establishes the bounded three-game canary outcome only. No broader collection was enabled.
