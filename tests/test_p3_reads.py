"""P3 stored-read contracts; synthetic sources and retained scratch PostgreSQL."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from game_census import queries
from game_census.config import Settings, Storage
from game_census.db import DatabaseError
from game_census.metrics import calculate
from game_census.sources.players import SOURCE, VERSION
from game_census.web import create_app
from test_api import FakeDatabase
from test_bootstrap import scratch_database
from test_storage import store


class ReadFixture(FakeDatabase):
    def __init__(self):
        super().__init__()
        self.now = datetime.now(timezone.utc)
        self.ranges = []
        original = self.apps[0]
        self.apps = []
        for app_id, name, state, count in [
            (570, "Synthetic Zero", "fresh", 0), (620, "Synthetic Peer", "fresh", 0),
            (730, "Synthetic Stale", "stale", 9000), (740, "Synthetic New", "no_observations", None),
            (750, "Synthetic Unsupported", "unsupported", None),
        ]:
            row = deepcopy(original)
            row.update(app_id=app_id, name=name, availability=state, player_count=count)
            if state == "stale":
                row.update(observed_at=self.now-timedelta(hours=2), last_attempt={
                    "status": "timeout", "at": self.now, "error": {"code": "timeout",
                    "message": "The player source timed out.", "next_action": "Inspect Status before retrying."}})
            elif count is None:
                row.update(observed_at=None, sample_count=0, observed_24h_peak=None, highest_recorded=None, last_attempt=None)
            self.apps.append(row)

    def discovered_app(self, app_id):
        return {"app_id": 400, "name": "Synthetic Catalog Only", "source": "steam_catalog_v2",
                "observed_at": self.now.isoformat(), "charts": []} if app_id == 400 else None

    def catalog(self, query="", page=1, page_size=25):
        if self.error:
            raise self.error
        rows = [{**row, "tracked": True} for row in self.apps]
        rows.append({**self.discovered_app(400), "tracked": False})
        matches = sorted([row for row in rows if query.casefold() in row["name"].casefold() or query == str(row["app_id"])],
                         key=lambda row: (row["name"].casefold(), row["app_id"]))
        return {"items": matches[(page-1)*page_size:page*page_size], "total": len(matches),
                "page": page, "page_size": page_size, "query": query, "scope": "locally discovered Steam apps"}

    def history(self, app_id, settings, hours=24, resolution="raw"):
        return self.history_range(app_id, settings, self.now-timedelta(hours=hours), self.now, resolution)

    def history_range(self, app_id, settings, start, end, resolution="raw"):
        self.ranges.append((app_id, start, end, resolution))
        if self.error:
            raise self.error
        row = next((row for row in self.apps if row["app_id"] == app_id), None)
        if row is None:
            return None
        points = []
        if row["player_count"] is not None:
            factor = 2 if app_id == 620 else 1
            points = [{"observed_at": self.now-timedelta(minutes=minute), "player_count": count*factor}
                      for minute, count in [(30, 21), (25, 8), (1, 0)]
                      if start <= self.now-timedelta(minutes=minute) < end]
        metrics = calculate(points, start, end, [{"started_at": row["tracking_started_at"], "interval_seconds": 300}])
        return {"app_id": app_id, "from": start, "to": end, "points": points,
                **metrics, "source": SOURCE, "source_version": VERSION}


def read_settings():
    settings = Settings(storage=Storage(database_url="postgresql://fixture:fixture@localhost/fixture"))
    settings.tracking.app_ids = [570, 620, 730, 740, 750]
    return settings


@pytest.fixture
def read_app():
    db, settings = ReadFixture(), read_settings()
    return TestClient(create_app(settings, db)), db, settings


def test_rankings_only_include_fresh_configured_cohort_and_keep_zero_ties(read_app):
    client, db, settings = read_app
    db.apps.append({**db.apps[0], "app_id": 999, "player_count": 999999})
    settings.tracking.app_ids.append(888)
    result = client.get("/api/v1/rankings")
    assert result.status_code == 200
    body = result.json()
    assert [(row["rank"], row["app_id"], row["player_count"]) for row in body["items"]] == [(1, 570, 0), (2, 620, 0)]
    assert body["cohort_size"] == 6 and body["ranked_apps"] == 2
    assert body["excluded"] == {"stale": 1, "no_observations": 1, "unsupported": 1, "not_initialized": 1}
    assert body["scope"] == "fresh_observations_in_configured_cohort"
    assert body["source"] == SOURCE and body["generated_at"]


def test_ranking_empty_and_inconsistent_fresh_state_are_distinct(read_app):
    client, db, _ = read_app
    db.apps = []
    response = client.get("/api/v1/rankings")
    assert response.status_code == 200 and response.json()["items"] == []
    db.apps = ReadFixture().apps
    db.apps[0]["player_count"] = None
    assert client.get("/api/v1/rankings").status_code == 503


def test_comparison_uses_exact_common_utc_window_and_preserves_gaps_and_zero(read_app):
    client, db, _ = read_app
    start, end = db.now-timedelta(hours=1), db.now
    params = {"app_ids": "620,570", "from": start.astimezone(timezone(timedelta(hours=-4))).isoformat(), "to": end.isoformat()}
    response = client.get("/api/v1/compare", params=params)
    assert response.status_code == 200
    body = response.json()
    assert [row["app_id"] for row in body["series"]] == [620, 570]
    assert body["returned_points"] == 6
    assert db.ranges == [(620, start, end, "auto"), (570, start, end, "auto")]
    for row in body["series"]:
        history = row["history"]
        assert (history["from"], history["to"]) == (body["from"], body["to"])
        assert history["points"][-1]["player_count"] == 0 and history["gaps"]
        assert 0 < history["coverage"]["coverage_ratio"] < 1
        assert history["source"] == SOURCE


def test_comparison_retains_every_unavailable_series_and_failed_attempt(read_app):
    client, db, _ = read_app
    response = client.get("/api/v1/compare", params={"app_ids": "570,730,740,750,400"})
    assert response.status_code == 200
    body = response.json()
    assert [row["availability"] for row in body["series"]] == ["fresh", "stale", "no_observations", "unsupported", "not_tracked"]
    assert body["series"][1]["last_attempt"]["status"] == "timeout"
    assert body["series"][2]["history"]["metrics"]["average_observed_ccu"] is None
    assert body["series"][2]["history"]["points"] == []
    assert all(row["reason"] for row in body["series"][2:])
    assert all(row["history"] is None for row in body["series"][3:])
    assert [row[0] for row in db.ranges] == [570, 730, 740]


@pytest.mark.parametrize("selection", [None, "", "0", "-1", "4294967296", "570,570", " 570", "570,", "570,620,730,740,750,400", "x", "5"*111])
def test_comparison_rejects_invalid_selection_before_reading_history(read_app, selection):
    client, db, _ = read_app
    params = {} if selection is None else {"app_ids": selection}
    assert client.get("/api/v1/compare", params=params).status_code == 422
    assert db.ranges == []


@pytest.mark.parametrize("params", [
    {"hours": 0}, {"hours": 999999}, {"resolution": "discard"},
    {"from": "2026-01-01T00:00:00Z"}, {"to": "2026-01-02T00:00:00Z"},
    {"from": "2026-01-01T00:00:00", "to": "2026-01-02T00:00:00Z"},
    {"from": "2026-01-02T00:00:00Z", "to": "2026-01-01T00:00:00Z"},
    {"from": "2026-01-01T00:00:00Z", "to": "2026-01-01T00:00:00Z"},
    {"hours": 1, "from": "2026-01-01T00:00:00Z", "to": "2026-01-02T00:00:00Z"},
    {"from": "2026-01-01T00:00:00Z", "to": "2027-01-02T00:00:00Z"},
])
def test_comparison_rejects_invalid_or_ambiguous_windows(read_app, params):
    client, db, _ = read_app
    assert client.get("/api/v1/compare", params={"app_ids": "570", **params}).status_code == 422
    assert db.ranges == []


def test_comparison_unknown_app_fails_whole_request(read_app):
    client, _, _ = read_app
    response = client.get("/api/v1/compare?app_ids=570,999")
    assert response.status_code == 404 and "series" not in response.json()


def test_comparison_enforces_combined_point_budget_without_dropping_series(read_app, monkeypatch):
    client, db, settings = read_app
    settings.web.max_compare_points = 10
    original = db.history_range

    def many_points(*args):
        result = original(*args)
        result["points"] *= 2
        return result

    monkeypatch.setattr(db, "history_range", many_points)
    result = client.get("/api/v1/compare?app_ids=570,620")
    assert result.status_code == 422 and "no series was omitted" in result.text
    assert "series" not in result.json()


def test_comparison_rejects_misaligned_history(read_app, monkeypatch):
    client, db, _ = read_app
    original = db.history_range

    def misaligned(*args):
        result = original(*args)
        result["from"] += timedelta(seconds=1)
        return result

    monkeypatch.setattr(db, "history_range", misaligned)
    assert client.get("/api/v1/compare?app_ids=570").status_code == 503


def test_explicit_history_range_and_repeated_selection_parameters(read_app):
    client, db, _ = read_app
    params = {"from": (db.now-timedelta(hours=1)).isoformat(), "to": db.now.isoformat()}
    body = client.get("/api/v1/apps/570/history", params=params).json()
    assert len(body["points"]) == 3
    response = client.get("/api/v1/compare?app_ids=570&app_ids=620&hours=1")
    assert response.status_code == 200 and len(response.json()["series"]) == 2


def test_catalog_api_is_paginated_and_known_untracked_is_not_unknown(read_app):
    client, _, _ = read_app
    first = client.get("/api/v1/apps?scope=catalog&page_size=2").json()
    second = client.get("/api/v1/apps?scope=catalog&page_size=2&page=2").json()
    assert first["tracking_scope"] == "catalog" and first["total"] == 6
    assert len(first["items"]) == len(second["items"]) == 2
    assert not {row["app_id"] for row in first["items"]} & {row["app_id"] for row in second["items"]}
    assert client.get("/api/v1/apps?scope=catalog&q=absent").json()["items"] == []
    assert client.get("/api/v1/apps?scope=catalog&q=400").json()["total"] == 1
    for path in ["/api/v1/apps/400", "/api/v1/apps/400/players"]:
        response = client.get(path)
        assert response.status_code == 200
        row = response.json()
        assert row["availability"] == "not_tracked"
        assert row["player_count"] is None and row["observed_at"] is None
        assert row["tracking_started_at"] is None and row["expected_interval_seconds"] is None
        assert row["catalog_observed_at"] and row["catalog_source"] == "steam_catalog_v2"
    assert client.get("/api/v1/apps/999").status_code == 404
    assert client.get("/api/v1/apps?scope=catalog&page=4294967295").json()["items"] == []


@pytest.mark.parametrize("suffix", ["page=0", "page_size=0", "page_size=101", "page=4294967296", "q=" + "x"*101])
def test_catalog_api_rejects_out_of_bounds_input(read_app, suffix):
    client, _, _ = read_app
    assert client.get("/api/v1/apps?scope=catalog&" + suffix).status_code == 422


def test_read_configuration_bounds_are_enforced_and_lower_page_limit_is_usable():
    from pydantic import ValidationError
    from game_census.config import Web
    for field, values in {"max_compare_apps": [0, 11], "max_compare_points": [9, 100001], "max_page_size": [0, 501]}.items():
        for value in values:
            with pytest.raises(ValidationError, match=field):
                Web(**{field: value})
    settings, db = read_settings(), ReadFixture()
    settings.web.max_page_size = 1
    client = TestClient(create_app(settings, db))
    assert len(client.get("/api/v1/apps?scope=catalog").json()["items"]) == 1
    assert client.get("/api/v1/catalog").json()["page_size"] == 1
    assert client.get("/api/v1/apps?scope=catalog&page_size=2").status_code == 422
    assert client.get("/api/v1/apps?page=1").status_code == 422


@pytest.mark.parametrize("path", ["/api/v1/compare?app_ids=570,620", "/api/v1/rankings", "/compare?app_ids=570", "/rankings"])
def test_read_failure_is_reported_without_leaking_database_message(read_app, path, caplog):
    client, db, _ = read_app
    db.error = DatabaseError("postgresql://private:do-not-print@db/fixture")
    response = client.get(path)
    assert response.status_code == 503
    assert "do-not-print" not in response.text + caplog.text
    assert "incident=" in caplog.text


def test_pages_share_queries_and_expose_independent_accessible_charts(read_app, monkeypatch):
    client, _, _ = read_app
    monkeypatch.setattr("game_census.collector.collect_discovery", lambda *a, **kw: pytest.fail("GET collected Steam data"))
    body = client.get("/compare?app_ids=570,620&hours=24")
    assert body.status_code == 200
    assert 'id="chart-title-chart-570"' in body.text and 'id="chart-title-chart-620"' in body.text
    assert body.text.count('data-chart ') == 2
    assert "same time and player-count scales" in body.text
    assert "View comparison as JSON" in body.text
    for path in ["/", "/rankings", "/compare", "/search?q=Synthetic", "/apps/570", "/methodology", "/status"]:
        assert client.get(path).status_code == 200, path
    paths = client.get("/openapi.json").json()["paths"]
    assert all(path in paths for path in ["/api/v1/compare", "/api/v1/rankings"])


def test_short_explicit_comparison_keeps_valid_hours_picker(read_app):
    client, db, _ = read_app
    params = {"app_ids": "570", "from": (db.now-timedelta(seconds=30)).isoformat(), "to": db.now.isoformat()}
    response = client.get("/compare", params=params)
    assert response.status_code == 200
    assert 'name="hours" value="1" min="1"' in response.text


@pytest.mark.integration
def test_real_database_explicit_range_is_half_open_and_cache_matches_comparison(scratch_database):
    db, settings = scratch_database
    start = datetime.now(timezone.utc)-timedelta(minutes=20)
    middle, end = start+timedelta(minutes=5), start+timedelta(minutes=10)
    store(db, start, 21)
    store(db, middle, 0)
    store(db, end, 999)
    raw = db.history_range(570, settings, start, end, "raw")
    assert [point["player_count"] for point in raw["points"]] == [21, 0]
    result = queries.compare(settings, db, "570", from_time=start, to=end)
    series = result["series"][0]["history"]
    assert [point["player_count"] for point in series["points"]] == [21, 0]
    assert (series["from"], series["to"]) == (start, end)
    assert series["metrics"]["observed_peak"] == 21
