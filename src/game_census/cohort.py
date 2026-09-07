"""Explicit, quota-bound cohort adoption over inspectable stored observations."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from psycopg.types.json import Jsonb

from .db import DatabaseError


def policy(settings):
    pinned = settings._cohort_pinned_app_ids or settings.tracking.app_ids
    value = {"version": 1, "pinned_app_ids": sorted(pinned), "cadence_seconds": settings.tracking.interval_seconds,
             **settings.cohort.model_dump()}
    if settings.cohort.enabled and len(pinned) + settings.cohort.exploration_slots > settings.cohort.max_apps:
        raise DatabaseError("cohort.max_apps must cover tracking.app_ids plus cohort.exploration_slots. Increase the bound or reduce the pinned configuration.")
    return value


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _utc(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("UTC offset required")
    return value.astimezone(timezone.utc)


def _members(rows):
    try:
        if not isinstance(rows, list) or not 1 <= len(rows) <= 25:
            raise ValueError()
        result = []
        for row in rows:
            app_id = row["app_id"]
            if type(app_id) is not int or not 1 <= app_id <= 4294967295 or row["role"] not in ("pinned", "active", "exploration"):
                raise ValueError()
            result.append({"app_id": app_id, "role": row["role"], "since": _utc(row["since"]).isoformat()})
        if len({row["app_id"] for row in result}) != len(result):
            raise ValueError()
        return result
    except (KeyError, TypeError, ValueError, OverflowError):
        raise DatabaseError("A cohort adoption has invalid membership data. Preserve the record and inspect cohort status or a scratch restore.") from None


def select(settings, previous, candidates, observations, now):
    """Pure selection: prior adoption + bounded IDs + dated counts -> proposal.

    Only repeated, fresh current-player observations can promote or demote.
    Catalog order chooses exploration candidates; a missing count is never zero.
    """
    now = _utc(now)
    rules = policy(settings)
    cfg = settings.cohort
    if len(candidates) > cfg.candidate_limit or len(set(candidates)) != len(candidates):
        raise DatabaseError("Cohort candidates exceed cohort.candidate_limit or contain duplicates. Inspect the bounded catalog query.")
    if any(type(app_id) is not int or not 1 <= app_id <= 4294967295 for app_id in candidates):
        raise DatabaseError("Cohort candidates contain an invalid Steam app ID. Inspect the catalog projection.")
    old = _members(previous["members"]) if previous else [
        {"app_id": app_id, "role": "pinned", "since": now.isoformat()} for app_id in rules["pinned_app_ids"]]
    if any(_utc(row["since"]) > now for row in old):
        raise DatabaseError("A cohort membership starts in the future. Inspect the adoption record and database clock.")
    old_by_id = {row["app_id"]: row for row in old}
    if not cfg.enabled:
        members = [{"app_id": app_id, "role": "pinned", "since": old_by_id.get(app_id, {}).get("since", now.isoformat())}
                   for app_id in rules["pinned_app_ids"]]
        removed = sorted(old_by_id.keys() - set(rules["pinned_app_ids"]))
        introduced = set(rules["pinned_app_ids"]) - old_by_id.keys()
        errors = []
        if max(len(removed), len(introduced)) > cfg.max_replacements:
            errors.append("Disabling exceeds cohort.max_replacements. Explicitly raise the per-run bound before reconciling the static cohort.")
        if previous and (now-_utc(previous["recorded_at"])).total_seconds() < cfg.reconcile_interval_seconds:
            errors.append("cohort.reconcile_interval_seconds has not elapsed. Wait for the next reconciliation time.")
        return {"policy": rules, "policy_hash": fingerprint(rules), "members": members,
                "app_ids": rules["pinned_app_ids"], "changes": [
                    {"app_id": app_id, "action": "remove", "reason": "cohort policy disabled"} for app_id in removed],
                "deferred": [], "candidate_count": 0, "exploration_cursor": previous["exploration_cursor"] if previous else 0,
                "exploration_slots_filled": 0, "exploration_slots_reserved": 0, "admitted": not errors, "errors": errors}
    known = set(candidates) | old_by_id.keys() | set(rules["pinned_app_ids"])
    if sum(len(rows) for rows in observations.values()) > cfg.max_evidence_samples:
        raise DatabaseError("Cohort observations exceed cohort.max_evidence_samples. Use a smaller candidate page or a measured evidence bound.")
    fresh = {}
    cutoff = now - timedelta(seconds=cfg.evidence_max_age_seconds)
    try:
        for app_id, rows in observations.items():
            if app_id not in known:
                raise ValueError()
            for row in rows:
                if type(row["player_count"]) is not int or row["player_count"] < 0:
                    raise ValueError()
                if _utc(row["observed_at"]) > now:
                    raise ValueError()
            # One received time contributes at most one corroborating sample.
            distinct = {}
            member_cutoff = max(cutoff, _utc(old_by_id[app_id]["since"])) if app_id in old_by_id else cutoff
            for row in sorted(rows, key=lambda row: _utc(row["observed_at"])):
                at = _utc(row["observed_at"])
                if at >= member_cutoff:
                    if at in distinct and distinct[at] != row["player_count"]:
                        raise ValueError()
                    distinct[at] = row["player_count"]
            fresh[app_id] = list(distinct.values())[-cfg.min_samples:]
    except (KeyError, TypeError, ValueError, OverflowError):
        raise DatabaseError("Cohort player evidence is malformed, conflicting or outside its declared time/ID scope. Inspect retained player captures.") from None

    def repeated(app_id, predicate):
        counts = fresh.get(app_id, [])
        return len(counts) == cfg.min_samples and all(predicate(count) for count in counts)

    def mature(row):
        return (now-_utc(row["since"])).total_seconds() >= cfg.min_residency_seconds

    def member(app_id, role):
        old_row = old_by_id.get(app_id)
        since = old_row["since"] if old_row and old_row["role"] == role else now.isoformat()
        return {"app_id": app_id, "role": role, "since": since}

    pinned = set(rules["pinned_app_ids"])
    active = [member(row["app_id"], "active") for row in old if row["app_id"] not in pinned and row["role"] in ("pinned", "active")]
    explorers = [row for row in old if row["app_id"] not in pinned and row["role"] == "exploration"]
    active_slots = cfg.max_apps - len(pinned) - cfg.exploration_slots
    errors, changes, deferred = [], [], []
    removals = set()
    if len(active) > active_slots:
        # A reduced configured limit is explicit scope reduction, not a zero-count inference.
        overflow = sorted(active, key=lambda row: (row["role"] != "pinned", row["app_id"]))[active_slots:]
        for row in overflow:
            removals.add(row["app_id"])
            changes.append({"app_id": row["app_id"], "action": "remove", "reason": "configured active capacity reduced"})
        active = [row for row in active if row["app_id"] not in removals]
    for row in active[:]:
        if mature(row) and repeated(row["app_id"], lambda count: count < cfg.demote_below):
            if len(removals) >= cfg.max_replacements:
                deferred.append({"app_id": row["app_id"], "reason": "demotion waits for the next bounded reconciliation"})
                continue
            removals.add(row["app_id"])
            changes.append({"app_id": row["app_id"], "action": "remove", "reason": "repeated fresh counts below demotion threshold"})
            active.remove(row)
    promote = sorted((row for row in explorers if mature(row) and repeated(row["app_id"], lambda count: count > cfg.promote_above)),
                     key=lambda row: (-min(fresh[row["app_id"]]), row["app_id"]))
    promoted = set()
    for row in promote[:max(0, active_slots-len(active))]:
        promoted.add(row["app_id"])
        active.append(member(row["app_id"], "active"))
        changes.append({"app_id": row["app_id"], "action": "promote", "reason": "repeated fresh counts above promotion threshold"})
    remaining_explorers = [row for row in explorers if row["app_id"] not in promoted]
    for row in remaining_explorers[cfg.exploration_slots:]:
        removals.add(row["app_id"])
        changes.append({"app_id": row["app_id"], "action": "remove", "reason": "configured exploration capacity reduced"})
    remaining_explorers = remaining_explorers[:cfg.exploration_slots]
    keep_explorers = [row for row in remaining_explorers if not mature(row)]
    expired = [row for row in remaining_explorers if mature(row)]
    selected_ids = pinned | {row["app_id"] for row in active + keep_explorers}
    available = [app_id for app_id in candidates if app_id not in selected_ids and app_id not in removals
                 and app_id not in {row["app_id"] for row in expired}]
    new_explorers = []
    def explore(app_id):
        new_explorers.append(member(app_id, "exploration"))
        changes.append({"app_id": app_id, "action": "explore", "reason": "reserved exploration slot"})
    for row in expired:
        if available and len(removals) < cfg.max_replacements and len(new_explorers) < cfg.max_replacements:
            removals.add(row["app_id"])
            changes.append({"app_id": row["app_id"], "action": "remove", "reason": "exploration residence completed"})
            explore(available.pop(0))
        else:
            keep_explorers.append(row)
    for app_id in available[:max(0, min(cfg.exploration_slots-len(keep_explorers)-len(new_explorers),
                                     cfg.max_replacements-len(new_explorers)))]:
        explore(app_id)
    if len(keep_explorers) > cfg.exploration_slots:
        for row in keep_explorers[cfg.exploration_slots:]:
            removals.add(row["app_id"])
            changes.append({"app_id": row["app_id"], "action": "remove", "reason": "configured exploration capacity reduced"})
        keep_explorers = keep_explorers[:cfg.exploration_slots]
    members = sorted([member(app_id, "pinned") for app_id in pinned] + active + keep_explorers + new_explorers,
                     key=lambda row: row["app_id"])
    introduced = {row["app_id"] for row in members} - old_by_id.keys()
    changed_slots = max(len(introduced), len(removals))
    if changed_slots > cfg.max_replacements:
        errors.append("Proposal exceeds cohort.max_replacements. Inspect the changes and explicitly raise the per-run bound or adjust the policy.")
    if previous and (now-_utc(previous["recorded_at"])).total_seconds() < cfg.reconcile_interval_seconds:
        errors.append("cohort.reconcile_interval_seconds has not elapsed. Inspect cohort status and wait for the next reconciliation time.")
    cursor = previous.get("exploration_cursor", 0) if previous else 0
    if new_explorers:
        cursor = new_explorers[-1]["app_id"]
    elif candidates and all(app_id in old_by_id or app_id in pinned for app_id in candidates):
        cursor = candidates[-1]
    return {"policy": rules, "policy_hash": fingerprint(rules), "members": _members(members),
            "app_ids": [row["app_id"] for row in members], "changes": changes, "deferred": deferred,
            "candidate_count": len(candidates), "exploration_cursor": cursor,
            "exploration_slots_filled": sum(row["role"] == "exploration" for row in members),
            "exploration_slots_reserved": cfg.exploration_slots, "admitted": not errors, "errors": errors}


def capacity_reserves(settings, app_count):
    """Reserve extra watched replacement runs and separately configured discovery attempts."""
    if not settings.cohort.enabled:
        return {}
    manual_runs = math.ceil(86400/settings.cohort.reconcile_interval_seconds) + 1
    manual_attempts = manual_runs * app_count * settings.http.max_attempts
    return {"webapi": settings.cohort.discovery_webapi_reserve + manual_attempts,
            "store": settings.cohort.discovery_store_reserve + (manual_attempts if settings.sources.store_metadata_enabled else 0)}


def _event(row):
    if row is None:
        return None
    value = {key: row[key] for key in ("previous_id", "policy_hash", "policy", "members", "exploration_cursor", "basis", "changes")}
    value["recorded_at"] = _utc(row["recorded_at"]).isoformat()
    _members(value["members"])
    if row["policy_hash"] != fingerprint(row["policy"]) or row["event_hash"] != fingerprint(value):
        raise DatabaseError("A cohort adoption differs from its recorded checksum or policy. Preserve the record and inspect a scratch restore.")
    return {"id": row["id"], **value, "event_hash": row["event_hash"]}


def latest(db):
    return _event(db.latest_cohort())


def effective(settings, db, *, check_disabled=False):
    """Resolve runtime membership without mutating the operator's configuration."""
    if not settings.cohort.enabled and not check_disabled:
        return settings
    rules = policy(settings)
    event = latest(db)
    if not settings.cohort.enabled:
        if event is not None and event["policy"]["enabled"]:
            raise DatabaseError("cohort.enabled changed after adoption. Disable scheduling and run cohort reconcile --once --apply to close exploration tracking before collecting.")
        return settings
    if event is not None and event["policy_hash"] != fingerprint(rules):
        raise DatabaseError("The cohort policy changed after adoption. Inspect cohort plan and explicitly reconcile before collecting or enabling a schedule.")
    resolved = settings.model_copy(deep=True)
    resolved._cohort_pinned_app_ids = rules["pinned_app_ids"]
    resolved.tracking.app_ids = [row["app_id"] for row in event["members"]] if event else rules["pinned_app_ids"]
    return resolved


