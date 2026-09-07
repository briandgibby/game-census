"""Bounded durable scheduling of direct Steam observations, with explicit approval.

PostgreSQL owns occurrence time, leases, fencing and shared attempt accounting.
There is deliberately no installation-time timer or background service.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import random
import re
import sys
import time
import uuid

import httpx
from psycopg.types.json import Jsonb

from . import __version__
from .db import DatabaseError, iso
from .sources import REGISTRY, SourceError, players, store


def _adapters(settings):
    return [players] + ([store] if settings.sources.store_metadata_enabled else [])


def plan(settings) -> dict:
    """Conservative serialized dispatch admission, including boundary bursts."""
    cadence = settings.tracking.interval_seconds
    apps = sorted(settings.tracking.app_ids)
    adapters = _adapters(settings)
    attempts = settings.http.max_attempts
    sources = []
    budgets = {}
    errors = []
    cycle_seconds = 0.0
    # Retry jitter is capped at the corresponding exponential-backoff maximum.
    backoff = sum(min(2 ** index, settings.http.timeout_seconds)
                  for index in range(attempts - 1))
    for adapter in adapters:
        spacing = max(settings.http.min_interval_seconds, 2 if adapter is store else 1)
        occupation_seconds = 4 * settings.http.timeout_seconds
        per_job = attempts * (occupation_seconds + spacing) + backoff
        cycle_seconds += len(apps) * per_job
        # One extra complete boundary burst also covers lease/retry timing skew.
        occurrences = math.ceil(86400 / cadence) + 1
        scheduled = len(apps) * occurrences * attempts
        quota = getattr(settings.quota, f"{adapter.HOST_GROUP}_rolling_24h")
        dispatch_capacity = math.floor(86400 / (occupation_seconds + spacing))
        usable_reserve = max(0, min(quota, dispatch_capacity) - scheduled)
        budgets[adapter.HOST_GROUP] = {
            "rolling_24h_occurrence_ceiling": occurrences * len(apps),
            "scheduled_attempt_ceiling": scheduled,
            "configured_attempt_budget": quota,
            "configured_reserve": settings.scheduler.retry_reserve,
            "serialized_attempt_capacity": dispatch_capacity,
            "usable_reserve": usable_reserve,
            "minimum_spacing_seconds": spacing,
        }
        if scheduled + settings.scheduler.retry_reserve > quota:
            errors.append(f"quota.{adapter.HOST_GROUP}_rolling_24h cannot cover scheduled attempts and scheduler.retry_reserve")
        if usable_reserve < settings.scheduler.retry_reserve:
            errors.append(f"scheduler.retry_reserve exceeds serialized {adapter.HOST_GROUP} dispatch capacity")
        sources.append({"source": adapter.SOURCE, "version": adapter.VERSION,
                        "endpoint": adapter.URL, "host_group": adapter.HOST_GROUP,
                        "requests": [{"app_id": app_id, "parameters": adapter.parameters(app_id)} for app_id in apps]})
    reserve_seconds = sum(settings.scheduler.retry_reserve *
                          (4 * settings.http.timeout_seconds + value["minimum_spacing_seconds"])
                          for value in budgets.values())
    daily_cycles = math.ceil(86400 / cadence) + 1
    shared_spare_seconds = max(0, 86400 - daily_cycles * cycle_seconds)
    for group, budget in budgets.items():
        # Allocate equal dispatch-time shares across hosts; two separate host
        # ceilings must never promise the same single-worker spare time twice.
        shared_share = math.floor(shared_spare_seconds / (len(budgets) *
                                 (4 * settings.http.timeout_seconds + budget["minimum_spacing_seconds"])))
        budget["usable_reserve"] = min(budget["usable_reserve"], shared_share)
        if budget["usable_reserve"] < settings.scheduler.retry_reserve:
            errors.append(f"scheduler.retry_reserve exceeds the shared-worker {group} reserve allocation")
    if daily_cycles * cycle_seconds + reserve_seconds > 86400:
        errors.append("tracking.interval_seconds and http policy exceed shared serialized daily dispatch capacity")
    if cycle_seconds > cadence:
        errors.append("tracking.interval_seconds cannot fit the worst-case serialized collection cycle")
    if cycle_seconds > settings.scheduler.max_run_seconds:
        errors.append("scheduler.max_run_seconds cannot fit the worst-case serialized collection cycle")
    required_lease = 4 * settings.http.timeout_seconds + max(settings.http.min_interval_seconds, 2) + 2
    if settings.scheduler.lease_seconds < required_lease:
        errors.append("scheduler.lease_seconds must exceed four HTTP timeout phases and host spacing by at least two seconds")
    canonical = {"version": 1, "app_ids": apps, "sources": sources,
                 "cadence_seconds": cadence, "http": settings.http.model_dump(),
                 "quota": settings.quota.model_dump(), "scheduler": settings.scheduler.model_dump(),
                 "concurrency": 1, "occurrence_policy": "enable-relative UTC slots; deadline is next slot; no backfill"}
    fingerprint = hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"plan_hash": fingerprint, "admitted": not errors, "errors": errors,
            "plan": canonical, "app_count": len(apps), "app_ids": apps,
            "one_cycle_jobs": len(apps) * len(adapters),
            "one_cycle_attempt_ceiling": len(apps) * len(adapters) * attempts,
            "worst_case_cycle_seconds": cycle_seconds,
            "capacity_basis": "serialized concurrency 1; four HTTP timeout phases plus spacing and maximum retry backoff; late responses are rejected",
            "shared_spare_dispatch_seconds_24h": shared_spare_seconds,
            "budgets": budgets, "writes": "retained runs, jobs, attempts, captures, projections and approval events",
            "scheduler_enabled": False}


def _admitted(settings):
    value = plan(settings)
    if not value["admitted"]:
        raise DatabaseError("Schedule plan is infeasible: " + "; ".join(value["errors"]) + ". Correct configuration and run schedule plan.")
    return value


def attest_manual(settings, db, report) -> dict:
    """Called by the real manual collector after durable successful completion."""
    value = plan(settings)
    with db.connection() as conn:
        row = conn.execute("""SELECT r.app_ids,r.sources,c.status,c.report FROM collection_run r
            JOIN run_completion c USING(run_id) WHERE run_id=%s""", (report["run_id"],)).fetchone()
        if (row is None or row["status"] != "succeeded" or row["report"].get("plan_hash") != value["plan_hash"]
                or sorted(row["app_ids"]) != value["app_ids"]
                or sorted(row["sources"]) != sorted(a.SOURCE for a in _adapters(settings))):
            raise DatabaseError("Manual run does not cover the effective schedule plan successfully. Run collect once for the configured cohort.")
        if conn.execute("SELECT 1 FROM scheduled_run WHERE run_id=%s", (report["run_id"],)).fetchone():
            raise DatabaseError("A scheduled run cannot establish watched manual-run evidence. Run collect once.")
        captures = conn.execute("SELECT app_id,source,source_version,parameters FROM capture WHERE run_id=%s", (report["run_id"],)).fetchall()
        pairs = {(capture["app_id"],capture["source"]) for capture in captures}
        expected_pairs = {(app_id,adapter.SOURCE) for app_id in value["app_ids"] for adapter in _adapters(settings)}
        if len(captures) != value["one_cycle_jobs"] or pairs != expected_pairs:
            raise DatabaseError("Manual capture count does not match the effective plan. Run collect once for the configured cohort.")
        for capture in captures:
            adapter = REGISTRY[capture["source"]]
            expected = adapter.parameters(capture["app_id"])
            if capture["source_version"] != adapter.VERSION or capture["parameters"] != expected:
                raise DatabaseError("Manual capture source policy differs from the effective plan. Run collect once again.")
        conn.execute("INSERT INTO schedule_attestation(run_id,plan_hash,plan) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING",
                     (report["run_id"], value["plan_hash"], Jsonb(value["plan"])))
    return {"run_id": report["run_id"], "plan_hash": value["plan_hash"], "status": "manual_run_recorded"}


def acknowledge(settings, db, run_id=None) -> dict:
    value = _admitted(settings)
    with db.connection() as conn:
        row = conn.execute("""SELECT run_id FROM schedule_attestation WHERE plan_hash=%s
            AND (%s::uuid IS NULL OR run_id=%s::uuid) ORDER BY recorded_at DESC LIMIT 1""",
                           (value["plan_hash"], run_id, run_id)).fetchone()
        if row is None:
            raise DatabaseError("No successful real manual run matches this plan. Watch collect once end to end, then acknowledge that run.")
        acknowledgment = str(uuid.uuid4())
        conn.execute("INSERT INTO schedule_acknowledgment(acknowledgment_id,run_id,plan_hash) VALUES(%s,%s,%s)",
                     (acknowledgment, row["run_id"], value["plan_hash"]))
    return {"status": "acknowledged", "run_id": str(row["run_id"]), "plan_hash": value["plan_hash"],
            "acknowledgment_id": acknowledgment}


def enable(settings, db) -> dict:
    value = _admitted(settings)
    with db.collection_lock():
        db.initialize(value["app_ids"], settings.tracking.interval_seconds)
        with db.connection() as conn:
            state = conn.execute("SELECT * FROM schedule_state WHERE singleton FOR UPDATE").fetchone()
            if conn.execute("SELECT 1 FROM schedule_acknowledgment WHERE plan_hash=%s LIMIT 1", (value["plan_hash"],)).fetchone() is None:
                raise DatabaseError("This plan has no watched-run acknowledgment. Watch collect once, then run schedule acknowledge --watched.")
            stopped = conn.execute("SELECT max(stopped_at) AS at FROM schedule_adapter_stop WHERE plan_hash=%s AND resolved_at IS NULL", (value["plan_hash"],)).fetchone()["at"]
            if stopped and conn.execute("""SELECT 1 FROM schedule_acknowledgment a JOIN schedule_attestation t USING(run_id)
                WHERE a.plan_hash=%s AND t.recorded_at>%s LIMIT 1""", (value["plan_hash"],stopped)).fetchone() is None:
                raise DatabaseError("An adapter stopped after repeated contract or authentication failures. Watch a new successful manual run, acknowledge it, then enable again.")
            if state["enabled"] and state["plan_hash"] == value["plan_hash"] and not stopped:
                return {"status": "enabled", "plan_hash": value["plan_hash"], "changed": False}
            now = conn.execute("SELECT clock_timestamp() AS now").fetchone()["now"]
            enabled = conn.execute("""UPDATE schedule_state SET enabled=true,plan_hash=%s,epoch=epoch+1,
                enabled_at=%s,cursor_at=%s WHERE singleton RETURNING epoch""", (value["plan_hash"], now, now)).fetchone()
            conn.execute("UPDATE schedule_adapter_stop SET resolved_at=%s WHERE plan_hash=%s AND resolved_at IS NULL", (now,value["plan_hash"]))
            conn.execute("""UPDATE scheduled_job SET state='cancelled',lease_version=lease_version+1,error=%s
                WHERE state IN ('pending','leased','retry_pending')""",
                         (Jsonb(_error("plan_replaced", "An acknowledged plan was enabled with a new occurrence epoch.", "Inspect retained old jobs; new occurrences use the enabled plan.")),))
            conn.execute("INSERT INTO schedule_event(recorded_at,action,plan_hash,details) VALUES(%s,'enabled',%s,%s)",
                         (now, value["plan_hash"], Jsonb({"app_ids": value["app_ids"], "cadence_seconds": settings.tracking.interval_seconds,"epoch":enabled["epoch"]})))
    return {"status": "enabled", "plan_hash": value["plan_hash"], "changed": True,
            "next_action": "Run schedule run with a bounded cycle count; no background process was installed."}


def disable(settings, db) -> dict:
    with db.connection() as conn:
        conn.execute("SELECT singleton FROM schedule_state WHERE singleton FOR UPDATE")
        now = conn.execute("SELECT clock_timestamp() AS now").fetchone()["now"]
        row = conn.execute("UPDATE schedule_state SET enabled=false,epoch=epoch+1 WHERE singleton RETURNING plan_hash,epoch").fetchone()
        conn.execute("""UPDATE scheduled_job SET state='cancelled',lease_version=lease_version+1,
            error=%s WHERE state IN ('pending','leased','retry_pending')""",
                     (Jsonb(_error("schedule_disabled", "The operator disabled scheduling.", "Enable an acknowledged plan before new collection.")),))
        conn.execute("INSERT INTO schedule_event(recorded_at,action,plan_hash,details) VALUES(%s,'disabled',%s,%s)",
                     (now,row["plan_hash"],Jsonb({"epoch":row["epoch"]})))
    return {"status": "disabled", "plan_hash": row["plan_hash"]}


def status(settings, db) -> dict:
    from .schedule_coverage import report as coverage_report
    value = plan(settings)
    with db.connection() as conn:
        state = conn.execute("SELECT * FROM schedule_state WHERE singleton").fetchone()
        acknowledgment = conn.execute("SELECT run_id,acknowledged_at FROM schedule_acknowledgment WHERE plan_hash=%s ORDER BY acknowledged_at DESC LIMIT 1", (value["plan_hash"],)).fetchone()
        counts = conn.execute("SELECT state,count(*) AS n FROM scheduled_job GROUP BY state").fetchall()
        uncertain = conn.execute("SELECT count(*) AS n FROM scheduled_attempt a LEFT JOIN request_result r USING(attempt_id) WHERE r.attempt_id IS NULL").fetchone()["n"]
        oldest = conn.execute("SELECT min(scheduled_at) AS at FROM scheduled_job WHERE state IN ('pending','leased','retry_pending')").fetchone()["at"]
        stops = conn.execute("SELECT source,stopped_at,error FROM schedule_adapter_stop WHERE plan_hash=%s AND resolved_at IS NULL", (value["plan_hash"],)).fetchall()
    matches = state["plan_hash"] == value["plan_hash"]
    return {"enabled": state["enabled"] and matches, "stored_enabled": state["enabled"],
            "plan_matches": matches, "plan_hash": value["plan_hash"], "stored_plan_hash": state["plan_hash"],
            "epoch": state["epoch"], "enabled_at": iso(state["enabled_at"]),
            "acknowledged_run_id": str(acknowledgment["run_id"]) if acknowledgment else None,
            "jobs": {row["state"]: row["n"] for row in counts}, "uncertain_attempts": uncertain,
            "stopped_adapters": [{"source":row["source"],"stopped_at":iso(row["stopped_at"]),"error":row["error"]} for row in stops],
            "oldest_unfinished_occurrence": iso(oldest),
            "coverage": coverage_report(settings,db,hours=24),
            "next_action": ("Run schedule plan and repeat watched manual acknowledgment after configuration changes."
                            if state["enabled"] and not matches else "Use a bounded schedule run after watched acknowledgment.")}


def _error(code, message, next_action):
    return {"code": code, "message": message, "next_action": next_action}


def _interruption(error, stage):
    """Allowlisted code locations and error classes, never messages or locals.

    DatabaseError deliberately suppresses the driver message. Its context still
    supplies a safe SQLSTATE, which distinguishes connection loss from SQL bugs.
    Bounds protect reporting from cyclic or unusually deep exception chains.
    """
    chain, seen = [], set()
    while error is not None and id(error) not in seen and len(chain) < 8:
        seen.add(id(error))
        frames = []
        trace = error.__traceback__
        while trace is not None:
            module = trace.tb_frame.f_globals.get("__name__", "")
            if isinstance(module, str) and re.fullmatch(r"game_census(?:\.[a-z_]+)*", module):
                frames.append({"module": module, "function": trace.tb_frame.f_code.co_name,
                               "line": trace.tb_lineno})
            trace = trace.tb_next
        item = {"type": type(error).__name__, "frames": frames[-8:]}
        state = getattr(error, "sqlstate", None)
        if isinstance(state, str) and re.fullmatch(r"[A-Z0-9]{5}", state):
            item["sqlstate"] = state
        chain.append(item)
        error = error.__cause__ if error.__cause__ is not None else error.__context__
    return {**_error("schedule_interrupted", "The bounded schedule could not finish; unconfirmed attempts remain charged.",
                     "Run schedule status and report --last-run. Inspect the recorded stage, code locations and database health before retrying."),
            "diagnostic": {"stage": stage, "exception_chain": chain}}


def _emit_failure(report, error, *, unpersisted=False):
    event = {"event": "scheduler_failure", "status": "failed", "run_id": report["run_id"],
             "plan_hash": report["plan_hash"], "error": error}
    if unpersisted:
        event["report_persisted"] = False
        if report.get("error"):
            event["run_error"] = report["error"]
    print(json.dumps(event, ensure_ascii=False), file=sys.stderr, flush=True)


def _state(conn, fingerprint, epoch=None):
    row = conn.execute("SELECT *,clock_timestamp() AS now FROM schedule_state WHERE singleton FOR UPDATE").fetchone()
    if not row["enabled"] or row["plan_hash"] != fingerprint or (epoch is not None and row["epoch"] != epoch):
        raise SourceError("schedule_invalidated", "The schedule was disabled or its approved plan changed.",
                          "Inspect schedule status and acknowledge the effective plan before enabling it.")
    return row


def _materialize(settings, db, fingerprint, epoch):
    """Create current jobs first and at most 128 older slot batches per tick."""
    cadence = settings.tracking.interval_seconds
    with db.connection() as conn:
        state = _state(conn, fingerprint, epoch)
        now, anchor = state["now"], state["enabled_at"]
        current = anchor + timedelta(seconds=math.floor((now-anchor).total_seconds()/cadence)*cadence)
        cursor = state["cursor_at"]
        slots = {current}
        for _ in range(128):
            if cursor > current:
                break
            slots.add(cursor)
            cursor += timedelta(seconds=cadence)
        for at in sorted(slots, reverse=True):
            deadline = at + timedelta(seconds=cadence)
            missed = deadline <= now
            for app_id in settings.tracking.app_ids:
                for adapter in _adapters(settings):
                    conn.execute("""INSERT INTO scheduled_job(job_id,plan_hash,app_id,source,scheduled_at,deadline,state,next_attempt_at,error)
                        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(app_id,source,scheduled_at) DO NOTHING""",
                                 (str(uuid.uuid4()), fingerprint, app_id, adapter.SOURCE, at, deadline,
                                  "missed" if missed else "pending", at,
                                  Jsonb(_error("missed_occurrence", "The occurrence expired without an observation.", "Collect the current slot; historical current-player gaps cannot be backfilled.")) if missed else None))
        conn.execute("UPDATE schedule_state SET cursor_at=%s WHERE singleton", (cursor,))
        conn.execute("""UPDATE scheduled_job SET state='missed',lease_version=lease_version+1,error=%s
            WHERE state IN ('pending','leased','retry_pending') AND deadline<=clock_timestamp()""",
                     (Jsonb(_error("missed_occurrence", "The occurrence deadline expired.", "Inspect failures and collect fresh occurrences; never backfill current counts.")),))
    return current, cursor <= current


def _claim(settings, db, fingerprint, epoch, worker):
    with db.connection() as conn:
        _state(conn, fingerprint, epoch)
        conn.execute("""UPDATE scheduled_job j SET state='failed',error=s.error FROM schedule_adapter_stop s
            WHERE j.plan_hash=%s AND j.plan_hash=s.plan_hash AND j.source=s.source AND s.resolved_at IS NULL
            AND j.state IN ('pending','retry_pending')""", (fingerprint,))
        row = conn.execute("""SELECT * FROM scheduled_job WHERE plan_hash=%s AND deadline>clock_timestamp()
            AND next_attempt_at<=clock_timestamp() AND (state IN ('pending','retry_pending')
              OR (state='leased' AND lease_until<=clock_timestamp()))
            ORDER BY scheduled_at DESC,app_id,source FOR UPDATE SKIP LOCKED LIMIT 1""", (fingerprint,)).fetchone()
        if row is None:
            return None
        if row["attempt_count"] >= settings.http.max_attempts:
            conn.execute("UPDATE scheduled_job SET state='failed',error=%s WHERE job_id=%s",
                         (Jsonb(_error("attempts_exhausted", "All attempts were consumed, including uncertain delivery.", "Inspect the attempt ledger; wait for the next fresh occurrence.")), row["job_id"]))
            return {"exhausted": True}
        return conn.execute("""UPDATE scheduled_job SET state='leased',lease_owner=%s,lease_version=lease_version+1,
            lease_until=LEAST(deadline,clock_timestamp()+(%s*interval '1 second'))
            WHERE job_id=%s RETURNING *""", (worker, settings.scheduler.lease_seconds, row["job_id"])).fetchone()


def _fence(conn, job, fingerprint, epoch, worker):
    state = _state(conn, fingerprint, epoch)
    row = conn.execute("SELECT * FROM scheduled_job WHERE job_id=%s FOR UPDATE", (job["job_id"],)).fetchone()
    if (row["state"] != "leased" or str(row["lease_owner"]) != worker
            or row["lease_version"] != job["lease_version"]
            or row["lease_until"] <= state["now"] or row["deadline"] <= state["now"]):
        raise SourceError("lease_lost", "The occurrence lease or deadline no longer permits this worker's result.",
                          "Inspect schedule status; the attempt stays charged and a fresh occurrence must be collected.")
    return row, state["now"]


def _finish_job_error(settings, db, job, fingerprint, epoch, worker, error, attempt_id, until):
    with db.connection() as conn:
        current, now = _fence(conn, job, fingerprint, epoch, worker)
        if attempt_id is not None:
            db.record_failure(attempt_id, error, conn=conn)
            recent = conn.execute("""SELECT r.http_status,r.error FROM request_result r JOIN scheduled_attempt a USING(attempt_id)
                JOIN scheduled_job j USING(job_id) WHERE j.plan_hash=%s AND j.source=%s
                ORDER BY r.completed_at DESC LIMIT 2""", (fingerprint,job["source"])).fetchall()
            schema_codes = {"invalid_json","invalid_schema","invalid_player_count","invalid_store_identity","invalid_store_name"}
            if len(recent)==2 and all(r["http_status"] in (401,403) or (r["error"] or {}).get("code") in schema_codes for r in recent):
                stopped_error = _error("adapter_stopped", "Repeated authentication or response-contract failures stopped this adapter.", "Inspect the Steam source contract, watch a new successful manual collection, acknowledge it, then enable again.")
                conn.execute("INSERT INTO schedule_adapter_stop(stop_id,plan_hash,source,error) VALUES(%s,%s,%s,%s)",
                             (str(uuid.uuid4()),fingerprint,job["source"],Jsonb(stopped_error)))
        delay = (error.retry_after_seconds if error.retry_after_seconds is not None else
                 random.uniform(0, min(2 ** max(0, current["attempt_count"]-1), settings.http.timeout_seconds)))
        retry = (error.retryable and current["attempt_count"] < settings.http.max_attempts
                 and now + timedelta(seconds=delay+settings.http.timeout_seconds) < min(current["deadline"], until))
        conn.execute("""UPDATE scheduled_job SET state=%s,next_attempt_at=%s,lease_owner=NULL,lease_until=NULL,error=%s
            WHERE job_id=%s""", ("retry_pending" if retry else "failed", now+timedelta(seconds=delay), Jsonb(error.as_dict()), job["job_id"]))


def _dispatch(settings, db, job, fingerprint, epoch, worker, run_id, client, until, cancelled):
    from .sources.http import request_limits
    adapter = REGISTRY[job["source"]]
    attempt_id = None
    try:
        if cancelled():
            raise SourceError("run_cancelled", "The bounded run was cancelled.", "Inspect its retained report before running again.")
        with db.connection() as conn:
            current, now = _fence(conn, job, fingerprint, epoch, worker)
            limit = min(current["deadline"], current["lease_until"], until)
            if now + timedelta(seconds=settings.http.timeout_seconds) >= limit:
                raise SourceError("source_deadline", "Insufficient time remains for an upstream request.", "Wait for a fresh occurrence; expired current counts cannot be backfilled.")
            attempt_id = db.reserve_attempt(run_id, job["app_id"], adapter.SOURCE, adapter.HOST_GROUP,
                                            getattr(settings.quota, f"{adapter.HOST_GROUP}_rolling_24h"),
                                            max(settings.http.min_interval_seconds, 2 if adapter is store else 1),
                                            conn=conn, deadline=limit-timedelta(seconds=settings.http.timeout_seconds))
            conn.execute("INSERT INTO scheduled_attempt(attempt_id,job_id,lease_version) VALUES(%s,%s,%s)",
                         (attempt_id, job["job_id"], job["lease_version"]))
            conn.execute("UPDATE scheduled_job SET attempt_count=attempt_count+1 WHERE job_id=%s", (job["job_id"],))
        response_deadline = min(limit, datetime.now(timezone.utc) + timedelta(seconds=settings.http.timeout_seconds))
        with request_limits(deadline=response_deadline, cancelled=cancelled):
            capture = adapter.fetch(client, job["app_id"], settings.http.max_response_bytes)
        with db.connection() as conn:
            _fence(conn, job, fingerprint, epoch, worker)
            if cancelled() or datetime.now(timezone.utc) >= until:
                raise SourceError("run_cancelled", "The bounded run ended before acceptance.", "Inspect the charged attempt and retained run report.")
            capture_id = db.record_capture(run_id, attempt_id, capture, conn=conn)
            _fence(conn, job, fingerprint, epoch, worker)
            if cancelled() or datetime.now(timezone.utc) >= until:
                raise SourceError("run_cancelled", "The bounded run ended during capture persistence.", "Inspect the charged attempt; no late observation was accepted.")
            conn.execute("UPDATE scheduled_job SET state='succeeded',capture_id=%s,lease_until=NULL,error=NULL WHERE job_id=%s", (capture_id, job["job_id"]))
        return {"job_id": str(job["job_id"]), "app_id": job["app_id"], "source": job["source"], "status": "succeeded", "capture_id": capture_id, "attempt_id": attempt_id}
    except SourceError as error:
        try:
            _finish_job_error(settings, db, job, fingerprint, epoch, worker, error, attempt_id, until)
        except SourceError as fence_error:
            return {"job_id": str(job["job_id"]), "status": "uncertain" if attempt_id else "cancelled", "attempt_id": attempt_id, "error": fence_error.as_dict()}
        return {"job_id": str(job["job_id"]), "status": "failed", "attempt_id": attempt_id, "error": error.as_dict()}


def run(settings, db, *, max_cycles=1, transport=None, cancelled=None) -> dict:
    """Run up to N current cycles, always bounded by the configured duration."""
    if type(max_cycles) is not int or not 1 <= max_cycles <= 288:
        raise DatabaseError("max_cycles must be an integer from 1 through 288. Request a bounded schedule run.")
    value = _admitted(settings)
    fingerprint = value["plan_hash"]
    cancelled = cancelled or (lambda: False)
    worker = str(uuid.uuid4())
    with db.collection_lock():
        with db.connection() as conn:
            initial = _state(conn, fingerprint)
        epoch = initial["epoch"]
        run_id = db.start_run(value["app_ids"], [a.SOURCE for a in _adapters(settings)])
        until = initial["now"] + timedelta(seconds=settings.scheduler.max_run_seconds)
        with db.connection() as conn:
            conn.execute("INSERT INTO scheduled_run(run_id,plan_hash,epoch,requested_cycles,max_run_seconds) VALUES(%s,%s,%s,%s,%s)",
                         (run_id, fingerprint, epoch, max_cycles, settings.scheduler.max_run_seconds))
        report = {"run_id": run_id, "status": "failed", "kind": "scheduled", "plan_hash": fingerprint,
                  "cycles": 0, "outcomes": [], "request_count": 0, "catchup_pending": False}
        seen = set()
        stage = "open_http_client"
        try:
            with httpx.Client(timeout=settings.http.timeout_seconds, transport=transport,
                              headers={"User-Agent": f"Game-Census/{__version__}", "Accept": "application/json"}) as client:
                while True:
                    stage = "check_cancellation"
                    if cancelled():
                        report["cancellation"] = _error("run_cancelled", "The operator cancelled this run.", "Inspect schedule status and the retained attempts.")
                        break
                    stage = "read_schedule_state"
                    with db.connection() as conn:
                        now = _state(conn, fingerprint, epoch)["now"]
                    if now >= until:
                        report["cancellation"] = _error("run_duration_exhausted", "scheduler.max_run_seconds ended this bounded run.", "Inspect remaining jobs and use a fresh bounded run.")
                        break
                    stage = "materialize"
                    current, pending = _materialize(settings, db, fingerprint, epoch)
                    report["catchup_pending"] = pending
                    if current not in seen and len(seen) >= max_cycles:
                        break
                    seen.add(current)
                    report["cycles"] = len(seen)
                    stage = "claim"
                    job = _claim(settings, db, fingerprint, epoch, worker)
                    if job and not job.get("exhausted"):
                        stage = "dispatch"
                        report["outcomes"].append(_dispatch(settings, db, job, fingerprint, epoch, worker, run_id, client, until, cancelled))
                        continue
                    if job:
                        continue
                    stage = "read_outstanding_jobs"
                    with db.connection() as conn:
                        outstanding = conn.execute("""SELECT count(*) AS n FROM scheduled_job WHERE plan_hash=%s
                            AND scheduled_at=%s AND state IN ('pending','leased','retry_pending')""", (fingerprint, current)).fetchone()["n"]
                    if len(seen) >= max_cycles and outstanding == 0:
                        break
                    stage = "wait_next_poll"
                    time.sleep(min(settings.scheduler.poll_seconds, max(0, (until-now).total_seconds())))
        except KeyboardInterrupt:
            report["cancellation"] = _error("run_cancelled", "The operator interrupted this run.", "Inspect charged attempts and job leases before the next bounded run.")
        except SourceError as error:
            report["error"] = error.as_dict()
        except Exception as error:
            report["error"] = _interruption(error, stage)
        if report.get("error"):
            _emit_failure(report, report["error"])
        stage = "account_attempts"
        try:
            with db.connection() as conn:
                counts = conn.execute("""SELECT count(*) AS attempts,count(*) FILTER(WHERE r.status='succeeded') AS succeeded,
                    count(*) FILTER(WHERE r.status='failed') AS failed,count(*) FILTER(WHERE r.status IS NULL) AS uncertain
                    FROM request_attempt a LEFT JOIN request_result r USING(attempt_id) WHERE run_id=%s""", (run_id,)).fetchone()
                jobs = conn.execute("SELECT state,count(*) AS n FROM scheduled_job WHERE plan_hash=%s GROUP BY state", (fingerprint,)).fetchall()
            report["request_count"] = counts["attempts"]
            report["attempts"] = counts
            report["jobs"] = {row["state"]: row["n"] for row in jobs}
            failed = (report.get("error") or report.get("cancellation") or report["catchup_pending"] or counts["uncertain"] or
                      any(o["status"] in ("uncertain", "cancelled") for o in report["outcomes"]))
            # Retried failures remain visible but do not make recovered occurrences fail.
            stage = "account_jobs"
            with db.connection() as conn:
                unsuccessful = conn.execute("""SELECT count(*) AS n FROM scheduled_job WHERE plan_hash=%s
                    AND scheduled_at=ANY(%s::timestamptz[]) AND state<>'succeeded'""", (fingerprint, list(seen))).fetchone()["n"]
            failed = failed or unsuccessful
            report["status"] = "partial" if failed and counts["succeeded"] else ("failed" if failed else "succeeded")
            report["lifecycle_state"] = "cancelled" if report.get("cancellation") else report["status"]
            stage = "persist_report"
            db.finish_run(report)
        except Exception as error:
            _emit_failure(report, _interruption(error, stage), unpersisted=True)
            raise DatabaseError("The scheduler report could not be persisted. Preserve stderr, check database health, then inspect schedule status and retained attempts before retrying.") from None
        return report
