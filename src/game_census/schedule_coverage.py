"""Rebuild schedule denominators from retained control events and observations.

Enable/disable events own scheduled app-time. Jobs are not a denominator: a
stopped worker may never have materialized the occurrences it missed.
"""
from bisect import bisect_left, bisect_right
from collections import defaultdict
from datetime import datetime, timedelta
import math

from .db import DatabaseError, QueryLimitError
from .sources import players


def _timestamp(value):
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("Schedule coverage timestamps must include a timezone.")
    return value


def _intervals(events, start, end):
    active, intervals = None, []
    ordered = sorted(enumerate(events), key=lambda item: (_timestamp(item[1]["recorded_at"]),
                                                         item[1].get("event_id", item[0])))
    for _, event in ordered:
        when = event["recorded_at"]
        if when >= end:
            break
        if active is not None:
            a, b = max(start, active["anchor"]), min(end, when)
            if a < b:
                intervals.append({**active, "from": a, "to": b})
        if event["action"] == "disabled":
            active = None
        elif event["action"] == "enabled":
            details = event.get("details")
            if not isinstance(details, dict):
                raise ValueError("Canonical enable event has invalid cohort or cadence details.")
            apps, cadence = details.get("app_ids"), details.get("cadence_seconds")
            if (not isinstance(apps, list) or not 1 <= len(apps) <= 25
                    or any(type(app) is not int or not 1 <= app <= 4294967295 for app in apps)
                    or len(apps) != len(set(apps)) or type(cadence) is not int or not 300 <= cadence <= 604800
                    or not isinstance(event.get("plan_hash"), str) or not event["plan_hash"]):
                raise ValueError("Canonical enable event has invalid cohort or cadence details.")
            active = {"anchor": when, "app_ids": frozenset(apps), "cadence_seconds": cadence,
                      "plan_hash": event["plan_hash"]}
        else:
            raise ValueError("Schedule coverage requires canonical enabled/disabled control events.")
    if active is not None:
        a = max(start, active["anchor"])
        if a < end:
            intervals.append({**active, "from": a, "to": end})
    return intervals


def _slots_before(instant, anchor, cadence):
    # Integer timedelta division preserves microsecond boundary alignment.
    quotient, remainder = divmod(instant - anchor, timedelta(seconds=cadence))
    return quotient + bool(remainder)


def calculate(events, samples, start, end, freshness_multiplier):
    """Return exact app-time and slot counts for the half-open UTC window.

    Samples may include a carry-in observation before start. Manual samples
    contribute freshness; only samples linked to an accepted scheduled slot
    contribute observed_scheduled_occurrences. Source values are never filled.
    """
    _timestamp(start)
    _timestamp(end)
    if start >= end:
        raise ValueError("Schedule coverage window start must precede its end.")
    if (type(freshness_multiplier) not in (int, float) or not math.isfinite(freshness_multiplier)
            or not 1 <= freshness_multiplier <= 4):
        raise ValueError("metrics.freshness_interval_multiplier must be from 1 through 4.")
    intervals = _intervals(events, start, end)
    by_app = defaultdict(list)
    for sample in samples:
        when = _timestamp(sample["observed_at"])
        if when < end:
            by_app[sample["app_id"]].append(sample)
    times = {}
    for app_id, rows in by_app.items():
        rows.sort(key=lambda sample: sample["observed_at"])
        times[app_id] = [sample["observed_at"] for sample in rows]
    tracked, fresh, expected = 0.0, 0.0, 0
    for interval in intervals:
        a, b, cadence = interval["from"], interval["to"], interval["cadence_seconds"]
        tracked += len(interval["app_ids"]) * (b - a).total_seconds()
        expected += len(interval["app_ids"]) * (
            _slots_before(b, interval["anchor"], cadence) - _slots_before(a, interval["anchor"], cadence))
        for app_id in interval["app_ids"]:
            rows, moments = by_app[app_id], times.get(app_id, [])
            left, right = bisect_left(moments, a), bisect_left(moments, b)
            cursor = a
            for sample in rows[max(0, left - 1):right]:
                span_start = max(a, sample["observed_at"], cursor)
                span_end = min(b, sample["observed_at"] + timedelta(seconds=cadence * freshness_multiplier))
                if span_end > span_start:
                    fresh += (span_end - span_start).total_seconds()
                    cursor = span_end
    anchors = [interval["anchor"] for interval in intervals]
    observed = set()
    for app_id, rows in by_app.items():
        for sample in rows:
            slot = sample.get("scheduled_at")
            if slot is None:
                continue
            _timestamp(slot)
            index = bisect_right(anchors, slot) - 1
            if index < 0:
                continue
            interval = intervals[index]
            cadence = timedelta(seconds=interval["cadence_seconds"])
            if (interval["from"] <= slot < interval["to"] and slot <= sample["observed_at"]
                    and app_id in interval["app_ids"] and sample.get("plan_hash") == interval["plan_hash"]
                    and (slot - interval["anchor"]) % cadence == timedelta(0)):
                observed.add((app_id, slot))
    return {"from": start.isoformat(), "to": end.isoformat(),
            "tracked_app_seconds": tracked, "fresh_app_seconds": fresh,
            "freshness_ratio": fresh / tracked if tracked else None,
            "expected_player_occurrences": expected, "observed_scheduled_occurrences": len(observed),
            "freshness_interval_multiplier": freshness_multiplier,
            "denominator_source": "retained schedule enable/disable events; expired and unmaterialized occurrences remain included",
            "freshness_source": "retained player observations, including valid manual carry-in observations"}


