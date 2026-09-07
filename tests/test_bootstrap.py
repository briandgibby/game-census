"""Live PostgreSQL checks using retained isolated schemas and offline Steam responses."""
from contextlib import contextmanager
import os
import uuid
import httpx
import pytest
from psycopg import sql
from game_census.config import load_settings
from game_census.db import Database, QueryLimitError
from game_census.collector import collect_once

pytestmark = pytest.mark.integration


class SchemaDatabase(Database):
    def __init__(self, dsn, schema):
        super().__init__(dsn)
        self.schema = schema

    @contextmanager
    def connection(self):
        with super().connection() as conn:
            conn.execute(sql.SQL("SET LOCAL search_path TO {} ").format(sql.Identifier(self.schema)))
            yield conn


@pytest.fixture
def scratch_database(monkeypatch):
    config = os.environ.get("GAME_CENSUS_CONFIG")
    if not config:
        pytest.skip("Set GAME_CENSUS_CONFIG to run isolated PostgreSQL integration checks.")
    settings = load_settings(config).model_copy(deep=True)
    settings.tracking.app_ids = [570]
    settings.tracking.interval_seconds = 300
    settings.http.max_attempts = 1
    settings.http.timeout_seconds = 1
    settings.sources.store_metadata_enabled = False
    settings.sources.catalog_api_key = None
    schema = "game_census_test_" + uuid.uuid4().hex
    dsn = settings.storage.database_url.get_secret_value()
    with Database(dsn).connection() as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {} ").format(sql.Identifier(schema)))
    db = SchemaDatabase(dsn,schema)
    db.initialize(settings.tracking.app_ids,settings.tracking.interval_seconds)
    # HTTP uses MockTransport throughout these tests, so no live pacing is necessary.
    monkeypatch.setattr("game_census.db.time.sleep",lambda seconds: None)
    return db,settings


def transport(count=0, status=200, store_status=200):
    def respond(request):
        if request.url.host == "api.steampowered.com":
            return httpx.Response(status,json={"response":{"result":1,"player_count":count}})
        return httpx.Response(store_status,json={"570":{"success":True,"data":{"steam_appid":570,"name":"Fixture Game"}}})
    return httpx.MockTransport(respond)


def test_empty_install_collect_and_reinitialize_preserve_real_projection(scratch_database):
    db,settings = scratch_database
    assert db.app_detail(570,settings)["availability"] == "no_observations"
    run = collect_once(settings,db,transport=transport(count=0))
    before = db.rebuild()
    again = db.initialize([570],300)
    after = db.rebuild()
    assert run["status"] == "succeeded"
    assert again["captures"] == 1
    assert before == after
    row = db.app_detail(570,settings)
    assert row["availability"] == "fresh"
    assert row["player_count"] == 0
    assert row["sample_count"] == 1
    assert row["last_attempt"]["status"] == "succeeded"
    with db.connection() as conn:
        assert conn.execute("SELECT count(*) AS n FROM tracking_interval").fetchone()["n"] == 1


def test_partial_metadata_failure_keeps_count_and_both_attempts(scratch_database):
    db,settings = scratch_database
    settings.sources.store_metadata_enabled = True
    run = collect_once(settings,db,transport=transport(count=123,store_status=503))
    assert run["status"] == "partial"
    assert run["request_count"] == 2
    assert db.app_detail(570,settings)["player_count"] == 123
    assert db.app_detail(570,settings)["name"] == "Steam app 570"
    assert db.status()["request_attempts_24h"] == {"webapi":1,"store":1}
    assert db.last_run()["status"] == "partial"
    with db.connection() as conn:
        outcomes = conn.execute("SELECT status FROM request_result ORDER BY completed_at").fetchall()
    assert [r["status"] for r in outcomes] == ["succeeded","failed"]


def test_failed_response_never_creates_zero_or_a_capture(scratch_database):
    db,settings = scratch_database
    run = collect_once(settings,db,transport=transport(status=503))
    assert run["status"] == "failed"
    row = db.app_detail(570,settings)
    assert row["player_count"] is None
    assert row["availability"] == "no_observations"
    assert row["last_attempt"]["error"]["code"] == "http_error"
    state = db.status()
    assert state["captures"] == state["player_samples"] == 0
    assert state["request_attempts_24h"]["webapi"] == 1
    assert state["uncertain_attempts"] == 0


def test_history_is_bounded_and_discloses_window_before_tracking(scratch_database):
    db,settings = scratch_database
    collect_once(settings,db,transport=transport(count=42))
    history = db.history(570,settings,hours=24)
    assert history["points"][0]["player_count"] == 42
    assert history["coverage"]["sample_count"] == 1
    assert history["coverage"]["requested_seconds"] == 86400
    assert history["coverage"]["coverage_ratio"] < .01
    assert history["metrics"]["observed_peak"] == 42
    assert history["gaps"][0]["seconds"] > 86000
    assert db.history(999,settings,1) is None
    with pytest.raises(QueryLimitError):
        db.history(570,settings,settings.web.max_history_days*24+1)


def test_history_point_limit_rejects_instead_of_silently_truncating(scratch_database):
    db,settings = scratch_database
    settings.web.max_points = 10
    for count in range(11):
        collect_once(settings,db,transport=transport(count=count))
    with pytest.raises(QueryLimitError,match="no observations were truncated"):
        db.history(570,settings,1)
    assert db.app_detail(570,settings)["sample_count"] == 11


def test_history_window_excludes_end_and_includes_start(scratch_database,monkeypatch):
    from datetime import datetime,timedelta
    db,settings = scratch_database
    collect_once(settings,db,transport=transport(count=99))
    with db.connection() as conn:
        boundary = conn.execute("SELECT observed_at FROM player_sample").fetchone()["observed_at"]
    class FixedClock(datetime):
        current = boundary
        @classmethod
        def now(cls,tz=None):
            return cls.current
    monkeypatch.setattr("game_census.db.datetime",FixedClock)
    older = db.history(570,settings,hours=1)
    assert older["points"] == []
    assert older["coverage"]["sample_count"] == 0
    assert older["metrics"]["observed_peak"] is None
    FixedClock.current = boundary + timedelta(hours=1)
    newer = db.history(570,settings,hours=1)
    assert len(newer["points"]) == 1
    assert newer["points"][0]["player_count"] == 99
    assert newer["coverage"]["sample_count"] == 1
