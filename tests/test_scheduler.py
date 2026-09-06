"""Scheduler proofs use synthetic Steam responses and retained scratch schemas."""
import uuid

import httpx
import pytest

from game_census import scheduler
from game_census.collector import collect_once
from game_census.config import Settings, Storage
from game_census.db import DatabaseError
from game_census.sources import SourceError, players
from test_bootstrap import SchemaDatabase, scratch_database, transport


def settings():
    return Settings(storage=Storage(database_url="postgresql://fixture:fixture@db:5432/fixture"))


def prepare(db, config):
    evidence = collect_once(config, db, transport=transport(count=11))
    scheduler.attest_manual(config, db, evidence)
    scheduler.acknowledge(config, db, evidence["run_id"])
    scheduler.enable(config, db)
    return evidence


def test_plan_has_stable_secret_free_identity_and_explicit_boundary_capacity():
    config = settings()
    first = scheduler.plan(config)
    config.storage.database_url = "postgresql://different:secret@other:5432/name"
    assert scheduler.plan(config)["plan_hash"] == first["plan_hash"]
    assert first["admitted"]
    assert first["budgets"]["webapi"]["rolling_24h_occurrence_ceiling"] == 289
    assert first["plan"]["concurrency"] == 1
    assert "secret" not in str(first)
    config.tracking.interval_seconds = 600
    assert scheduler.plan(config)["plan_hash"] != first["plan_hash"]


def test_plan_rejects_daily_quota_and_serialized_freshness():
    config = settings()
    config.quota.webapi_rolling_24h = 1
    assert not scheduler.plan(config)["admitted"]
    config = settings()
    config.tracking.app_ids = list(range(1,26))
    result = scheduler.plan(config)
    assert not result["admitted"]
    assert result["worst_case_cycle_seconds"] > 300


def test_plan_hash_changes_with_source_budget_retry_and_lease_policy():
    config = settings()
    original = scheduler.plan(config)["plan_hash"]
    for group, key, value in [("sources", "store_metadata_enabled", False),
                               ("quota", "webapi_rolling_24h", 80000),
                               ("http", "max_attempts", 2),
                               ("scheduler", "lease_seconds", 90)]:
        changed = config.model_copy(deep=True)
        setattr(getattr(changed, group), key, value)
        assert scheduler.plan(changed)["plan_hash"] != original


@pytest.mark.integration
def test_enable_requires_successful_matching_watched_manual_run(scratch_database):
    db,config = scratch_database
    with pytest.raises(DatabaseError, match="watched-run acknowledgment"):
        scheduler.enable(config,db)
    with pytest.raises(DatabaseError, match="No successful real manual run"):
        scheduler.acknowledge(config,db)
    evidence = prepare(db,config)
    assert scheduler.status(config,db)["acknowledged_run_id"] == evidence["run_id"]
    config.http.max_attempts = 2
    assert not scheduler.status(config,db)["enabled"]
    with pytest.raises(DatabaseError,match="does not cover"):
        scheduler.attest_manual(config,db,evidence)
    with pytest.raises(DatabaseError,match="watched-run acknowledgment"):
        scheduler.enable(config,db)


@pytest.mark.integration
def test_one_cycle_accepts_one_capture_and_repeated_run_never_duplicates(scratch_database):
    db,config = scratch_database
    prepare(db,config)
    report = scheduler.run(config,db,transport=transport(count=42))
    again = scheduler.run(config,db,transport=httpx.MockTransport(lambda request: pytest.fail("the same slot must not dispatch twice")))
    assert report["status"] == again["status"] == "succeeded"
    assert report["request_count"] == 1
    assert again["request_count"] == 0
    with db.connection() as conn:
        assert conn.execute("SELECT count(*) AS n FROM scheduled_job WHERE state='succeeded'").fetchone()["n"] == 1
        assert conn.execute("SELECT count(*) AS n FROM capture").fetchone()["n"] == 2
    with pytest.raises(DatabaseError,match="scheduled run cannot"):
        scheduler.attest_manual(config,db,report)


