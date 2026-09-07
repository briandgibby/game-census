"""Injected failures prove diagnosability without disclosing exception contents."""
import json

import httpx
import pytest
from psycopg.errors import ConnectionFailure

from game_census import scheduler
from game_census.db import DatabaseError
from test_bootstrap import scratch_database, transport
from test_scheduler import prepare


SECRET = "postgresql://operator:fixture-secret@private-db/private-name"


def fail_storage(*args, **kwargs):
    try:
        raise ConnectionFailure(SECRET)
    except ConnectionFailure:
        raise DatabaseError("Storage failed: " + SECRET) from None


def assert_safe_failure(error, stage):
    diagnostic = error["diagnostic"]
    assert diagnostic["stage"] == stage
    chain = diagnostic["exception_chain"]
    assert [row["type"] for row in chain] == ["DatabaseError", "ConnectionFailure"]
    assert chain[1]["sqlstate"] == "08006"
    assert any(frame["module"] == "game_census.scheduler" for frame in chain[0]["frames"])
    assert SECRET not in json.dumps(error)
    assert "fixture-secret" not in json.dumps(error)


def test_interruption_is_durable_and_emitted_without_exception_text(scratch_database, monkeypatch, capsys):
    db, settings = scratch_database
    prepare(db, settings)
    monkeypatch.setattr(scheduler, "_materialize", fail_storage)
    report = scheduler.run(settings, db, transport=transport())
    assert report["status"] == "failed"
    assert report["request_count"] == 0
    assert_safe_failure(report["error"], "materialize")
    assert db.last_run()["error"] == report["error"]
    event = json.loads(capsys.readouterr().err)
    assert event["run_id"] == report["run_id"]
    assert event["error"] == report["error"]


def test_dispatch_interruption_keeps_uncertain_charge(scratch_database, monkeypatch, capsys):
    db, settings = scratch_database
    prepare(db, settings)
    monkeypatch.setattr(db, "record_capture", fail_storage)
    report = scheduler.run(settings, db, transport=transport())
    assert report["status"] == "failed"
    assert report["attempts"] == {"attempts": 1, "succeeded": 0, "failed": 0, "uncertain": 1}
    assert_safe_failure(report["error"], "dispatch")
    assert db.last_run()["error"] == report["error"]
    assert "fixture-secret" not in capsys.readouterr().err


def test_finalization_failure_is_reported_on_stderr_and_stops(scratch_database, monkeypatch, capsys):
    db, settings = scratch_database
    prepare(db, settings)
    monkeypatch.setattr(db, "finish_run", fail_storage)
    with pytest.raises(DatabaseError, match="report could not be persisted"):
        scheduler.run(settings, db, transport=transport())
    event = json.loads(capsys.readouterr().err)
    assert event["status"] == "failed"
    assert event["report_persisted"] is False
    assert event["run_id"]
    assert_safe_failure(event["error"], "persist_report")


def test_accounting_failure_retains_original_interruption(scratch_database, monkeypatch, capsys):
    db, settings = scratch_database
    prepare(db, settings)
    original_connection = db.connection

    def interrupt(*args):
        monkeypatch.setattr(db, "connection", fail_storage)
        fail_storage()

    monkeypatch.setattr(scheduler, "_materialize", interrupt)
    try:
        with pytest.raises(DatabaseError, match="report could not be persisted"):
            scheduler.run(settings, db, transport=httpx.MockTransport(lambda _: pytest.fail("No request expected")))
    finally:
        monkeypatch.setattr(db, "connection", original_connection)
    events = [json.loads(line) for line in capsys.readouterr().err.splitlines()]
    assert len(events) == 2
    assert_safe_failure(events[0]["error"], "materialize")
    assert_safe_failure(events[1]["error"], "account_attempts")
    assert events[1]["run_error"] == events[0]["error"]
    assert events[1]["report_persisted"] is False
