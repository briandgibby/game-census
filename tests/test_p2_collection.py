"""Shared admission and bounded request checks; no live Steam requests."""
import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path
import httpx
import pytest
from game_census.db import Database
from game_census.sources import SourceError, players
from game_census.sources.http import request_limits
from game_census.collector import collect_once, retry_delay
from game_census.config import Settings
from test_bootstrap import scratch_database, transport
from test_sources import MemoryLedger


def test_planning_probe_delegates_to_accounted_collection(monkeypatch):
    from game_census import cli
    calls = []
    monkeypatch.setattr(cli, "main", lambda args: calls.append(args) or 7)
    path = Path(__file__).resolve().parents[1] / "tools" / "probe_sources.py"
    spec = importlib.util.spec_from_file_location("probe_sources", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.main(["--config", "fixture.json", "--app-id", "570"]) == 7
    assert calls == [["--config", "fixture.json", "collect", "--once", "--app-id", "570"]]


def test_expired_request_cannot_dispatch():
    with httpx.Client(transport=httpx.MockTransport(lambda request: pytest.fail("expired work cannot dispatch"))) as client:
        with pytest.raises(SourceError) as caught:
            with request_limits(datetime.now(timezone.utc) - timedelta(seconds=1)):
                players.fetch(client, 570, 1024)
    assert caught.value.code == "source_deadline"


def test_cancelled_request_cannot_dispatch():
    with httpx.Client(transport=httpx.MockTransport(lambda request: pytest.fail("cancelled work cannot dispatch"))) as client:
        with pytest.raises(SourceError) as caught:
            with request_limits(datetime.now(timezone.utc) + timedelta(seconds=10), cancelled=lambda: True):
                players.fetch(client, 570, 1024)
    assert caught.value.code == "collection_cancelled"


def test_late_response_is_rejected_and_request_context_is_reset(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr("game_census.sources.http.time.monotonic", lambda: clock[0])
    def respond(request):
        clock[0] += 20
        return httpx.Response(200, json={"response": {"result": 1, "player_count": 7}})
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(SourceError) as caught:
            with request_limits(datetime.now(timezone.utc) + timedelta(seconds=10)):
                players.fetch(client, 570, 1024)
        assert caught.value.code == "source_deadline"
        assert players.fetch(client, 570, 1024).value == 7


def test_remaining_deadline_caps_http_waits():
    def respond(request):
        assert all(0 < seconds <= 2 for seconds in request.extensions["timeout"].values())
        return httpx.Response(200, json={"response": {"result": 1, "player_count": 7}})
    with httpx.Client(timeout=60, transport=httpx.MockTransport(respond)) as client:
        with request_limits(datetime.now(timezone.utc) + timedelta(seconds=2)):
            assert players.fetch(client, 570, 1024).value == 7


def test_retry_delay_has_bounded_jitter_and_respects_provider_minimum(monkeypatch):
    bounds = []
    monkeypatch.setattr("game_census.collector.random.uniform", lambda lower, upper: bounds.append((lower, upper)) or upper / 2)
    error = SourceError("temporary", "Temporary source failure.", "Retry later.", retryable=True)
    assert retry_delay(error, 0, 15) == .5
    assert retry_delay(error, 20, 15) == 7.5
    delayed = SourceError("temporary", "Temporary source failure.", "Retry later.", retryable=True, retry_after_seconds=60)
    assert retry_delay(delayed, 0, 15) == 60
    assert bounds == [(0, 1), (0, 15), (0, 1)]


def test_injected_manual_run_retains_plan_without_authorizing_schedule(monkeypatch):
    from game_census import scheduler
    monkeypatch.setattr(scheduler, "attest_manual", lambda *args: pytest.fail("Injected responses must not automatically authorize a schedule"))
    settings = Settings.model_validate({"storage": {"database_url": "postgresql://fixture:fixture@db:5432/fixture"},
                                       "sources": {"store_metadata_enabled": False}})
    ledger = MemoryLedger()
    report = collect_once(settings, ledger, transport=transport(count=8))
    assert report["status"] == "succeeded"
    assert report["source_transport"] == "injected"
    assert report["plan_hash"] == scheduler.plan(settings)["plan_hash"]
    assert ledger.reports == [report]


def test_interrupted_manual_run_keeps_uncertain_attempt_reportable():
    settings = Settings.model_validate({"storage": {"database_url": "postgresql://fixture:fixture@db:5432/fixture"},
                                       "sources": {"store_metadata_enabled": False}})
    ledger = MemoryLedger()
    def interrupted(request):
        raise KeyboardInterrupt()
    from game_census.db import DatabaseError
    with pytest.raises(DatabaseError, match="reserved requests remain charged"):
        collect_once(settings, ledger, transport=httpx.MockTransport(interrupted))
    assert len(ledger.attempts) == 1
    assert ledger.reports[0]["status"] == "failed"
    assert ledger.reports[0]["error"]["code"] == "collection_interrupted"


class AdmissionConnection:
    """Offline database clock responses; supplied transactions must not reconnect."""
    def __init__(self, remaining, elapsed):
        self.remaining, self.elapsed, self.statements = iter(remaining), elapsed, []
    def execute(self, statement, parameters=None):
        self.statements.append(statement)
        if " AS remaining" in statement:
            row = {"remaining": next(self.remaining)}
        elif " AS expires_at" in statement:
            row = {"expires_at": None}
        elif " AS n" in statement:
            row = {"n": 0}
        elif " AS elapsed" in statement:
            row = {"elapsed": self.elapsed}
        else:
            row = {}
        return type("Result", (), {"fetchone": lambda self: row})()


@pytest.mark.parametrize("remaining,elapsed", [([0], None), ([10, .5], 0), ([10, 10, 0], 0)])
def test_admission_cannot_charge_a_request_after_deadline(remaining, elapsed, monkeypatch):
    conn = AdmissionConnection(remaining, elapsed)
    monkeypatch.setattr("game_census.db.time.sleep", lambda seconds: None)
    with pytest.raises(SourceError) as caught:
        Database("unused").reserve_attempt("run", 570, players.SOURCE, "webapi", 100, 1,
                                            conn=conn, deadline=datetime.now(timezone.utc))
    assert caught.value.code == "source_deadline"
    assert not any("INSERT INTO request_attempt" in statement for statement in conn.statements)


@pytest.mark.integration
def test_capture_joins_the_scheduler_transaction(scratch_database):
    db, settings = scratch_database
    run = db.start_run([570], [players.SOURCE])
    attempt = db.reserve_attempt(run, 570, players.SOURCE, "webapi", 100, 1)
    with httpx.Client(transport=transport(count=22)) as client:
        capture = players.fetch(client, 570, 1024)
    with pytest.raises(RuntimeError, match="fence rejected"):
        with db.connection() as conn:
            db.record_capture(run, attempt, capture, conn=conn)
            raise RuntimeError("fence rejected")
    with db.connection() as conn:
        assert conn.execute("SELECT count(*) AS n FROM capture").fetchone()["n"] == 0
        assert conn.execute("SELECT count(*) AS n FROM player_sample").fetchone()["n"] == 0
        assert conn.execute("SELECT count(*) AS n FROM request_result").fetchone()["n"] == 0
    assert db.status()["uncertain_attempts"] == 1


@pytest.mark.integration
def test_reservation_joins_the_scheduler_transaction(scratch_database):
    db, settings = scratch_database
    run = db.start_run([570], [players.SOURCE])
    with pytest.raises(RuntimeError, match="fence rejected"):
        with db.connection() as conn:
            db.reserve_attempt(run, 570, players.SOURCE, "webapi", 100, 1, conn=conn)
            raise RuntimeError("fence rejected")
    assert db.status()["request_attempts_24h"]["webapi"] == 0
