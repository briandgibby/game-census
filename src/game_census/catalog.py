"""Bounded catalog lifecycle with checkpoints derived from canonical captures."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import sys
import uuid

import httpx
from psycopg.types.json import Jsonb

from . import __version__
from .db import DatabaseError, iso
from .sources import SourceError
from .sources import catalog as source
from .sources.discovery import CATALOG as LEGACY_SOURCE
from .sources.http import request_limits


def policy(settings):
    return {"page_size": settings.catalog.page_size,
            **{key: value for key, value in settings.catalog.model_dump().items() if key.startswith("include_")}}


def _hash(parameters):
    return hashlib.sha256(json.dumps({"source": source.SOURCE, "parameters": parameters},
                                    sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def checkpoint(capture, value):
    """Canonical request context owns scan identity; this table is a projection."""
    try:
        scan = capture["parameters"]["_catalog_scan"]
        started = datetime.fromisoformat(scan["started_at"])
        if started.tzinfo is None or scan["mode"] not in ("full", "incremental"):
            raise ValueError()
        parameters = scan["policy"]
        return {"capture_id": capture["capture_id"], "scan_id": uuid.UUID(scan["scan_id"]),
                "started_at": started, "mode": scan["mode"],
                "if_modified_since": capture["parameters"].get("if_modified_since", 0),
                "last_appid": value["last_appid"], "complete": not value["have_more_results"],
                "policy_hash": _hash(parameters)}, parameters
    except (KeyError, TypeError, ValueError):
        raise DatabaseError("A retained catalog capture has invalid scan context. Inspect its canonical request parameters before replaying.") from None


def project(conn, capture, value):
    row, parameters = checkpoint(capture, value)
    conn.execute("INSERT INTO source_policy(policy_hash,source,parameters) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING",
                 (row["policy_hash"], source.SOURCE, Jsonb(parameters)))
    conn.execute("""INSERT INTO catalog_checkpoint(capture_id,scan_id,started_at,mode,if_modified_since,last_appid,complete,policy_hash)
        VALUES(%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""", tuple(row.values()))


def verify(conn, capture, value):
    expected, parameters = checkpoint(capture, value)
    row = conn.execute("SELECT * FROM catalog_checkpoint WHERE capture_id=%s", (capture["capture_id"],)).fetchone()
    saved_policy = conn.execute("SELECT source,parameters FROM source_policy WHERE policy_hash=%s", (expected["policy_hash"],)).fetchone()
    if row != expected or saved_policy != {"source": source.SOURCE, "parameters": parameters}:
        raise DatabaseError("A catalog checkpoint or source policy differs from its canonical capture. Inspect a scratch restore before changing storage.")


def state(db):
    with db.connection() as conn:
        latest = conn.execute("""SELECT c.*,s.observed_at,p.parameters AS policy FROM catalog_checkpoint c
            JOIN discovery_snapshot s USING(capture_id) JOIN source_policy p USING(policy_hash)
            ORDER BY s.observed_at DESC,c.capture_id DESC LIMIT 1""").fetchone()
        completed = conn.execute("SELECT started_at FROM catalog_checkpoint WHERE complete ORDER BY started_at DESC LIMIT 1").fetchone()
        full = conn.execute("""SELECT s.observed_at FROM catalog_checkpoint c JOIN discovery_snapshot s USING(capture_id)
            WHERE c.complete AND c.mode='full' ORDER BY s.observed_at DESC LIMIT 1""").fetchone()
        legacy = None
        if latest is None:
            legacy = conn.execute("SELECT value,observed_at FROM discovery_snapshot WHERE source=%s ORDER BY observed_at DESC,capture_id DESC LIMIT 1",
                                  (LEGACY_SOURCE,)).fetchone()
    return {"last_appid": latest["last_appid"] if latest else legacy["value"]["last_appid"] if legacy else 0,
            "complete": latest["complete"] if latest else not legacy["value"]["have_more_results"] if legacy else False,
            "observed_at": iso(latest["observed_at"] if latest else legacy["observed_at"] if legacy else None),
            "scan_id": str(latest["scan_id"]) if latest else None,
            "started_at": iso(latest["started_at"]) if latest else None,
            "mode": latest["mode"] if latest else None,
            "if_modified_since": latest["if_modified_since"] if latest else 0,
            "policy": latest["policy"] if latest else None,
            "completed_watermark": iso(completed["started_at"]) if completed else None,
            "full_completed_at": iso(full["observed_at"]) if full else None}


def _now(db):
    with db.connection() as conn:
        return conn.execute("SELECT clock_timestamp() AS now").fetchone()["now"]


def plan(settings, db, *, max_pages=None, restart=False):
    pages = settings.catalog.max_pages_per_run if max_pages is None else max_pages
    if type(pages) is not int or not 1 <= pages <= 20:
        raise DatabaseError("Catalog max-pages must be from 1 through 20. Request a smaller bounded run.")
    current, now = state(db), _now(db)
    requested = policy(settings)
    pending = current["scan_id"] and not current["complete"]
    if pending and not restart and current["policy"] != requested:
        raise DatabaseError("The catalog request policy changed during a partial scan. Run catalog sync --restart to start a new scan; retained pages stay available.")
    if pending and not restart:
        scan = {"scan_id": current["scan_id"], "started_at": current["started_at"], "mode": current["mode"], "policy": requested}
        cursor, since, action = current["last_appid"], current["if_modified_since"], "resume"
    else:
        last = datetime.fromisoformat(current["observed_at"]) if current["observed_at"] else None
        full_at = datetime.fromisoformat(current["full_completed_at"]) if current["full_completed_at"] else None
        full_due = full_at is None or (now-full_at).total_seconds() >= settings.catalog.full_scan_interval_seconds
        changed = current["policy"] != requested
        idle = not restart and not changed and not full_due and last and (now-last).total_seconds() < settings.catalog.sync_interval_seconds
        mode = "full" if restart or full_due or changed or not current["completed_watermark"] else "incremental"
        watermark = datetime.fromisoformat(current["completed_watermark"]) if current["completed_watermark"] else None
        since = max(0, int(watermark.timestamp()) - settings.catalog.overlap_seconds) if mode == "incremental" else 0
        cursor, action = 0, "idle" if idle else mode
        scan = {"scan_id": None, "started_at": now.isoformat(), "mode": mode, "policy": requested}
    parameters = {"last_appid": cursor, "max_results": requested["page_size"],
                  **{key: value for key, value in requested.items() if key.startswith("include_")}}
    if since:
        parameters["if_modified_since"] = since
    return {"operation": "catalog_sync", "action": action, "source": source.SOURCE,
            "max_pages": pages, "maximum_requests": 0 if action == "idle" else pages,
            "maximum_run_seconds": settings.scheduler.max_run_seconds,
            "parameters": parameters, "scan": scan, "state": current,
            "key_configured": bool(settings.sources.catalog_api_key),
            "writes": ["runs", "charged attempts", "canonical captures", "catalog entries", "source policy", "scan checkpoints"],
            "tracking_enrollment": False}


def sync_catalog(settings, db, *, max_pages=None, restart=False, transport=None):
    if not settings.sources.catalog_api_key:
        raise SourceError("catalog_key_required", "Catalog sync requires sources.catalog_api_key.",
                          "Configure a Steam Web API key before running catalog sync.")
    with db.collection_lock():
        db.initialize([], settings.tracking.interval_seconds)
        scope = plan(settings, db, max_pages=max_pages, restart=restart)
        if scope["action"] == "idle":
            return {"status": "succeeded", "request_count": 0, "catalog_complete": True, "sources": [], "plan": scope}
        scope["scan"]["scan_id"] = scope["scan"]["scan_id"] or str(uuid.uuid4())
        run_id = db.start_run([], [source.SOURCE])
        result = {"run_id": run_id, "status": "failed", "request_count": 0, "sources": [], "catalog_complete": False, "plan": scope}
        deadline = datetime.now(timezone.utc) + timedelta(seconds=settings.scheduler.max_run_seconds)
        parameters = dict(scope["parameters"])
        try:
            with httpx.Client(timeout=settings.http.timeout_seconds, transport=transport,
                              headers={"User-Agent": f"Game-Census/{__version__}"}) as client:
                for _ in range(scope["max_pages"]):
                    attempt = None
                    try:
                        attempt = db.reserve_attempt(run_id, None, source.SOURCE, source.HOST_GROUP,
                            settings.quota.webapi_rolling_24h, settings.http.min_interval_seconds,
                            deadline=deadline-timedelta(seconds=settings.http.timeout_seconds))
                        result["request_count"] += 1
                        with request_limits(min(deadline, datetime.now(timezone.utc)+timedelta(seconds=settings.http.timeout_seconds))):
                            capture = source.fetch(client, parameters, settings.http.max_response_bytes, settings.sources.catalog_api_key)
                        capture = replace(capture, parameters={**parameters, "_catalog_scan": dict(scope["scan"])})
                        capture_id = db.record_capture(run_id, attempt, capture)
                        result["sources"].append({"source": source.SOURCE, "status": "succeeded", "capture_id": capture_id,
                                                  "items": len(capture.value["items"]), "observed_at": capture.received_at.isoformat()})
                        if not capture.value["have_more_results"]:
                            result["catalog_complete"] = True
                            break
                        parameters = {**parameters, "last_appid": capture.value["last_appid"]}
                    except SourceError as error:
                        if attempt:
                            db.record_failure(attempt, error)
                        result["sources"].append({"source": source.SOURCE, "status": "failed", "error": error.as_dict()})
                        break
            successes = sum(row["status"] == "succeeded" for row in result["sources"])
            result["status"] = "succeeded" if result["catalog_complete"] else "partial" if successes else "failed"
            if not result["catalog_complete"]:
                result["next_action"] = "Inspect source outcomes, then resume with catalog sync --once. The completed watermark has not advanced."
        except Exception:
            result["status"] = "partial" if result["sources"] else "failed"
            result["error"] = {"code": "catalog_interrupted", "message": "Catalog sync stopped; reserved attempts remain charged.",
                               "next_action": "Check database health and catalog status before resuming."}
        try:
            db.finish_run(result)
        except Exception:
            print(json.dumps({"event": "catalog_failure", "status": "failed", "run_id": run_id,
                              "report_persisted": False, "error": result.get("error"),
                              "next_action": "Preserve stderr and inspect database health and retained request attempts."}), file=sys.stderr, flush=True)
            raise DatabaseError("Catalog report could not be persisted. Preserve stderr and check database health before retrying.") from None
        return result