@pytest.mark.integration
def test_shared_manual_quota_can_prevent_scheduled_dispatch(scratch_database):
    db,config = scratch_database
    config.quota.webapi_rolling_24h = 290
    config.scheduler.retry_reserve = 0
    prepare(db,config)
    # Shared ledger already contains the manual request; exhaust quota without
    # changing the acknowledged configuration. These are synthetic attempts.
    with db.connection() as conn:
        conn.execute("""INSERT INTO request_attempt(attempt_id,run_id,app_id,source,host_group)
            SELECT gen_random_uuid(),r.run_id,570,%s,'webapi' FROM collection_run r,
            generate_series(1,%s) LIMIT %s""",
                     (players.SOURCE, config.quota.webapi_rolling_24h, config.quota.webapi_rolling_24h))
    report = scheduler.run(config,db,transport=httpx.MockTransport(lambda request: pytest.fail("quota must block network")))
    assert report["status"] == "failed"
    assert report["request_count"] == 0
    assert report["outcomes"][0]["error"]["code"] == "quota_exhausted"


@pytest.mark.integration
def test_global_collection_lock_prevents_two_workers_before_request(scratch_database):
    db,config = scratch_database
    prepare(db,config)
    other = SchemaDatabase(config.storage.database_url.get_secret_value(),db.schema)
    with db.collection_lock():
        with pytest.raises(DatabaseError,match="collector is active"):
            scheduler.run(config,other,transport=transport())


@pytest.mark.integration
def test_retry_is_new_charged_attempt_but_single_accepted_occurrence(scratch_database,monkeypatch):
    db,config = scratch_database
    config.http.max_attempts = 2
    prepare(db,config)
    monkeypatch.setattr(scheduler.random,"uniform",lambda lower,upper:0)
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(503) if len(calls)==1 else httpx.Response(200,json={"response":{"result":1,"player_count":0}})
    report = scheduler.run(config,db,transport=httpx.MockTransport(respond))
    assert report["status"] == "succeeded"
    assert report["attempts"] == {"attempts":2,"succeeded":1,"failed":1,"uncertain":0}
    with db.connection() as conn:
        row = conn.execute("SELECT state,attempt_count,capture_id FROM scheduled_job").fetchone()
    assert row["state"] == "succeeded" and row["attempt_count"] == 2 and row["capture_id"]


@pytest.mark.integration
def test_disable_during_fetch_fences_late_result_and_retains_uncertain_charge(scratch_database):
    db,config = scratch_database
    prepare(db,config)
    def respond(request):
        scheduler.disable(config,db)
        return httpx.Response(200,json={"response":{"result":1,"player_count":9}})
    report = scheduler.run(config,db,transport=httpx.MockTransport(respond))
    assert report["status"] == "failed"
    assert report["attempts"]["uncertain"] == 1
    assert scheduler.status(config,db)["uncertain_attempts"] == 1
    with db.connection() as conn:
        assert conn.execute("SELECT count(*) AS n FROM capture").fetchone()["n"] == 1
        assert conn.execute("SELECT state FROM scheduled_job").fetchone()["state"] == "cancelled"


@pytest.mark.integration
def test_expired_slots_are_missed_and_only_fresh_slot_dispatches(scratch_database):
    db,config = scratch_database
    prepare(db,config)
    with db.connection() as conn:
        conn.execute("UPDATE schedule_state SET enabled_at=clock_timestamp()-interval '610 seconds',cursor_at=clock_timestamp()-interval '610 seconds'")
    report = scheduler.run(config,db,transport=transport(count=5))
    assert report["request_count"] == 1
    with db.connection() as conn:
        assert conn.execute("SELECT count(*) AS n FROM scheduled_job WHERE state='missed'").fetchone()["n"] == 2
        assert conn.execute("SELECT count(*) AS n FROM scheduled_job WHERE state='succeeded'").fetchone()["n"] == 1


@pytest.mark.integration
def test_expired_lease_is_reclaimable_and_old_worker_is_fenced(scratch_database):
    db,config = scratch_database
    prepare(db,config)
    state = scheduler.status(config,db)
    first_worker, second_worker = str(uuid.uuid4()), str(uuid.uuid4())
    scheduler._materialize(config,db,state["plan_hash"],state["epoch"])
    first = scheduler._claim(config,db,state["plan_hash"],state["epoch"],first_worker)
    with db.connection() as conn:
        conn.execute("UPDATE scheduled_job SET lease_until=clock_timestamp()-interval '1 second'")
    second = scheduler._claim(config,db,state["plan_hash"],state["epoch"],second_worker)
    assert second["lease_version"] == first["lease_version"]+1
    with db.connection() as conn:
        with pytest.raises(SourceError,match="lease or deadline"):
            scheduler._fence(conn,first,state["plan_hash"],state["epoch"],first_worker)


