# Contributing

Use a feature branch from `develop`; send changes to `develop`. Keep each change small enough to describe file by file. Include its reproduction when fixing a bug, followed by the same command's passing output.

Install the host prerequisites reported by `python tools/dev.py doctor`. Start an isolated instance with `python tools/dev.py --instance contribution setup --port 8004`; setup generates local configuration and performs no Steam requests. Run `python tools/dev.py --instance contribution test`. Tests use injected source responses and retained scratch schemas. Never point tests at a collection instance.

Configuration defaults and bounds belong to `src/game_census/config.py`. Runtime pins belong to `build/toolchain.lock.json`; Python dependencies belong to `uv.lock`. Deliberate dependency changes need their own verification. Check derived files with `python tools/sync_toolchain.py --check` and `python tools/sync_ci.py --check`. Compare wheel artifacts and check installed assets with `python tools/repro_build.py`.

Retain existing versioned parsers for historical captures. GET routes read stored data. New source requests need quota admission, safe failure reporting, bounded tests, and a watched manual live run before schedule activation. Use synthetic fixtures for review/privacy tests; never commit API keys, local configuration, database files or real reviewer records.

The full capacity/recovery profile is opt-in: `python tools/dev.py --instance contribution test --capacity tests/test_capacity.py`. Preview the `benchmark` configuration and available resources first. This profile uses a new scratch schema and no Steam requests; it retains its backup and recovery proof. Running the command alone does not establish the documented reference-machine performance targets.
