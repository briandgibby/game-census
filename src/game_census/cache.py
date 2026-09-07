"""Exact persisted UTC buckets; canonical observations and tracking own all data.

Per-app advisory locks are shared with source-table invalidation triggers. Warm
queries read complete buckets and only the raw partial edges; source revisions
are inspected when a bucket is built, rather than rescanned on every read.
"""
from bisect import bisect_left, bisect_right
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math

from psycopg.types.json import Jsonb

from . import metrics
from .db import DatabaseError, QueryLimitError
from .sources import players

CACHE_VERSION = "exact-bucket-v1"


def lock_app(conn, app_id):
    """Storage acquires this before partition DDL; readers use the same order."""
    conn.execute("SELECT lock_rollup_app(%s)", (app_id,))


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def _floor(value, seconds):
    return datetime.fromtimestamp(math.floor(value.timestamp()/seconds)*seconds, timezone.utc)


def _key(settings):
    return (settings.cache.bucket_seconds, players.SOURCE, players.VERSION,
            metrics.policy_version(settings.metrics.gap_cap_multiplier), CACHE_VERSION)


def reinitialize(conn):
    """Partition cutovers call this after installing the final source tables."""
    from pathlib import Path
    conn.execute((Path(__file__).parent / "migrations/005_rollups.sql").read_text(encoding="utf-8"))
    conn.execute("UPDATE player_rollup_cache SET valid=false WHERE valid")


def _tracking(conn, app_id, settings):
    rows = conn.execute("""SELECT t.started_at,t.interval_seconds,s.ended_at FROM tracking_interval t
        LEFT JOIN tracking_stop s ON s.interval_id=t.id WHERE t.app_id=%s
        ORDER BY t.started_at,t.id LIMIT %s""", (app_id, settings.web.max_history_samples+1)).fetchall()
    if len(rows) > settings.web.max_history_samples:
        raise QueryLimitError("Tracking policies exceed web.max_history_samples. Inspect the retained policy history before rebuilding metrics.")
    return rows


def _read_samples(conn, app_id, start, end, settings, usage, carry=True):
    rows = conn.execute("""SELECT capture_id,observed_at,player_count,parser_version FROM player_sample
        WHERE app_id=%s AND observed_at>=%s AND observed_at<%s ORDER BY observed_at,capture_id LIMIT %s""",
        (app_id,start,end,settings.web.max_history_samples+1)).fetchall()
    previous = (conn.execute("""SELECT capture_id,observed_at,player_count,parser_version FROM player_sample
        WHERE app_id=%s AND observed_at<%s ORDER BY observed_at DESC,capture_id DESC LIMIT 1""",
        (app_id,start)).fetchone() if carry else None)
    for row in ([previous] if previous else []) + rows:
        usage["sample_ids"].add(str(row["capture_id"]))
    if len(usage["sample_ids"]) > settings.web.max_history_samples:
        raise QueryLimitError("History and its comparison window exceed web.max_history_samples. Request a smaller hours window or rebuild bounded cache ranges; no source observations were truncated.")
    return rows, previous


def _geometry(rows, gaps):
    if not rows:
        return []
    times = [row["observed_at"] for row in rows]
    indices = {0, len(rows)-1,
               max(range(len(rows)), key=lambda index: rows[index]["player_count"]),
               min(range(len(rows)), key=lambda index: rows[index]["player_count"])}
    for gap in gaps:
        before = bisect_right(times, datetime.fromisoformat(gap["from"]))-1
        after = bisect_left(times, datetime.fromisoformat(gap["to"]))
        if before >= 0:
            indices.add(before)
        if after < len(rows):
            indices.add(after)
    return [metrics._point(rows[index]) for index in sorted(indices)]


def _raw_part(conn, app_id, start, end, tracking, settings, usage):
    rows, previous = _read_samples(conn,app_id,start,end,settings,usage)
    inputs = ([previous] if previous else []) + rows
    calculation = metrics.calculate(inputs,start,end,tracking,settings.metrics.gap_cap_multiplier)
    return {"from": start.isoformat(), "to": end.isoformat(), "calculation": calculation,
            "geometry": _geometry(rows,calculation["gaps"]),
            "first": metrics._point(rows[0]) if rows else None,
            "last": metrics._point(rows[-1]) if rows else None,
            "minimum": metrics._point(min(rows,key=lambda row: row["player_count"],default=None)),
            "maximum": metrics._point(max(rows,key=lambda row: row["player_count"],default=None)),
            "input_revision": _hash({"samples": inputs, "tracking": tracking, "source": players.SOURCE,
                                      "source_version": players.VERSION, "policy": _key(settings),
                                      "from": start, "to": end})}


