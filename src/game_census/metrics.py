"""Coverage and capped time-weighted averages calculated from retained observations."""
from bisect import bisect_left, bisect_right
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math

ESTIMATOR_VERSION = "capped-step-v1"


def policy_version(gap_cap_multiplier: float, minimum_coverage_ratio: float | None = None) -> str:
    """Identify exactly the settings which affect this derived metric."""
    policy = {"estimator": ESTIMATOR_VERSION, "gap_cap_multiplier": float(gap_cap_multiplier)}
    if minimum_coverage_ratio is not None:
        policy["minimum_coverage_ratio"] = float(minimum_coverage_ratio)
    encoded = json.dumps(policy, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _ordered(samples: list[dict]) -> list[dict]:
    return sorted(samples, key=lambda row: (row["observed_at"], str(row.get("capture_id", ""))))


def _point(sample: dict | None) -> dict | None:
    return None if sample is None else {"observed_at": sample["observed_at"].isoformat(),
                                       "player_count": sample["player_count"]}


def calculate(samples: list[dict], start: datetime, end: datetime, tracking: list[dict],
              gap_cap_multiplier: float = 2) -> dict:
    if start >= end:
        raise ValueError("history window start must precede its end")
    ordered = _ordered(samples)
    policies = sorted(tracking, key=lambda row: row["started_at"])
    requested = (end - start).total_seconds()
    in_window = [s for s in ordered if start <= s["observed_at"] < end]
    tracked_seconds, expected = 0.0, 0
    for index, policy in enumerate(policies):
        a = max(start, policy["started_at"])
        b = min(end, policies[index + 1]["started_at"] if index + 1 < len(policies) else end,
                policy.get("ended_at") or end)
        seconds = max(0.0, (b - a).total_seconds())
        tracked_seconds += seconds
        if seconds:
            # Count actual cadence-aligned slots, rather than rounding the
            # clipped duration up and inventing an occurrence near an edge.
            interval = policy["interval_seconds"]
            expected += max(0, math.ceil((b-policy["started_at"]).total_seconds()/interval)
                            - math.ceil((a-policy["started_at"]).total_seconds()/interval))
    covered, integral = 0.0, 0.0
    spans = []
    for index, sample in enumerate(ordered):
        observed = sample["observed_at"]
        applicable = [p for p in policies if p["started_at"] <= observed]
        if not applicable:
            continue
        current_policy = applicable[-1]
        if current_policy.get("ended_at") is not None and observed >= current_policy["ended_at"]:
            continue
        cadence = current_policy["interval_seconds"]
        a = max(start, observed)
        b = min(end, observed + timedelta(seconds=cadence * gap_cap_multiplier),
                ordered[index + 1]["observed_at"] if index + 1 < len(ordered) else end,
                current_policy.get("ended_at") or end)
        if b <= a:
            continue
        seconds = (b - a).total_seconds()
        covered += seconds
        integral += sample["player_count"] * seconds
        spans.append((a, b))
    gaps = []
    cursor = start
    for a, b in spans:
        if a > cursor:
            gaps.append({"from": cursor.isoformat(), "to": a.isoformat(), "seconds": (a - cursor).total_seconds()})
        cursor = max(cursor, b)
    if cursor < end:
        gaps.append({"from": cursor.isoformat(), "to": end.isoformat(), "seconds": (end - cursor).total_seconds()})
    peak = max(in_window, key=lambda row: row["player_count"], default=None)
    minimum = min(in_window, key=lambda row: row["player_count"], default=None)
    return {
        "coverage": {"sample_count": len(in_window), "expected_samples": expected,
                     "covered_seconds": covered, "requested_seconds": requested,
                     "tracked_seconds": tracked_seconds, "maximum_gap_seconds": max((g["seconds"] for g in gaps), default=0),
                     "coverage_ratio": covered / requested,
                     "tracked_coverage_ratio": covered / tracked_seconds if tracked_seconds else None,
                     "tracking_started_at": policies[0]["started_at"].isoformat() if policies else None},
        "metrics": {"observed_peak": peak["player_count"] if peak else None,
                    "observed_peak_at": peak["observed_at"].isoformat() if peak else None,
                    "observed_minimum": minimum["player_count"] if minimum else None,
                    "observed_minimum_at": minimum["observed_at"].isoformat() if minimum else None,
                    "integral_player_seconds": integral,
                    "average_observed_ccu": integral / covered if covered else None,
                    "estimator_version": ESTIMATOR_VERSION,
                    "metric_policy_version": policy_version(gap_cap_multiplier)},
        "gaps": gaps,
    }


def growth(samples: list[dict], start: datetime, end: datetime, tracking: list[dict],
           gap_cap_multiplier: float = 2, minimum_coverage_ratio: float = 0.9) -> dict:
    """Compare adjacent equal windows; incomplete history cannot imply growth."""
    if not 0 <= minimum_coverage_ratio <= 1:
        raise ValueError("metrics.min_coverage_ratio must be between 0 and 1")
    comparison_start = start - (end-start)
    older = calculate(samples, comparison_start, start, tracking, gap_cap_multiplier)
    newer = calculate(samples, start, end, tracking, gap_cap_multiplier)
    return growth_from_calculated(older, newer, start, end, gap_cap_multiplier, minimum_coverage_ratio)


def growth_from_calculated(older: dict, newer: dict, start: datetime, end: datetime,
                           gap_cap_multiplier: float = 2, minimum_coverage_ratio: float = 0.9) -> dict:
    """One growth formula for raw observations and exact persisted integrals."""
    if not 0 <= minimum_coverage_ratio <= 1:
        raise ValueError("metrics.min_coverage_ratio must be between 0 and 1")
    comparison_start = start - (end-start)
    old_average = older["metrics"]["average_observed_ccu"]
    new_average = newer["metrics"]["average_observed_ccu"]
    old_coverage = older["coverage"]["coverage_ratio"]
    new_coverage = newer["coverage"]["coverage_ratio"]
    absolute = percentage = None
    if old_average is None or new_average is None:
        status = "no_observations"
    elif min(old_coverage, new_coverage) < minimum_coverage_ratio:
        status = "insufficient_coverage"
    else:
        absolute = new_average - old_average
        if old_average == 0:
            status = "zero_baseline"
        else:
            status = "available"
            percentage = absolute / old_average * 100
    return {"from": start.isoformat(), "to": end.isoformat(),
            "comparison_from": comparison_start.isoformat(), "comparison_to": start.isoformat(),
            "older_average": old_average, "newer_average": new_average,
            "absolute_change": absolute, "percentage_change": percentage, "status": status,
            "minimum_coverage_ratio": minimum_coverage_ratio,
            "older_coverage_ratio": old_coverage, "newer_coverage_ratio": new_coverage,
            "metric_policy_version": policy_version(gap_cap_multiplier, minimum_coverage_ratio)}


def combine(calculations: list[dict], start: datetime, end: datetime, gap_cap_multiplier: float = 2) -> dict:
    """Compose nonoverlapping adjacent exact pieces by integral, never averages."""
    requested = (end-start).total_seconds()
    if requested <= 0:
        raise ValueError("history window start must precede its end")
    if not math.isclose(sum(piece["coverage"]["requested_seconds"] for piece in calculations), requested,
                        rel_tol=0, abs_tol=0.00001):
        raise ValueError("History pieces do not cover the complete requested window")
    covered = sum(piece["coverage"]["covered_seconds"] for piece in calculations)
    tracked = sum(piece["coverage"]["tracked_seconds"] for piece in calculations)
    integral = sum(piece["metrics"]["integral_player_seconds"] for piece in calculations)
    peaks = [piece["metrics"] for piece in calculations if piece["metrics"]["observed_peak"] is not None]
    peak = min(peaks, key=lambda item: (-item["observed_peak"], item["observed_peak_at"]), default=None)
    minimum = min(peaks, key=lambda item: (item["observed_minimum"], item["observed_minimum_at"]), default=None)
    gaps = []
    for piece in calculations:
        for gap in piece["gaps"]:
            if gaps and gaps[-1]["to"] == gap["from"]:
                gaps[-1]["to"] = gap["to"]
                gaps[-1]["seconds"] += gap["seconds"]
            else:
                gaps.append(dict(gap))
    starts = [piece["coverage"]["tracking_started_at"] for piece in calculations
              if piece["coverage"]["tracking_started_at"] is not None]
    return {"coverage": {"sample_count": sum(piece["coverage"]["sample_count"] for piece in calculations),
                         "expected_samples": sum(piece["coverage"]["expected_samples"] for piece in calculations),
                         "covered_seconds": covered, "requested_seconds": requested, "tracked_seconds": tracked,
                         "maximum_gap_seconds": max((gap["seconds"] for gap in gaps), default=0),
                         "coverage_ratio": covered/requested, "tracked_coverage_ratio": covered/tracked if tracked else None,
                         "tracking_started_at": min(starts) if starts else None},
            "metrics": {"observed_peak": peak["observed_peak"] if peak else None,
                        "observed_peak_at": peak["observed_peak_at"] if peak else None,
                        "observed_minimum": minimum["observed_minimum"] if minimum else None,
                        "observed_minimum_at": minimum["observed_minimum_at"] if minimum else None,
                        "integral_player_seconds": integral, "average_observed_ccu": integral/covered if covered else None,
                        "estimator_version": ESTIMATOR_VERSION, "metric_policy_version": policy_version(gap_cap_multiplier)},
            "gaps": gaps}


def rollup(samples: list[dict], start: datetime, end: datetime, tracking: list[dict],
           bucket_seconds: int, gap_cap_multiplier: float = 2) -> list[dict]:
    """Rebuild UTC-aligned buckets from source observations, including edge carry.

    A bucket keeps integral and duration separately. Its average is never used
    as an input to another metric; canonical observations remain the owner.
    """
    if type(bucket_seconds) is not int or bucket_seconds < 1:
        raise ValueError("bucket_seconds must be a positive integer")
    if start >= end:
        raise ValueError("history window start must precede its end")
    ordered = _ordered(samples)
    times = [row["observed_at"] for row in ordered]
    cursor = start
    result = []
    while cursor < end:
        next_boundary = datetime.fromtimestamp(
            (math.floor(cursor.timestamp()/bucket_seconds)+1)*bucket_seconds, timezone.utc)
        boundary = min(end, next_boundary)
        left, right = bisect_left(times, cursor), bisect_left(times, boundary)
        rows = ordered[left:right]
        calculated = calculate(ordered[max(0, left-1):right], cursor, boundary, tracking, gap_cap_multiplier)
        metric = calculated["metrics"]
        result.append({"from": cursor.isoformat(), "to": boundary.isoformat(),
                       "first": _point(rows[0]) if rows else None,
                       "last": _point(rows[-1]) if rows else None,
                       "minimum": _point(min(rows, key=lambda row: row["player_count"], default=None)),
                       "maximum": _point(max(rows, key=lambda row: row["player_count"], default=None)),
                       "sample_count": len(rows), "integral_player_seconds": metric["integral_player_seconds"],
                       "covered_seconds": calculated["coverage"]["covered_seconds"],
                       "requested_seconds": calculated["coverage"]["requested_seconds"],
                       "average_observed_ccu": metric["average_observed_ccu"], "gaps": calculated["gaps"]})
        cursor = boundary
    return result


def downsample(samples: list[dict], start: datetime, end: datetime, tracking: list[dict],
               max_points: int, gap_cap_multiplier: float = 2) -> dict:
    """Bound the display while retaining original extrema and outage boundaries.

    If the gap boundaries alone exceed the limit, reject the request rather
    than silently join a gap or erase its neighboring observations.
    """
    if type(max_points) is not int or max_points < 4:
        raise ValueError("max_points must be an integer of at least 4")
    ordered = _ordered(samples)
    current = [row for row in ordered if start <= row["observed_at"] < end]
    if len(current) <= max_points:
        return {"points": [_point(row) for row in current], "resolution": "raw", "bucket_seconds": None, "rollups": []}
    times = [row["observed_at"] for row in current]
    gaps = calculate(ordered, start, end, tracking, gap_cap_multiplier)["gaps"]
    mandatory = {}

    def retain(target, point):
        if point is not None:
            target[(point["observed_at"], point["player_count"])] = point

    retain(mandatory, _point(current[0]))
    retain(mandatory, _point(current[-1]))
    for gap in gaps:
        before = bisect_right(times, datetime.fromisoformat(gap["from"])) - 1
        after = bisect_left(times, datetime.fromisoformat(gap["to"]))
        if before >= 0:
            retain(mandatory, _point(current[before]))
        if after < len(current):
            retain(mandatory, _point(current[after]))
    if len(mandatory) > max_points:
        raise ValueError("History gap boundaries exceed web.max_points. Request a smaller hours window or raise web.max_points; no gaps were removed.")
    duration = (end-start).total_seconds()
    bucket_seconds = max(1, math.ceil(duration/max(1, max_points//4)))
    while True:
        buckets = rollup(ordered, start, end, tracking, bucket_seconds, gap_cap_multiplier)
        selected = dict(mandatory)
        for bucket in buckets:
            for key in ("first", "last", "minimum", "maximum"):
                retain(selected, bucket[key])
        if len(selected) <= max_points:
            return {"points": sorted(selected.values(), key=lambda row: (row["observed_at"], row["player_count"])),
                    "resolution": "bucketed", "bucket_seconds": bucket_seconds, "rollups": buckets}
        if bucket_seconds >= max(duration, 1)*2:
            raise ValueError("History extrema and gap boundaries exceed web.max_points. Request a smaller hours window or raise web.max_points; no observations were silently truncated.")
        bucket_seconds *= 2
