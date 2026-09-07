"""Inspectable commands for every component in the first usable release."""
import argparse
import json
import sys
from pydantic import ValidationError
from .config import ConfigurationError, describe_settings, generate_profile, load_settings
from .db import Database, DatabaseError
from .sources import SourceError


def parser():
    p = argparse.ArgumentParser(prog="game-census", description="Collect and display recorded Steam player counts.")
    p.add_argument("--config", help="JSON configuration path (or GAME_CENSUS_CONFIG)")
    commands = p.add_subparsers(dest="command", required=True)
    config = commands.add_parser("config", help="Generate or inspect validated configuration").add_subparsers(dest="action", required=True)
    init = config.add_parser("init", help="Create a profile only when absent; never overwrite it")
    init.add_argument("--app-id", type=int, action="append")
    init.add_argument("--port", type=int, default=8000)
    desc = config.add_parser("describe", help="Print redacted settings or schema with supported bounds")
    desc.add_argument("--schema", action="store_true")
    commands.add_parser("initialize", help="Apply schema and enroll configured app IDs idempotently")
    collect = commands.add_parser("collect", help="Run one bounded manual collection")
    collect.add_argument("--once", action="store_true", required=True)
    collect.add_argument("--app-id", type=int, action="append")
    charts = commands.add_parser("charts", help="Collect Steam global charts").add_subparsers(dest="action", required=True)
    charts.add_parser("collect").add_argument("--once", action="store_true", required=True)
    catalog = commands.add_parser("catalog", help="Discover Steam games").add_subparsers(dest="action", required=True)
    catalog.add_parser("plan", help="Preview full, incremental or resumed catalog work without writes")
    catalog.add_parser("status", help="Read catalog progress and completed-scan watermark")
    sync = catalog.add_parser("sync", help="Run a bounded full, incremental or resumed catalog scan")
    sync.add_argument("--once", action="store_true", required=True)
    sync.add_argument("--max-pages", type=int, help="Override catalog.max_pages_per_run, from 1 to 20")
    sync.add_argument("--dry-run", action="store_true", help="Print the bounded plan without collecting or writing")
    sync.add_argument("--restart", action="store_true", help="Start a new bounded scan from the first page, retaining prior discoveries")
    search = catalog.add_parser("search", help="Import a bounded public Store search page")
    search.add_argument("query")
    search.add_argument("--page", type=int, default=1)
    cohort = commands.add_parser("cohort", help="Inspect and explicitly adopt a bounded tracking cohort").add_subparsers(dest="action", required=True)
    cohort.add_parser("plan", help="Read a bounded selection and quota/capacity preview; no writes or Steam calls")
    cohort.add_parser("status", help="Read the current policy and canonical adoption")
    reconcile = cohort.add_parser("reconcile", help="Preview or explicitly adopt one bounded cohort change")
    reconcile.add_argument("--once", required=True, action="store_true")
    mode = reconcile.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    reconcile.add_argument("--expected-previous-id", type=int, help="Reject adoption if the prior event changed after preview")
    report = commands.add_parser("report", help="Print recorded operations status")
    report.add_argument("--last-run", action="store_true")
    commands.add_parser("apps", help="Print the enrolled cohort with source timestamps")
    history = commands.add_parser("history", help="Print bounded history and coverage")
    history.add_argument("--app-id", type=int, required=True)
    history.add_argument("--hours", type=int, default=24)
    history.add_argument("--resolution", choices=("raw", "auto"), default="raw")
    schedule = commands.add_parser("schedule", help="Inspect and explicitly activate bounded scheduled collection").add_subparsers(dest="action", required=True)
    schedule.add_parser("plan", help="Print the effective plan and capacity; no database writes or Steam requests")
    acknowledgment = schedule.add_parser("acknowledge", help="Attest that you watched a successful manual run end to end")
    acknowledgment.add_argument("--run-id", help="Successful manual run ID; defaults to the latest matching run")
    acknowledgment.add_argument("--watched", action="store_true", required=True)
    schedule.add_parser("enable", help="Enable only the attested plan; does not launch a worker")
    schedule.add_parser("disable", help="Disable admission of scheduled collection")
    schedule.add_parser("status", help="Print plan, activation and durable job outcomes")
    worker = schedule.add_parser("run", help="Run a bounded number of cycles for an explicitly enabled plan")
    worker.add_argument("--max-cycles", type=int, default=1)
    aggregate = commands.add_parser("aggregate", help="Validate projections against canonical captures").add_subparsers(dest="action", required=True)
    rebuild = aggregate.add_parser("rebuild", help="Replay captures and rebuild bounded exact rollup caches")
    rebuild.add_argument("--app-id", type=int, help="Limit rollup rebuilding to one enrolled app")
    rebuild.add_argument("--hours", type=int, help="Limit rollup rebuilding to recent hours")
    storage = commands.add_parser("storage", help="Plan and maintain retained monthly history partitions").add_subparsers(dest="action", required=True)
    storage.add_parser("plan", help="Inspect partition state and migration bounds without writes")
    storage.add_parser("legacy-verify", help="Regenerate frozen migration copies in scratch and compare full contents")
    maintain = storage.add_parser("maintain", help="Create missing monthly partitions without deleting history")
    maintain.add_argument("--start-month", help="First UTC month, YYYY-MM; defaults to the current month")
    maintain.add_argument("--months", type=int, help="Number of months; defaults to configured lookahead")
    migrate = storage.add_parser("migrate", help="Convert legacy history only after verifying its scratch restore")
    proof = migrate.add_mutually_exclusive_group(required=True)
    proof.add_argument("--proof-id", help="Verification record returned by backup restore")
    proof.add_argument("--latest-backup", action="store_true", help="Use the latest completed backup's latest verified scratch restore")
    backup = commands.add_parser("backup", help="Create immutable snapshots and verify a new scratch restore").add_subparsers(dest="action", required=True)
    backup.add_parser("create", help="Create one complete application snapshot in storage.backup_path")
    backup_plan = backup.add_parser("plan", help="Print backup or selected restore scope without writes")
    restore = backup.add_parser("restore", help="Restore into a new database, verify content, then protect it read-only")
    for command, required in ((backup_plan, False), (restore, True)):
        selector = command.add_mutually_exclusive_group(required=required)
        selector.add_argument("--backup-id", help="Backup ID returned by backup create")
        selector.add_argument("--latest-backup", action="store_true", help="Select the latest complete backup")
    commands.add_parser("serve", help="Serve the local read-only web application")
    doctor = commands.add_parser("doctor", help="Check configuration and database readiness")
    doctor.add_argument("--http", action="store_true", help="Also require an actual healthy HTTP server on the configured local port")
    return p