def _store(conn, app_id, start, part, settings):
    conn.execute("""INSERT INTO player_rollup_cache(app_id,bucket_start,bucket_seconds,source,source_version,
        metric_policy_version,cache_version,input_revision,payload,payload_checksum)
        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT(app_id,bucket_start,bucket_seconds,source,source_version,metric_policy_version,cache_version)
        DO UPDATE SET input_revision=EXCLUDED.input_revision,payload=EXCLUDED.payload,
            payload_checksum=EXCLUDED.payload_checksum,valid=true,built_at=clock_timestamp()""",
        (app_id,start,*_key(settings),part["input_revision"],Jsonb(part),_hash(part)))


def _parts(conn, app_id, start, end, tracking, settings, usage, force=False):
    seconds = settings.cache.bucket_seconds
    count = math.ceil((end-_floor(start,seconds)).total_seconds()/seconds)
    usage["buckets_requested"] += count
    if usage["buckets_requested"] > settings.cache.rebuild_max_buckets:
        raise QueryLimitError("History exceeds cache.rebuild_max_buckets. Request a smaller hours window or increase that bounded setting.")
    existing = {} if force else {row["bucket_start"]: row for row in conn.execute("""SELECT bucket_start,
        input_revision,payload,payload_checksum FROM player_rollup_cache WHERE app_id=%s AND bucket_seconds=%s
        AND source=%s AND source_version=%s AND metric_policy_version=%s AND cache_version=%s AND valid
        AND bucket_start>=%s AND bucket_start<%s""", (app_id,*_key(settings),start,end)).fetchall()}
    cursor, result = start, []
    while cursor < end:
        anchor = _floor(cursor,seconds)
        boundary = min(end,anchor+timedelta(seconds=seconds))
        complete = cursor == anchor and boundary == anchor+timedelta(seconds=seconds)
        cached = existing.get(anchor) if complete else None
        if cached:
            part = cached["payload"]
            if (_hash(part) != cached["payload_checksum"] or part.get("input_revision") != cached["input_revision"]
                    or part.get("from") != cursor.isoformat() or part.get("to") != boundary.isoformat()):
                raise DatabaseError("A persisted rollup failed its integrity check. Run aggregate rebuild for this app and range; canonical observations remain retained.")
            usage["reused_buckets"] += 1
        else:
            part = _raw_part(conn,app_id,cursor,boundary,tracking,settings,usage)
            if complete:
                _store(conn,app_id,anchor,part,settings)
                usage["rebuilt_buckets"] += 1
            else:
                usage["raw_edge_pieces"] += 1
        result.append(part)
        cursor = boundary
    return result


def _usage():
    return {"sample_ids": set(), "buckets_requested": 0, "rebuilt_buckets": 0,
            "reused_buckets": 0, "raw_edge_pieces": 0}


def _calculated(parts, start, end, settings):
    return metrics.combine([part["calculation"] for part in parts],start,end,settings.metrics.gap_cap_multiplier)


def _points(parts):
    return sorted({(point["observed_at"],point["player_count"]): point
                   for part in parts for point in part["geometry"]}.values(),
                  key=lambda point: (point["observed_at"],point["player_count"]))


def _public_rollup(parts, start, end, settings):
    calculation = _calculated(parts,start,end,settings)
    points = _points(parts)
    metric, coverage = calculation["metrics"], calculation["coverage"]
    return {"from": start.isoformat(), "to": end.isoformat(),
            "first": points[0] if points else None, "last": points[-1] if points else None,
            "minimum": min(points,key=lambda row: row["player_count"],default=None),
            "maximum": max(points,key=lambda row: row["player_count"],default=None),
            "sample_count": coverage["sample_count"], "integral_player_seconds": metric["integral_player_seconds"],
            "covered_seconds": coverage["covered_seconds"], "requested_seconds": coverage["requested_seconds"],
            "average_observed_ccu": metric["average_observed_ccu"], "gaps": calculation["gaps"]}


def _display(parts, start, end, settings):
    """Coarsen exact bucket states; never calculate coverage from reduced points."""
    points = _points(parts)
    times = [point["observed_at"] for point in points]
    calculation = _calculated(parts,start,end,settings)
    mandatory = {}

    def retain(target, point):
        if point is not None:
            target[(point["observed_at"],point["player_count"])] = point

    if points:
        retain(mandatory,points[0])
        retain(mandatory,points[-1])
    for gap in calculation["gaps"]:
        before, after = bisect_right(times,gap["from"])-1, bisect_left(times,gap["to"])
        if before >= 0:
            retain(mandatory,points[before])
        if after < len(points):
            retain(mandatory,points[after])
    if len(mandatory) > settings.web.max_points:
        raise QueryLimitError("History gap boundaries exceed web.max_points. Request a smaller hours window or raise web.max_points; no gaps were removed.")
    seconds = settings.cache.bucket_seconds
    while True:
        groups = []
        for part in parts:
            anchor = _floor(datetime.fromisoformat(part["from"]),seconds)
            if not groups or groups[-1][0] != anchor:
                groups.append((anchor,[]))
            groups[-1][1].append(part)
        selected, rollups = dict(mandatory), []
        for _, group in groups:
            bucket = _public_rollup(group,datetime.fromisoformat(group[0]["from"]),
                                    datetime.fromisoformat(group[-1]["to"]),settings)
            rollups.append(bucket)
            for key in ("first","last","minimum","maximum"):
                retain(selected,bucket[key])
        if len(selected) <= settings.web.max_points:
            return {"points": sorted(selected.values(),key=lambda point: (point["observed_at"],point["player_count"])),
                    "resolution": "bucketed", "bucket_seconds": seconds, "rollups": rollups}
        if seconds >= (end-start).total_seconds()*2:
            raise QueryLimitError("History extrema and gap boundaries exceed web.max_points. Request a smaller hours window; no observations were silently truncated.")
        seconds *= 2