def report(settings, db, hours=24):
    """Read one bounded, repeatable snapshot; no jobs or observations are created."""
    if type(hours) is not int or not 1 <= hours <= settings.web.max_history_days * 24:
        raise QueryLimitError("hours must be an integer from 1 through web.max_history_days × 24. Request a smaller coverage window.")
    limit = settings.web.max_history_samples
    with db.connection() as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        end = conn.execute("SELECT clock_timestamp() AS now").fetchone()["now"]
        start = end - timedelta(hours=hours)
        event_count = conn.execute("""SELECT count(*) AS n FROM schedule_event
            WHERE action IN ('enabled','disabled') AND recorded_at>=%s AND recorded_at<%s""",
                                   (start, end)).fetchone()["n"]
        previous = conn.execute("""SELECT event_id,recorded_at,action,plan_hash,details FROM schedule_event
            WHERE action IN ('enabled','disabled') AND recorded_at<%s
            ORDER BY recorded_at DESC,event_id DESC LIMIT 1""", (start,)).fetchone()
        if event_count + bool(previous) > limit:
            raise QueryLimitError("Schedule events exceed web.max_history_samples. Request a smaller coverage window; no scheduled app-time was removed.")
        events = conn.execute("""SELECT event_id,recorded_at,action,plan_hash,details FROM schedule_event
            WHERE action IN ('enabled','disabled') AND recorded_at>=%s AND recorded_at<%s
            ORDER BY recorded_at,event_id""", (start, end)).fetchall()
        events = ([previous] if previous else []) + events
        try:
            intervals = _intervals(events, start, end)
        except (ValueError, KeyError, TypeError):
            raise DatabaseError("A retained schedule control event cannot define its cohort and cadence. Inspect schedule events before reporting coverage; no denominator was omitted.") from None
        if sum(len(interval["app_ids"]) for interval in intervals) > limit:
            raise QueryLimitError("Schedule cohort intervals exceed web.max_history_samples. Request a smaller coverage window; no scheduled app-time was removed.")
        app_ids = sorted({app for interval in intervals for app in interval["app_ids"]})
        samples = []
        if app_ids:
            count = conn.execute("""SELECT count(*) AS n FROM player_sample
                WHERE app_id=ANY(%s::bigint[]) AND observed_at>=%s AND observed_at<%s""",
                                 (app_ids, start, end)).fetchone()["n"]
            carries = conn.execute("""SELECT p.app_id,p.observed_at,p.capture_id,NULL::timestamptz AS scheduled_at,NULL::text AS plan_hash
                FROM unnest(%s::bigint[]) AS cohort(app_id)
                CROSS JOIN LATERAL (SELECT app_id,observed_at,capture_id FROM player_sample
                    WHERE app_id=cohort.app_id AND observed_at<%s
                    ORDER BY observed_at DESC,capture_id DESC LIMIT 1) p""", (app_ids, start)).fetchall()
            if count + len(carries) > limit:
                raise QueryLimitError("Schedule coverage observations exceed web.max_history_samples. Request a smaller coverage window; no failures or samples were removed.")
            samples = carries + conn.execute("""SELECT p.app_id,p.observed_at,p.capture_id,j.scheduled_at,j.plan_hash
                FROM player_sample p LEFT JOIN scheduled_job j ON j.capture_id=p.capture_id AND j.source=%s
                WHERE p.app_id=ANY(%s::bigint[]) AND p.observed_at>=%s AND p.observed_at<%s
                ORDER BY p.app_id,p.observed_at,p.capture_id""", (players.SOURCE, app_ids, start, end)).fetchall()
    return calculate(events, samples, start, end, settings.metrics.freshness_interval_multiplier)
