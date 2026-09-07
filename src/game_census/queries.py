"""Bounded stored-statistics queries shared by HTML and the versioned read API."""
from datetime import datetime, timedelta, timezone
import re

from .contracts import AppList, AppSummary, Comparison, ComparisonSeries, PlayerHistory, Rankings
from .db import DatabaseError, QueryLimitError
from .sources import players


class UnknownAppError(DatabaseError):
    pass


def catalog_summary(row, tracked=None):
    values = tracked or {"app_id": row["app_id"], "name": row["name"], "availability": "not_tracked",
                         "sample_count": 0, "source": players.SOURCE, "source_version": players.VERSION}
    return AppSummary(**{**values, "name": row["name"], "catalog_source": row["source"],
                         "catalog_observed_at": row["observed_at"]}).model_dump()


def app_summary(settings, db, app_id):
    tracked = db.app_detail(app_id, settings)
    if tracked is not None:
        return AppSummary.model_validate(tracked).model_dump()
    row = db.discovered_app(app_id)
    if row is None:
        raise UnknownAppError(f"Steam app {app_id} is not known to this instance. Search for a known game.")
    return catalog_summary(row)


def catalog_search(settings, db, query="", page=1, page_size=25):
    if not 1 <= page <= 4294967295 or not 1 <= page_size <= settings.web.max_page_size or len(query) > 100:
        raise QueryLimitError("Catalog search exceeds web.max_page_size or its page/query bounds. Request a smaller page.")
    result = db.catalog(query, page, page_size)
    if len(result["items"]) > page_size:
        raise DatabaseError("Catalog search returned more rows than requested. Inspect the stored catalog query.")
    tracked = {row["app_id"]: row for row in db.list_apps(settings)}
    return AppList(items=[catalog_summary(row, tracked.get(row["app_id"])) for row in result["items"]],
                   total=result["total"], tracking_scope="catalog", generated_at=datetime.now(timezone.utc),
                   page=page, page_size=page_size).model_dump()


def window(settings, *, hours=None, from_time=None, to=None, now=None):
    now = now or datetime.now(timezone.utc)
    if from_time is not None or to is not None:
        if hours is not None or from_time is None or to is None:
            raise QueryLimitError("Supply both from and to, or hours alone. A comparison needs one common time window.")
        if not all(isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None for value in (from_time, to)):
            raise QueryLimitError("from and to must include a UTC offset. Use an ISO 8601 timestamp ending in Z or +00:00.")
        start, end = from_time.astimezone(timezone.utc), to.astimezone(timezone.utc)
    else:
        hours = 24 if hours is None else hours
        if type(hours) is not int or not 1 <= hours <= settings.web.max_history_days * 24:
            raise QueryLimitError("hours must be an integer from 1 through web.max_history_days × 24. Request a smaller window.")
        end, start = now, now-timedelta(hours=hours)
    if not start < end <= now or end-start > timedelta(days=settings.web.max_history_days):
        raise QueryLimitError("from must precede to, to cannot be in the future, and the range cannot exceed web.max_history_days.")
    return start, end


def app_ids(settings, value):
    if not isinstance(value, str) or len(value) > 110:
        raise QueryLimitError("app_ids must be a bounded comma-separated list of Steam app IDs.")
    parts = value.split(",")
    if not 1 <= len(parts) <= settings.web.max_compare_apps:
        raise QueryLimitError("Select from 1 through web.max_compare_apps games. Reduce the comparison selection.")
    if any(not re.fullmatch(r"[0-9]{1,10}", part) for part in parts):
        raise QueryLimitError("app_ids must contain only positive numeric Steam app IDs separated by commas.")
    ids = [int(part) for part in parts]
    if any(not 1 <= value <= 4294967295 for value in ids) or len(ids) != len(set(ids)):
        raise QueryLimitError("app_ids must be unique IDs from 1 through 4294967295.")
    return ids


def rankings(settings, db):
    generated = datetime.now(timezone.utc)
    cohort = set(settings.tracking.app_ids)
    known = {item.app_id: item for row in db.list_apps(settings)
             if (item := AppSummary.model_validate(row)).app_id in cohort}
    exclusions = {state: sum(item.availability == state for item in known.values())
                  for state in ("stale", "no_observations", "unsupported")}
    exclusions["not_initialized"] = len(cohort - known.keys()) + sum(item.availability == "not_tracked" for item in known.values())
    fresh = sorted((item for item in known.values() if item.availability == "fresh"),
                   key=lambda item: (-item.player_count if item.player_count is not None else 0, item.app_id))
    if any(item.player_count is None or item.observed_at is None for item in fresh):
        raise DatabaseError("A fresh ranking entry lacks its observed count or time. Inspect the stored player projection.")
    return Rankings(items=[{**item.model_dump(), "rank": index+1} for index, item in enumerate(fresh)],
                    cohort_size=len(cohort), ranked_apps=len(fresh), excluded=exclusions,
                    generated_at=generated, source=players.SOURCE, source_version=players.VERSION).model_dump()


def compare(settings, db, selection, *, hours=None, from_time=None, to=None, resolution="auto"):
    ids = app_ids(settings, selection)
    generated = datetime.now(timezone.utc)
    start, end = window(settings, hours=hours, from_time=from_time, to=to, now=generated)
    if resolution not in ("raw", "auto"):
        raise QueryLimitError("resolution must be raw or auto.")
    known = {item.app_id: item for row in db.list_apps(settings) if (item := AppSummary.model_validate(row)).app_id in ids}
    series, total = [], 0
    for app_id in ids:
        item = known.get(app_id)
        if item is None:
            discovered = db.discovered_app(app_id)
            if discovered is None:
                raise UnknownAppError(f"Steam app {app_id} is not known to this instance. Search for a known game before comparing.")
            series.append(ComparisonSeries(app_id=app_id, name=discovered["name"], availability="not_tracked",
                                           reason="This catalog game has no tracked player history in this instance."))
            continue
        result = ComparisonSeries(**item.model_dump())
        if item.availability == "unsupported":
            result.reason = "The player source is unsupported for this game. Other series remain available."
        else:
            history = db.history_range(app_id, settings, start, end, resolution)
            if history is None:
                raise DatabaseError("A tracked comparison game has no history contract. Inspect its stored tracking state.")
            result.history = PlayerHistory.model_validate(history)
            if result.history.from_time != start or result.history.to != end:
                raise DatabaseError("A comparison history returned different time bounds. Inspect the stored history query.")
            total += len(result.history.points)
            if len(result.history.points) > settings.web.max_points or total > settings.web.max_compare_points:
                raise QueryLimitError("Comparison exceeds web.max_points or web.max_compare_points. Use fewer apps, a shorter window or auto resolution; no series was omitted.")
            if not result.history.points:
                result.reason = "No player observations were recorded in this time window. Unknown time is not zero."
        series.append(result)
    return Comparison(from_time=start, to=end, generated_at=generated, series=series, returned_points=total,
                      source=players.SOURCE, source_version=players.VERSION).model_dump(by_alias=True)