def history(db, settings, app_id, start, end, resolution, diagnostics=None):
    usage = _usage()
    with db.connection() as conn:
        # BEFORE source-write triggers take the same lock. READ COMMITTED after
        # lock acquisition sees any writer which completed while we waited.
        lock_app(conn,app_id)
        if conn.execute("SELECT app_id FROM app WHERE app_id=%s", (app_id,)).fetchone() is None:
            return None
        n = conn.execute("""SELECT count(*) AS n FROM (SELECT 1 FROM player_sample WHERE app_id=%s
            AND observed_at>=%s AND observed_at<%s LIMIT %s) bounded""",
            (app_id,start,end,settings.web.max_points+1)).fetchone()["n"]
        if resolution == "raw" and n > settings.web.max_points:
            raise QueryLimitError("History exceeds web.max_points. Request a smaller hours window or resolution=auto; no observations were truncated.")
        tracking = _tracking(conn,app_id,settings)
        comparison_start = start-(end-start)
        older_parts = _parts(conn,app_id,comparison_start,start,tracking,settings,usage)
        newer_parts = _parts(conn,app_id,start,end,tracking,settings,usage)
        older, newer = (_calculated(older_parts,comparison_start,start,settings),
                        _calculated(newer_parts,start,end,settings))
        if resolution == "raw" or n <= settings.web.max_points:
            rows, _ = _read_samples(conn,app_id,start,end,settings,usage,carry=False)
            display = {"points": [metrics._point(row) for row in rows], "resolution": "raw", "bucket_seconds": None, "rollups": []}
        else:
            display = _display(newer_parts,start,end,settings)
    if diagnostics is not None:
        diagnostics.update({key:value for key,value in usage.items() if key != "sample_ids"})
        diagnostics.update(input_samples_loaded=len(usage["sample_ids"]), bounded_count_limit=settings.web.max_points+1)
    return {"app_id": app_id, "from": start.isoformat(), "to": end.isoformat(),
            "source": players.SOURCE, "source_version": players.VERSION, **newer, **display,
            "growth": metrics.growth_from_calculated(older,newer,start,end,settings.metrics.gap_cap_multiplier,settings.metrics.min_coverage_ratio)}


def rebuild(db, settings, app_id=None, hours=None):
    """Recompute complete buckets from owners; never replace canonical records."""
    if hours is not None and (type(hours) is not int or not 1 <= hours <= settings.web.max_history_days*24):
        raise QueryLimitError("hours must be from 1 through web.max_history_days × 24. Request a smaller cache rebuild range.")
    with db.connection() as conn:
        apps = conn.execute("SELECT app_id FROM app WHERE (%s::bigint IS NULL OR app_id=%s) ORDER BY app_id", (app_id,app_id)).fetchall()
    if app_id is not None and not apps:
        raise DatabaseError("app_id has not been enrolled. Inspect apps before rebuilding its rollups.")
    reports = []
    for app in apps:
        usage = _usage()
        with db.connection() as conn:
            lock_app(conn,app["app_id"])
            now = conn.execute("SELECT clock_timestamp() AS now").fetchone()["now"]
            end = _floor(now,settings.cache.bucket_seconds)
            oldest = conn.execute("SELECT min(observed_at) AS at FROM player_sample WHERE app_id=%s", (app["app_id"],)).fetchone()["at"]
            start = (_floor(now-timedelta(hours=hours),settings.cache.bucket_seconds) if hours is not None
                     else _floor(oldest,settings.cache.bucket_seconds) if oldest else end)
            if start < end:
                _parts(conn,app["app_id"],start,end,_tracking(conn,app["app_id"],settings),settings,usage,force=True)
        reports.append({"app_id":app["app_id"],"from":start.isoformat(),"to":end.isoformat(),
                        "rebuilt_buckets":usage["rebuilt_buckets"],"input_samples_read":len(usage["sample_ids"])})
    return {"status":"succeeded","apps":reports,"rebuilt_buckets":sum(row["rebuilt_buckets"] for row in reports),
            "reused_buckets":0,"input_samples_read":sum(row["input_samples_read"] for row in reports),
            "canonical_history_changed":False,"cache_version":CACHE_VERSION}