def public_status(settings, db):
    """Safe stored-read membership and adoption state; no credentials or writes."""
    event = latest(db)
    rules = policy(settings)
    matches = event is None or (event["policy_hash"] == fingerprint(rules) if settings.cohort.enabled
                               else not event["policy"]["enabled"])
    members = event["members"] if event and (settings.cohort.enabled or not matches) else [
        {"app_id": app_id, "role": "pinned", "since": None} for app_id in rules["pinned_app_ids"]]
    return {"enabled": settings.cohort.enabled, "policy_matches": matches,
            "state": "policy_changed" if not matches else "adopted" if event and settings.cohort.enabled else "configured",
            "event_id": event["id"] if event else None, "adopted_at": event["recorded_at"] if event else None,
            "members": members, "exploration_slots_reserved": settings.cohort.exploration_slots if settings.cohort.enabled else 0}


def _inputs(settings, db):
    """One stored snapshot, bounded catalog page and bounded recent sample set."""
    rules = policy(settings)
    with db.connection() as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        now = _now(conn)
        previous = _event(conn.execute("SELECT * FROM cohort_event ORDER BY id DESC LIMIT 1").fetchone())
        if not settings.cohort.enabled:
            enabled = conn.execute("SELECT enabled FROM schedule_state WHERE singleton").fetchone()["enabled"]
            return previous, [], {}, now, enabled
        if previous is None:
            baseline = conn.execute("""WITH current AS (
                SELECT DISTINCT ON(app_id) id,app_id,started_at FROM tracking_interval
                ORDER BY app_id,started_at DESC,id DESC
            ) SELECT t.* FROM current t LEFT JOIN tracking_stop s ON s.interval_id=t.id
                WHERE s.interval_id IS NULL ORDER BY t.app_id LIMIT 26""").fetchall()
            if len(baseline) > 25:
                raise DatabaseError("Initial tracking exceeds the 25-member cohort bound. Preserve the interval history and inspect the preexisting enrollment before adopting a cohort.")
            if baseline:
                previous = {"id": None, "recorded_at": (now-timedelta(seconds=settings.cohort.reconcile_interval_seconds)).isoformat(),
                    "exploration_cursor": 0, "initial_tracking": [{"app_id": row["app_id"], "interval_id": row["id"]} for row in baseline],
                    "members": [{"app_id": row["app_id"], "role": "pinned" if row["app_id"] in rules["pinned_app_ids"] else "active",
                                 "since": _utc(row["started_at"]).isoformat()} for row in baseline]}
        cursor = previous["exploration_cursor"] if previous else 0
        rows = conn.execute("""SELECT DISTINCT app_id FROM catalog_entry WHERE app_id>%s
            ORDER BY app_id LIMIT %s""", (cursor, settings.cohort.candidate_limit)).fetchall()
        if len(rows) < settings.cohort.candidate_limit:
            rows += conn.execute("""SELECT DISTINCT app_id FROM catalog_entry WHERE app_id<=%s
                ORDER BY app_id LIMIT %s""", (cursor, settings.cohort.candidate_limit-len(rows))).fetchall()
        candidates = [row["app_id"] for row in rows]
        prior_ids = {row["app_id"] for row in previous["members"]} if previous else set()
        ids = sorted(set(candidates) | set(rules["pinned_app_ids"]) | prior_ids)
        samples = conn.execute("""SELECT a.app_id,p.capture_id,p.observed_at,p.player_count
            FROM unnest(%s::bigint[]) a(app_id) CROSS JOIN LATERAL (
                SELECT capture_id,observed_at,player_count FROM player_sample WHERE app_id=a.app_id
                AND observed_at>=%s AND observed_at<=%s ORDER BY observed_at DESC,capture_id DESC LIMIT %s
            ) p ORDER BY a.app_id,p.observed_at,p.capture_id LIMIT %s""",
            (ids, now-timedelta(seconds=settings.cohort.evidence_max_age_seconds), now,
             settings.cohort.min_samples, settings.cohort.max_evidence_samples+1)).fetchall()
        if len(samples) > settings.cohort.max_evidence_samples:
            raise DatabaseError("Cohort evidence exceeds cohort.max_evidence_samples. Reduce cohort.candidate_limit or inspect a measured evidence bound.")
        enabled = conn.execute("SELECT enabled FROM schedule_state WHERE singleton").fetchone()["enabled"]
    observations = {}
    for row in samples:
        observations.setdefault(row["app_id"], []).append(row)
    return previous, candidates, observations, now, enabled