def emit(value, stream=None):
    print(json.dumps(value, indent=2, ensure_ascii=False), file=stream or sys.stdout, flush=True)


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "config":
            if args.action == "init":
                emit(generate_profile(args.config, app_ids=args.app_id, port=args.port))
            else:
                emit(describe_settings(None if args.schema else load_settings(args.config)))
            return 0
        settings = load_settings(args.config)
        db = Database(settings.storage.database_url.get_secret_value())
        from . import cohort
        if args.command in ("schedule", "collect", "apps", "history", "aggregate") and not (args.command == "schedule" and args.action == "disable"):
            settings = cohort.effective(settings, db)
        if args.command == "initialize":
            db.initialize([], settings.tracking.interval_seconds)
            settings = cohort.effective(settings, db, check_disabled=True)
            result = db.initialize(settings.tracking.app_ids, settings.tracking.interval_seconds)
        elif args.command == "cohort":
            if args.action == "status":
                result = {"policy": cohort.policy(settings), "adoption": cohort.latest(db)}
            else:
                scope = cohort.plan(settings, db)
                emit(scope)
                if args.action == "plan" or args.dry_run:
                    return 0 if scope["admitted"] else 1
                if not scope["admitted"]:
                    return 1
                result = cohort.adopt(settings, db, expected_previous_id=args.expected_previous_id)
        elif args.command == "schedule":
            from . import scheduler
            if args.action == "plan":
                result = scheduler.plan(settings)
                emit(result)
                return 0 if result["admitted"] else 1
            if args.action == "acknowledge":
                result = scheduler.acknowledge(settings, db, run_id=args.run_id)
            elif args.action == "run":
                if not 1 <= args.max_cycles <= 288:
                    raise ConfigurationError("schedule --max-cycles must be from 1 through 288. Use a smaller bounded run.")
                import signal
                from threading import Event
                cancelled = Event()
                previous = signal.getsignal(signal.SIGINT)
                signal.signal(signal.SIGINT, lambda *_: cancelled.set())
                try:
                    plan = scheduler.plan(settings)
                    cancellation_reason = {}

                    def should_stop():
                        if cancelled.is_set():
                            return True
                        try:
                            current = cohort.effective(load_settings(args.config), db)
                            if scheduler.plan(current)["plan_hash"] != plan["plan_hash"]:
                                cancellation_reason["error"] = "The effective collection plan changed. Run schedule plan and repeat the watched manual-run acknowledgment."
                                cancelled.set()
                        except (ConfigurationError, DatabaseError) as error:
                            cancellation_reason["error"] = str(error)
                            cancelled.set()
                        return cancelled.is_set()

                    emit({"operation": "schedule_run", "app_ids": settings.tracking.app_ids,
                          "max_cycles": args.max_cycles, "maximum_run_seconds": settings.scheduler.max_run_seconds,
                          "plan": plan})
                    result = scheduler.run(settings, db, max_cycles=args.max_cycles, cancelled=should_stop)
                    if cancellation_reason:
                        result["cancellation_reason"] = cancellation_reason["error"]
                finally:
                    signal.signal(signal.SIGINT, previous)
                emit(result)
                return 0 if result["status"] == "succeeded" else 1
            else:
                result = getattr(scheduler, args.action)(settings, db)
        elif args.command == "collect":
            from .collector import collect_once
            targets = args.app_id or settings.tracking.app_ids
            if any(app_id not in settings.tracking.app_ids for app_id in targets):
                raise ConfigurationError("Collection app IDs must be enrolled through tracking.app_ids. Update configuration before expanding the cohort.")
            emit({"operation": "collect_once", "app_ids": targets,
                  "maximum_requests": len(targets) * (2 if settings.sources.store_metadata_enabled else 1) * settings.http.max_attempts,
                  "collection_mode": "manual"})
            result = collect_once(settings, db, targets)
            emit(result)
            return 0 if result["status"] == "succeeded" else 1
        elif args.command == "catalog" and args.action in ("plan", "status", "sync"):
            from . import catalog
            if args.action == "status":
                emit(catalog.state(db))
                return 0
            scope = catalog.plan(settings, db, max_pages=getattr(args, "max_pages", None), restart=getattr(args, "restart", False))
            emit(scope)
            if args.action == "plan" or args.dry_run:
                return 0
            from .collector import collect_discovery
            result = collect_discovery(settings, db, "catalog", max_pages=args.max_pages, restart=args.restart)
            emit(result)
            return 0 if result["status"] == "succeeded" else 1
        elif args.command in ("charts", "catalog"):
            from .collector import collect_discovery
            operation = "charts" if args.command == "charts" else "catalog" if args.action == "sync" else "search"
            result = collect_discovery(settings, db, operation, query=getattr(args, "query", ""),
                                       page=getattr(args, "page", 1), max_pages=getattr(args, "max_pages", 5),
                                       restart=getattr(args, "restart", False))
            emit(result)
            return 0 if result["status"] == "succeeded" else 1
        elif args.command == "report":
            result = db.last_run() if args.last_run else db.status()
            if result is None:
                result = {"status": "no_runs", "next_action": "Run collect --once to record observations."}
        elif args.command == "apps":
            rows = db.list_apps(settings)
            result = {"items": rows, "total": len(rows), "tracking_scope": "enrolled"}
        elif args.command == "history":
            result = (db.history(args.app_id, settings, args.hours) if args.resolution == "raw"
                      else db.history(args.app_id, settings, args.hours, resolution=args.resolution))
            if result is None:
                raise ConfigurationError("app_id has not been enrolled. Add it to tracking.app_ids and run initialize.")
        elif args.command == "aggregate":
            from . import cache
            if args.hours is not None and not 1 <= args.hours <= settings.web.max_history_days * 24:
                raise ConfigurationError("aggregate --hours must be from 1 through web.max_history_days × 24.")
            if args.app_id is not None and args.app_id not in settings.tracking.app_ids:
                raise ConfigurationError("aggregate --app-id must be included in tracking.app_ids.")
            state = db.status()
            emit({"operation": "aggregate_rebuild", "canonical_replay": "all retained captures",
                  "capture_count": state["captures"], "cache_app_count": state["tracked_apps"] if args.app_id is None else 1,
                  "cache_app_id": args.app_id, "cache_hours": args.hours,
                  "maximum_cache_buckets_per_app": settings.cache.rebuild_max_buckets})
            result = {**db.rebuild(), "rollup_cache": cache.rebuild(db, settings, app_id=args.app_id, hours=args.hours)}
        elif args.command == "storage":
            from . import storage
            if args.action == "plan":
                result = storage.plan(settings, db)
            elif args.action == "maintain":
                emit({"operation": "storage_maintain", "start_month": args.start_month,
                      "months": args.months if args.months is not None else 1 + settings.storage.partition_months_ahead,
                      "maximum_months": settings.storage.max_partition_months, "canonical_history_deleted": False})
                result = storage.maintain(settings, db, start_month=args.start_month, months=args.months)
            elif args.action == "legacy-verify":
                emit({"operation": "storage_legacy_verify", "maximum_captures": settings.storage.migration_max_captures,
                      "destination": "new scratch schema", "canonical_history_changed": False})
                result = storage.verify_legacy(settings, db)
            else:
                from . import recovery
                proof_id = recovery.latest_proof(settings) if args.latest_backup else args.proof_id
                emit({"operation": "storage_migrate", "plan": storage.plan(settings, db), "proof_id": str(proof_id)})
                result = storage.migrate(settings, db, proof_id=proof_id)
        elif args.command == "backup":
            from . import recovery
            selected = getattr(args, "backup_id", None)
            if getattr(args, "latest_backup", False):
                selected = recovery.latest_backup(settings)
            operation = "restore" if selected is not None else "backup"
            scope = recovery.plan(settings, db, operation=operation, backup_id=selected)
            if args.action == "plan":
                result = scope
            else:
                emit(scope)
                result = (recovery.backup(settings, db) if args.action == "create"
                          else recovery.restore_verify(settings, db, backup_id=selected))
        elif args.command == "doctor":
            result = {"configuration": "ok", **db.status()}
            if args.http:
                import httpx
                try:
                    response = httpx.get(f"http://127.0.0.1:{settings.web.port}/health/ready", timeout=settings.http.timeout_seconds)
                    response.raise_for_status()
                    if response.json() != {"status": "ready"}:
                        raise ValueError("Unexpected readiness response")
                except (httpx.HTTPError, ValueError):
                    raise DatabaseError("HTTP readiness failed. Check the web process and web.bind/web.port settings, then run start again.") from None
                result["http"] = "ready"
        elif args.command == "serve":
            import uvicorn
            from .web import create_app
            db.status()  # Fail clearly before accepting requests against an empty/unavailable schema.
            uvicorn.run(create_app(settings, db), host=settings.web.bind, port=settings.web.port, access_log=False)
            return 0
        emit(result)
        return 0
    except (ConfigurationError, DatabaseError, SourceError) as error:
        emit({"status": "failed", "error": str(error)}, sys.stderr)
        return 1
    except ValidationError as error:
        keys = sorted({".".join(map(str, item["loc"])) for item in error.errors()})
        emit({"status": "failed", "error": "Invalid configuration setting(s): " + ", ".join(keys) + ". Run config describe --schema for bounds."}, sys.stderr)
        return 1
    except OSError:
        emit({"status": "failed", "error": "File or network operation failed. Check configuration paths, permissions, database status and available disk space."}, sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
