"""Retained-ledger replay and restart checks; no live source and no destructive setup."""
import hashlib
import httpx
import pytest
from game_census.collector import collect_once
from game_census.db import DatabaseError
from game_census.sources import SourceError, players
from test_bootstrap import SchemaDatabase, scratch_database, transport

pytestmark = pytest.mark.integration


def test_quota_is_shared_across_new_database_instances(scratch_database):
    db,settings = scratch_database
    settings.quota.webapi_rolling_24h = 1
    first = collect_once(settings,db,transport=transport(count=5))
    restarted = SchemaDatabase(settings.storage.database_url.get_secret_value(),db.schema)
    second = collect_once(settings,restarted,transport=httpx.MockTransport(lambda r: pytest.fail("quota must prevent dispatch")))
    assert first["status"] == "succeeded"
    assert second["status"] == "failed"
    assert second["request_count"] == 0
    assert second["apps"][0]["sources"][0]["error"]["code"] == "quota_exhausted"
    assert restarted.status()["request_attempts_24h"]["webapi"] == 1
    assert restarted.app_detail(570,settings)["sample_count"] == 1


def test_rebuild_retains_captures_and_is_deterministic(scratch_database):
    db,settings = scratch_database
    settings.sources.store_metadata_enabled = True
    collect_once(settings,db,transport=transport(count=101))
    with db.connection() as conn:
        before = conn.execute("SELECT capture_id,payload,checksum FROM capture ORDER BY capture_id").fetchall()
    first = db.rebuild()
    second = db.rebuild()
    with db.connection() as conn:
        after = conn.execute("SELECT capture_id,payload,checksum FROM capture ORDER BY capture_id").fetchall()
    assert before == after
    assert all(hashlib.sha256(bytes(r["payload"])).hexdigest() == r["checksum"] for r in after)
    assert first == second
    assert first["captures_replayed"] == first["projections_verified"] == 2
    assert first["canonical_history_changed"] is False
    assert db.app_detail(570,settings)["name"] == "Fixture Game"


def test_uncertain_request_stays_charged_and_reportable(scratch_database):
    db,settings = scratch_database
    run_id = db.start_run([570],[players.SOURCE])
    db.reserve_attempt(run_id,570,players.SOURCE,"webapi",1,1)
    restarted = SchemaDatabase(settings.storage.database_url.get_secret_value(),db.schema)
    status = restarted.status()
    assert status["uncertain_attempts"] == 1
    assert status["last_run"]["status"] == "unfinished"
    assert status["last_run"]["next_action"]
    with pytest.raises(SourceError,match="budget is exhausted"):
        restarted.reserve_attempt(run_id,570,players.SOURCE,"webapi",1,1)


def test_concurrent_manual_collector_is_rejected_before_dispatch(scratch_database):
    db,settings = scratch_database
    other = SchemaDatabase(settings.storage.database_url.get_secret_value(),db.schema)
    with db.collection_lock():
        with pytest.raises(DatabaseError,match="Another collector is active"):
            collect_once(settings,other,transport=httpx.MockTransport(lambda r: pytest.fail("must not dispatch")))
    assert db.last_run() is None


def test_capture_failure_rolls_back_success_outcome_and_projection(scratch_database,monkeypatch):
    db,settings = scratch_database
    run_id = db.start_run([570],[players.SOURCE])
    attempt = db.reserve_attempt(run_id,570,players.SOURCE,"webapi",10,1)
    with httpx.Client(transport=transport(count=77)) as client:
        capture = players.fetch(client,570,2000000)
    def reject_projection(conn,capture):
        raise SourceError("fixture_projection_failure","Injected projection failure.","Inspect fixture.")
    monkeypatch.setattr("game_census.db.projections.project",reject_projection)
    with pytest.raises(SourceError,match="Injected projection failure"):
        db.record_capture(run_id,attempt,capture)
    with db.connection() as conn:
        counts = conn.execute("""SELECT (SELECT count(*) FROM capture) AS captures,
            (SELECT count(*) FROM player_sample) AS samples,(SELECT count(*) FROM request_result) AS outcomes""").fetchone()
    assert counts == {"captures":0,"samples":0,"outcomes":0}
    assert db.status()["uncertain_attempts"] == 1


def test_retry_after_survives_a_new_manual_run(scratch_database):
    db,settings = scratch_database
    first = collect_once(settings,db,transport=httpx.MockTransport(lambda r: httpx.Response(429,headers={"Retry-After":"120"})))
    assert first["status"] == "failed"
    restarted = SchemaDatabase(settings.storage.database_url.get_secret_value(),db.schema)
    second = collect_once(settings,restarted,transport=httpx.MockTransport(lambda r: pytest.fail("Retry-After must prevent another dispatch")))
    assert second["status"] == "failed"
    assert second["request_count"] == 0
    assert second["apps"][0]["sources"][0]["error"]["code"] == "source_cooldown"
    assert restarted.status()["request_attempts_24h"]["webapi"] == 1