def _now(conn):
    return conn.execute("SELECT clock_timestamp() AS now").fetchone()["now"]


def plan(settings, db):
    from .scheduler import plan as schedule_plan
    previous, candidates, observations, now, enabled = _inputs(settings, db)
    result = select(settings, previous, candidates, observations, now)
    resolved = settings.model_copy(deep=True)
    resolved._cohort_pinned_app_ids = result["policy"]["pinned_app_ids"]
    resolved.tracking.app_ids = result["app_ids"]
    capacity = schedule_plan(resolved)
    errors = [*result["errors"], *capacity["errors"]]
    if enabled:
        errors.append("Scheduling is enabled. Run schedule disable before adopting a new cohort, then watch and acknowledge its manual run before enabling it.")
    basis = {"initial_tracking": previous.get("initial_tracking", []) if previous else [],
             "candidates": candidates, "player_captures": {str(app_id): [str(row["capture_id"]) for row in rows]
             for app_id, rows in observations.items()}}
    return {**result, "operation": "cohort_reconcile", "admitted": not errors, "errors": errors,
            "previous_id": previous["id"] if previous else None, "recorded_at": now.isoformat(), "basis": basis,
            "capacity": capacity, "steam_requests": 0, "scheduler_enabled": enabled,
            "writes": "one cohort adoption and new tracking start/end events; no source collection or history deletion"}


