"""Bounded manual collection: shared accounting, atomic captures, explicit outcomes."""
from datetime import datetime, timedelta, timezone
import random
import time
import httpx
from . import __version__
from .db import DatabaseError
from .sources import SourceError, players, store
from .sources.base import validate_app_id
from .sources.http import request_limits


def retry_delay(error, attempt_index, timeout_seconds):
    """Bound exponential full jitter; provider Retry-After is a minimum wait."""
    delay = random.uniform(0, min(2 ** attempt_index, timeout_seconds))
    if error.retry_after_seconds is not None:
        delay = max(delay, error.retry_after_seconds)
    return delay


def collect_once(settings, db, app_ids=None, transport=None) -> dict:
    targets = list(settings.tracking.app_ids if app_ids is None else app_ids)
    if not 1 <= len(targets) <= 25 or len(set(targets)) != len(targets):
        raise DatabaseError("Manual collection requires 1–25 unique app IDs. Correct tracking.app_ids.")
    for app_id in targets:
        validate_app_id(app_id)
    from .scheduler import attest_manual, plan
    fingerprint = plan(settings)["plan_hash"]
    deadline = datetime.now(timezone.utc) + timedelta(seconds=settings.scheduler.max_run_seconds)
    adapters = [players] + ([store] if settings.sources.store_metadata_enabled else [])
    with db.collection_lock():
        db.initialize(targets, settings.tracking.interval_seconds)
        run_id = db.start_run(targets, [a.SOURCE for a in adapters])
        report = {"run_id": run_id, "status": "failed", "apps": [], "request_count": 0,
                  "plan_hash": fingerprint, "source_transport": "live" if transport is None else "injected"}
        successes, failures = 0, 0
        try:
            with httpx.Client(timeout=settings.http.timeout_seconds, transport=transport,
                              headers={"User-Agent": f"Game-Census/{__version__}", "Accept": "application/json"}) as client:
                for app_id in targets:
                    app_report = {"app_id": app_id, "sources": []}
                    report["apps"].append(app_report)
                    for adapter in adapters:
                        outcome = {"source": adapter.SOURCE, "status": "failed"}
                        app_report["sources"].append(outcome)
                        for attempt_index in range(settings.http.max_attempts):
                            attempt_id = None
                            try:
                                group = adapter.HOST_GROUP
                                quota = getattr(settings.quota, f"{group}_rolling_24h")
                                spacing = max(settings.http.min_interval_seconds, 2 if group == "store" else 1)
                                attempt_id = db.reserve_attempt(run_id, app_id, adapter.SOURCE, group, quota, spacing,
                                                                deadline=deadline - timedelta(seconds=settings.http.timeout_seconds))
                                report["request_count"] += 1
                                request_deadline = min(deadline, datetime.now(timezone.utc) + timedelta(seconds=settings.http.timeout_seconds))
                                with request_limits(request_deadline):
                                    capture = adapter.fetch(client, app_id, settings.http.max_response_bytes)
                                capture_id = db.record_capture(run_id, attempt_id, capture)
                                # Earlier attempt failures remain in request_result; this outcome is successful.
                                outcome.pop("error", None)
                                outcome.update(status="succeeded", capture_id=capture_id,
                                               observed_at=capture.received_at.isoformat(), value=capture.value)
                                successes += 1
                                break
                            except SourceError as error:
                                outcome["error"] = error.as_dict()
                                if attempt_id:
                                    db.record_failure(attempt_id, error)
                                delay = retry_delay(error, attempt_index, settings.http.timeout_seconds)
                                available = (deadline - datetime.now(timezone.utc)).total_seconds()
                                if (not error.retryable or attempt_index + 1 >= settings.http.max_attempts
                                        or delay > settings.http.timeout_seconds
                                        or delay + settings.http.timeout_seconds >= available):
                                    failures += 1
                                    break
                                time.sleep(delay)
                    app_report["status"] = "succeeded" if all(s["status"] == "succeeded" for s in app_report["sources"]) else ("partial" if any(s["status"] == "succeeded" for s in app_report["sources"]) else "failed")
        except (Exception, KeyboardInterrupt):
            report["error"] = {"code": "collection_interrupted", "message": "Collection could not finish; any unconfirmed requests remain charged.",
                               "next_action": "Check database and collector status, then run a new bounded collection. No historical gap can be backfilled."}
            report["status"] = "partial" if successes else "failed"
            db.finish_run(report)
            raise DatabaseError("Collection stopped before completion. Inspect the last-run report and database status; reserved requests remain charged.") from None
        report["status"] = "partial" if successes and failures else ("failed" if failures else "succeeded")
        db.finish_run(report)
        if (report["status"] == "succeeded" and transport is None
                and sorted(targets) == sorted(settings.tracking.app_ids)):
            attest_manual(settings, db, report)
        return report