@pytest.mark.integration
def test_cancelled_run_has_durable_lifecycle_and_no_request(scratch_database):
    db,config = scratch_database
    prepare(db,config)
    report = scheduler.run(config,db,transport=transport(),cancelled=lambda:True)
    assert report["status"] == "failed"
    assert report["lifecycle_state"] == "cancelled"
    assert report["request_count"] == 0
    assert db.last_run()["cancellation"]["code"] == "run_cancelled"


@pytest.mark.integration
def test_long_downtime_materialization_is_bounded_and_current_first(scratch_database):
    db,config = scratch_database
    prepare(db,config)
    with db.connection() as conn:
        conn.execute("UPDATE schedule_state SET enabled_at=clock_timestamp()-interval '2 days',cursor_at=clock_timestamp()-interval '2 days'")
    state = scheduler.status(config,db)
    current,pending = scheduler._materialize(config,db,state["plan_hash"],state["epoch"])
    assert pending
    with db.connection() as conn:
        assert conn.execute("SELECT count(*) AS n FROM scheduled_job").fetchone()["n"] == 129
        latest = conn.execute("SELECT * FROM scheduled_job ORDER BY scheduled_at DESC LIMIT 1").fetchone()
    assert latest["scheduled_at"] == current
    assert latest["state"] == "pending"


@pytest.mark.integration
def test_retry_after_cannot_overrun_occurrence_deadline(scratch_database):
    db,config = scratch_database
    config.http.max_attempts=2
    prepare(db,config)
    report = scheduler.run(config,db,transport=httpx.MockTransport(lambda request:httpx.Response(429,headers={"Retry-After":"600"})))
    assert report["status"] == "failed"
    assert report["request_count"] == 1
    with db.connection() as conn:
        assert conn.execute("SELECT state FROM scheduled_job").fetchone()["state"] == "failed"
    assert db.status()["source_cooldowns"]
    repeat = scheduler.run(config,db,transport=httpx.MockTransport(lambda request:pytest.fail("failed current job cannot restart")))
    assert repeat["status"] == "failed"
    assert repeat["request_count"] == 0


@pytest.mark.integration
def test_repeated_schema_failures_stop_adapter_until_new_manual_evidence(scratch_database):
    db,config = scratch_database
    config.tracking.app_ids = [570,730]
    prepare(db,config)
    report = scheduler.run(config,db,transport=httpx.MockTransport(lambda request:httpx.Response(200,json={"response":{"result":1,"player_count":"invalid"}})))
    assert report["status"] == "failed" and report["request_count"] == 2
    assert scheduler.status(config,db)["stopped_adapters"][0]["error"]["code"] == "adapter_stopped"
    with pytest.raises(DatabaseError,match="new successful manual run"):
        scheduler.enable(config,db)
    prepare(db,config)
    assert scheduler.status(config,db)["stopped_adapters"] == []


@pytest.mark.integration
def test_exhausted_uncertain_reclaim_is_failed_without_sending_again(scratch_database):
    db,config = scratch_database
    prepare(db,config)
    state = scheduler.status(config,db)
    scheduler._materialize(config,db,state["plan_hash"],state["epoch"])
    worker = str(uuid.uuid4())
    job = scheduler._claim(config,db,state["plan_hash"],state["epoch"],worker)
    prior_run = db.start_run([570],[players.SOURCE])
    with db.connection() as conn:
        attempt = db.reserve_attempt(prior_run,570,players.SOURCE,"webapi",1000,1,conn=conn)
        conn.execute("INSERT INTO scheduled_attempt(attempt_id,job_id,lease_version) VALUES(%s,%s,%s)",(attempt,job["job_id"],job["lease_version"]))
        conn.execute("UPDATE scheduled_job SET attempt_count=1,lease_until=clock_timestamp()-interval '1 second'")
    report = scheduler.run(config,db,transport=httpx.MockTransport(lambda request:pytest.fail("all uncertain attempts have already been consumed")))
    assert report["status"] == "failed" and report["request_count"] == 0
    assert scheduler.status(config,db)["uncertain_attempts"] == 1