def adopt(settings, db, *, expected_previous_id=None):
    """Adopt under the same lock as manual/scheduled collectors; never enable work."""
    with db.collection_lock():
        proposal = plan(settings, db)
        if not proposal["admitted"]:
            raise DatabaseError("Cohort proposal is not admitted: " + "; ".join(proposal["errors"]))
        if expected_previous_id is not None and proposal["previous_id"] != expected_previous_id:
            raise DatabaseError("The cohort changed after preview. Inspect cohort plan again before adopting it.")
        with db.connection() as conn:
            conn.execute("SELECT * FROM schedule_state WHERE singleton FOR UPDATE")
            if conn.execute("SELECT enabled FROM schedule_state WHERE singleton").fetchone()["enabled"]:
                raise DatabaseError("Scheduling became enabled. Disable it before adopting a cohort.")
            previous = _event(conn.execute("SELECT * FROM cohort_event ORDER BY id DESC LIMIT 1").fetchone())
            if (previous["id"] if previous else None) != proposal["previous_id"]:
                raise DatabaseError("The cohort changed during adoption. Inspect cohort plan and retry.")
            event = {key: proposal[key] for key in ("previous_id", "recorded_at", "policy_hash", "policy", "members", "exploration_cursor", "basis", "changes")}
            digest = fingerprint(event)
            event_id = conn.execute("""INSERT INTO cohort_event(previous_id,recorded_at,policy_hash,policy,members,exploration_cursor,basis,changes,event_hash)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""", (event["previous_id"], _utc(event["recorded_at"]),
                event["policy_hash"], Jsonb(event["policy"]), Jsonb(event["members"]), event["exploration_cursor"],
                Jsonb(event["basis"]), Jsonb(event["changes"]), digest)).fetchone()["id"]
            at = _utc(event["recorded_at"])
            old_ids = ({row["app_id"] for row in previous["members"]} if previous else
                       {row["app_id"] for row in event["basis"]["initial_tracking"]} | set(event["policy"]["pinned_app_ids"]))
            selected = set(proposal["app_ids"])
            for app_id in sorted(old_ids | selected):
                if app_id in selected:
                    conn.execute("INSERT INTO app(app_id) VALUES (%s) ON CONFLICT DO NOTHING", (app_id,))
                interval = conn.execute("""SELECT t.id,t.started_at,t.interval_seconds,s.ended_at FROM tracking_interval t
                    LEFT JOIN tracking_stop s ON s.interval_id=t.id WHERE app_id=%s ORDER BY started_at DESC,t.id DESC LIMIT 1""", (app_id,)).fetchone()
                if app_id not in selected and interval and interval["ended_at"] is None:
                    if interval["started_at"] > at:
                        raise DatabaseError("Tracking starts after this adoption time. Inspect the database clock and interval history.")
                    conn.execute("INSERT INTO tracking_stop(interval_id,ended_at,cohort_event_id) VALUES(%s,%s,%s)", (interval["id"], at, event_id))
                if app_id in selected and (interval is None or interval["ended_at"] is not None or interval["interval_seconds"] != settings.tracking.interval_seconds):
                    conn.execute("INSERT INTO tracking_interval(app_id,started_at,interval_seconds) VALUES(%s,%s,%s)", (app_id, at, settings.tracking.interval_seconds))
    return {"status": "adopted", "event_id": event_id, "app_ids": proposal["app_ids"],
            "changes": proposal["changes"], "plan_hash": proposal["capacity"]["plan_hash"], "scheduler_enabled": False, "steam_requests": 0}