def collect_discovery(settings, db, operation, *, query="", page=1, max_pages=None, restart=False, app_id=None, transport=None):
    """Explicit bounded global collection using the same durable admission ledger."""
    from .sources.discovery import ADAPTERS, PLAYED, SALES, SEARCH
    from .sources.details import ADAPTERS as DETAIL_ADAPTERS
    adapters = {**ADAPTERS, **DETAIL_ADAPTERS}
    if operation not in ("charts", "search", "catalog", "details"):
        raise DatabaseError("Unknown discovery operation.")
    if operation == "catalog":
        from .catalog import sync_catalog
        return sync_catalog(settings, db, max_pages=max_pages, restart=restart, transport=transport)
    if operation == "details":
        validate_app_id(app_id)
    query = query.strip()
    if operation == "search" and (not 1 <= len(query) <= 100 or not 1 <= page <= 100):
        raise DatabaseError("Steam search requires a 1–100 character query and a page from 1 to 100.")
    sources = list(DETAIL_ADAPTERS) if operation == "details" else [PLAYED, SALES] if operation == "charts" else [SEARCH]
    deadline = datetime.now(timezone.utc) + timedelta(seconds=settings.scheduler.max_run_seconds)
    with db.collection_lock():
        db.initialize([], settings.tracking.interval_seconds)
        run_id = db.start_run([], sources)
        report = {"run_id": run_id, "status": "failed", "sources": [], "request_count": 0}
        if operation == "details":
            report["requested_app_id"] = app_id
        try:
            with httpx.Client(timeout=settings.http.timeout_seconds, transport=transport,
                              headers={"User-Agent": f"Game-Census/{__version__}"}) as client:
                for source in sources:
                    adapter = adapters[source]
                    for page_index in range(1):
                        parameters = {"cc": "US", "l": "english"}
                        if source == SEARCH:
                            parameters.update(term=query, page=page, category1=998, count=50)
                        attempt = None
                        try:
                            group = adapter.HOST_GROUP
                            attempt = db.reserve_attempt(run_id, None, source, group, getattr(settings.quota, f"{group}_rolling_24h"),
                                                         max(settings.http.min_interval_seconds, 2 if group == "store" else 1),
                                                         deadline=deadline - timedelta(seconds=settings.http.timeout_seconds))
                            report["request_count"] += 1
                            request_deadline = min(deadline, datetime.now(timezone.utc) + timedelta(seconds=settings.http.timeout_seconds))
                            with request_limits(request_deadline):
                                capture = (adapter.fetch(client, app_id, settings.http.max_response_bytes) if operation == "details" else
                                           adapter.fetch(client, parameters, settings.http.max_response_bytes, settings.sources.catalog_api_key))
                            capture_id = db.record_capture(run_id, attempt, capture)
                            report["sources"].append({"source": source, "status": "succeeded", "capture_id": capture_id,
                                                      "items": len(capture.value["items"]), "observed_at": capture.received_at.isoformat()})
                        except SourceError as error:
                            if attempt:
                                db.record_failure(attempt, error)
                            report["sources"].append({"source": source, "status": "failed", "error": error.as_dict()})
                            break
            successes = sum(row["status"] == "succeeded" for row in report["sources"])
            failures = sum(row["status"] == "failed" for row in report["sources"])
            report["status"] = "partial" if successes and failures else "failed" if failures else "succeeded"
        except Exception:
            report["status"] = "partial" if any(row["status"] == "succeeded" for row in report["sources"]) else "failed"
            report["error"] = {"code": "collection_interrupted", "message": "Discovery collection stopped; reserved requests remain charged."}
            db.finish_run(report)
            raise DatabaseError("Discovery collection stopped. Inspect database status and the last run before retrying.") from None
        db.finish_run(report)
        return report
