"""Read boundaries use synthetic stored records, never live Steam requests."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from game_census.config import Settings, Storage
from game_census.db import DatabaseError, QueryLimitError
from game_census.sources.players import SOURCE, VERSION
from game_census.web import create_app


class FakeDatabase:
    def __init__(self):
        now = datetime.now(timezone.utc)
        self.apps = [{"app_id": 570, "name": "Synthetic fixture game", "player_count": 0,
                      "availability": "fresh", "observed_at": now, "tracking_started_at": now - timedelta(hours=1),
                      "sample_count": 3, "observed_24h_peak": 21, "highest_recorded": 21,
                      "last_attempt": {"status": "success", "at": now, "error": None},
                      "expected_interval_seconds": 300, "source": SOURCE, "source_version": VERSION}]
        self.history_value = {"app_id": 570, "from": now - timedelta(hours=24), "to": now,
                              "points": [{"observed_at": now - timedelta(minutes=30), "player_count": 21},
                                         {"observed_at": now - timedelta(minutes=25), "player_count": 8},
                                         {"observed_at": now - timedelta(minutes=1), "player_count": 0}],
                              "coverage": {"sample_count": 3, "expected_samples": 12, "covered_seconds": 960,
                                           "requested_seconds": 86400, "maximum_gap_seconds": 84600, "coverage_ratio": 960 / 86400},
                              "metrics": {"observed_peak": 21, "average_observed_ccu": 11.56, "estimator_version": "capped-step-v1"},
                              "gaps": [{"from": now - timedelta(hours=24), "to": now - timedelta(minutes=30), "seconds": 84600}],
                              "source": SOURCE, "source_version": VERSION}
        self.error = None
        self.calls = []

    def list_apps(self, settings):
        self.calls.append("list_apps")
        if self.error:
            raise self.error
        return deepcopy(self.apps)

    def dashboard(self):
        self.calls.append("dashboard")
        from game_census.sources.discovery import PLAYED, SALES, URLS
        return {"catalog_apps": len(self.apps), "tracked_apps": len(self.apps),
                "charts": {source: {"items": [], "observed_at": None, "age_seconds": None, "source_url": URLS[source]} for source in (PLAYED, SALES)},
                "latest_attempts": {PLAYED: None, SALES: None}, "catalog_sync": {"complete": False},
                "trending": {"items": [], "from": None, "to": None, "scope": "Apps in both snapshots"}}

    def catalog(self, query="", page=1, page_size=25):
        self.calls.append("catalog")
        items = [dict(row, tracked=True) for row in self.apps if query.lower() in row["name"].lower() or query == str(row["app_id"])]
        return {"items": items, "total": len(items), "page": page, "page_size": page_size}

    def discovered_app(self, app_id):
        return None

    def latest_cohort(self):
        return None

    def game_details(self, app_id):
        self.calls.append("game_details")
        return {"snapshots": {}, "prices": [], "updates": [], "last_refresh": None,
                "highest_recorded": None, "observed_24h_peak": None}

    def app_detail(self, app_id, settings):
        self.calls.append("app_detail")
        if self.error:
            raise self.error
        return next((deepcopy(item) for item in self.apps if item["app_id"] == app_id), None)

    def history(self, app_id, settings, hours=24, resolution="raw"):
        self.calls.append("history")
        if self.error:
            raise self.error
        return deepcopy(self.history_value) if app_id == 570 else None

    def status(self):
        self.calls.append("status")
        if self.error:
            raise self.error
        return {"database": "ok", "scheduler": "disabled", "private_config": "never-public"}

    def last_run(self):
        self.calls.append("last_run")
        return {"run_id": "synthetic-run", "status": "succeeded", "succeeded": 1,
                "database_url": "postgresql://private:secret@db/game_census"}


@pytest.fixture
def fixture_app():
    settings = Settings(storage=Storage(database_url=SecretStr("postgresql://fixture:fixture@localhost/game_census")))
    db = FakeDatabase()
    return TestClient(create_app(settings, db)), db, settings


def test_zero_is_preserved_with_source_and_timestamp(fixture_app):
    client, _, _ = fixture_app
    body = client.get("/api/v1/apps/570/players").json()
    assert body["player_count"] == 0
    assert body["availability"] == "fresh"
    assert body["source"] == SOURCE
    assert body["observed_at"]


def test_stale_and_failed_attempt_remain_separate(fixture_app):
    client, db, _ = fixture_app
    db.apps[0].update(player_count=87, availability="stale", last_attempt={"status": "timeout", "at": datetime.now(timezone.utc),
                    "error": {"code": "timeout", "message": "Steam request timed out.", "next_action": "Retry a bounded collection."}})
    response = client.get("/api/v1/apps/570/players")
    assert response.status_code == 200
    assert response.json()["player_count"] == 87
    assert response.json()["availability"] == "stale"
    assert response.json()["last_attempt"]["error"]["code"] == "timeout"
    html = client.get("/apps/570").text
    assert "Last observed concurrent players" in html
    assert "Steam request timed out." in html
    assert "Retry a bounded collection." in html


@pytest.mark.parametrize("route", ["/api/v1/apps/0", "/api/v1/apps/4294967296", "/api/v1/apps/570/history?hours=0", "/api/v1/apps/570/history?hours=999999", "/api/v1/apps?q=" + "x" * 101])
def test_public_inputs_are_bounded(fixture_app, route):
    client, _, _ = fixture_app
    response = client.get(route)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"


def test_unknown_app_is_not_empty_success(fixture_app):
    client, _, _ = fixture_app
    assert client.get("/api/v1/apps/999").status_code == 404
    assert client.get("/api/v1/apps/999/history").status_code == 404


def test_storage_failure_is_reported_without_credentials(fixture_app, caplog):
    client, db, _ = fixture_app
    db.error = DatabaseError("postgresql://private:secret@db/game_census")
    response = client.get("/api/v1/apps")
    assert response.status_code == 503
    assert "secret" not in response.text + caplog.text
    assert "incident" in response.json()["error"]
    assert "read_service_failed" in caplog.text
    assert client.get("/health/live").status_code == 200
    assert client.get("/health/ready").status_code == 503


def test_history_limit_error_is_actionable(fixture_app):
    client, db, _ = fixture_app
    db.error = QueryLimitError("History exceeds web.max_points. Request a smaller hours window.")
    response = client.get("/api/v1/apps/570/history")
    assert response.status_code == 422
    assert "smaller" in response.json()["error"]["message"]


def test_history_keeps_coverage_gaps_and_policy(fixture_app):
    client, _, _ = fixture_app
    response = client.get("/api/v1/apps/570/history")
    assert response.status_code == 200
    body = response.json()
    assert len(body["points"]) == 3
    assert body["points"][-1]["player_count"] == 0
    assert body["coverage"]["coverage_ratio"] < 0.02
    assert body["gaps"][0]["seconds"] == 84600
    assert body["metrics"]["estimator_version"] == "capped-step-v1"
    assert body["source_version"] == VERSION


def test_reads_do_not_initialize_or_collect_and_status_is_allowlisted(fixture_app):
    client, db, _ = fixture_app
    for route in ["/", "/apps/570", "/status", "/methodology", "/api/v1/apps", "/api/v1/apps/570/history", "/health/ready"]:
        assert client.get(route).status_code == 200
    status = client.get("/api/v1/status")
    assert "never-public" not in status.text
    assert "private" not in status.text
    assert set(db.calls) <= {"list_apps", "app_detail", "history", "status", "last_run", "catalog", "dashboard", "game_details"}
    assert client.post("/api/v1/apps", json={"app_id": 123}).status_code == 405


def test_search_only_matches_configured_cohort(fixture_app):
    client, _, _ = fixture_app
    assert client.get("/api/v1/apps?q=570").json()["total"] == 1
    assert client.get("/api/v1/apps?q=not-present").json()["total"] == 0
    assert client.get("/api/v1/apps").json()["tracking_scope"] == "enrolled"


def test_retained_enrollment_is_not_mislabeled_current_configuration(fixture_app):
    client, db, settings = fixture_app
    assert settings.tracking.app_ids == [570]
    retained = deepcopy(db.apps[0])
    retained.update(app_id=730, name="Synthetic retained enrollment")
    db.apps.append(retained)
    body = client.get("/api/v1/apps").json()
    assert {item["app_id"] for item in body["items"]} == {570, 730}
    assert body["tracking_scope"] == "enrolled"
    assert "Your collection" in client.get("/").text


def test_web_security_headers_and_local_assets(fixture_app):
    client, _, _ = fixture_app
    response = client.get("/")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "script-src 'self'" in response.headers["content-security-policy"]
    assert response.headers["cache-control"] == "no-store"
    assert client.get("/static/app.css").status_code == 200
    assert client.get("/static/app.js").status_code == 200
    assert "cdn" not in response.text.lower()


def test_upstream_text_is_escaped_in_html(fixture_app):
    client, db, _ = fixture_app
    db.apps[0]["name"] = '<script>alert("unsafe")</script>'
    response = client.get("/apps/570")
    assert '<script>alert("unsafe")</script>' not in response.text
    assert "&lt;script&gt;" in response.text


def test_openapi_exposes_typed_read_models(fixture_app):
    client, _, _ = fixture_app
    schema = client.get("/openapi.json").json()
    assert "PlayerHistory" in schema["components"]["schemas"]
    assert "get" in schema["paths"]["/api/v1/apps"]
    assert "post" not in schema["paths"]["/api/v1/apps"]


def test_invalid_stored_contract_returns_reportable_safe_failure(fixture_app, caplog):
    client, db, _ = fixture_app
    db.apps[0]["player_count"] = "postgresql://private:secret@db/game_census"
    response = TestClient(client.app, raise_server_exceptions=False).get("/api/v1/apps")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "read_contract_failure"
    assert "incident" in response.json()["error"]
    assert "secret" not in response.text + caplog.text
