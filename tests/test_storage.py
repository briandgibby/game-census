"""Bounded partition/storage checks; every source response is an offline fixture."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime,timedelta,timezone
from pathlib import Path
from threading import Event
import uuid

import httpx
import pytest
from psycopg import sql
from psycopg.types.json import Jsonb

from game_census import storage
from game_census.db import DatabaseError
from game_census.sources import players
from test_bootstrap import SchemaDatabase,scratch_database,transport


def fixture_capture(at,count=7):
    with httpx.Client(transport=transport(count=count)) as client:
        capture=players.fetch(client,570,2000000)
    return replace(capture,request_started_at=at,received_at=at)


def store(db,at,count=7):
    run_id=db.start_run([570],[players.SOURCE])
    attempt=db.reserve_attempt(run_id,570,players.SOURCE,"webapi",90000,1)
    capture=fixture_capture(at,count)
    capture_id=db.record_capture(run_id,attempt,capture)
    db.finish_run({"run_id":run_id,"status":"succeeded","request_count":1,"apps":[]})
    return capture_id,attempt,run_id,capture


def test_month_boundaries_are_utc_and_cover_december_rollover():
    start=storage.month_start(datetime(2026,12,31,23,59,59,tzinfo=timezone.utc))
    assert storage.add_months(start,1)==datetime(2027,1,1,tzinfo=timezone.utc)
    with pytest.raises(DatabaseError,match="UTC offset"):
        storage.month_start(datetime(2026,1,1))


@pytest.mark.integration
def test_empty_bootstrap_is_partitioned_and_idempotent(scratch_database):
    db,settings=scratch_database
    with db.connection() as conn:
        kinds=conn.execute("SELECT relname,relkind FROM pg_class WHERE oid=ANY(ARRAY['capture'::regclass,'player_sample'::regclass]) ORDER BY relname").fetchall()
        assert kinds==[{"relname":"capture","relkind":"p"},{"relname":"player_sample","relkind":"p"}]
        assert conn.execute("SELECT count(*) AS n FROM capture_identity").fetchone()["n"]==0
    first=storage.plan(settings,db)
    db.initialize([570],300)
    second=storage.plan(settings,db)
    assert first["layout"]==second["layout"]=="partitioned"
    assert [p["partition"] for p in first["partitions"]]==[p["partition"] for p in second["partitions"]]
    with db.connection() as conn:
        assert conn.execute("SELECT to_regclass('capture_partitioned') AS r").fetchone()["r"] is None


@pytest.mark.integration
def test_late_captures_crossing_month_boundary_route_with_global_identity(scratch_database):
    db,settings=scratch_database
    store(db,datetime(2026,1,31,23,59,59,tzinfo=timezone.utc),10)
    store(db,datetime(2026,2,1,tzinfo=timezone.utc),20)
    with db.connection() as conn:
        capture_tables=conn.execute("SELECT DISTINCT tableoid::regclass::text AS name FROM capture ORDER BY name").fetchall()
        sample_tables=conn.execute("SELECT DISTINCT tableoid::regclass::text AS name FROM player_sample ORDER BY name").fetchall()
        assert capture_tables==[{"name":"capture_p202601"},{"name":"capture_p202602"}]
        assert sample_tables==[{"name":"player_sample_p202601"},{"name":"player_sample_p202602"}]
        assert conn.execute("SELECT count(*) AS n FROM capture_identity").fetchone()["n"]==2
    assert db.rebuild()["captures_replayed"]==2


@pytest.mark.integration
def test_attempt_uniqueness_survives_another_month(scratch_database):
    db,_=scratch_database
    capture_id,attempt,run_id,capture=store(db,datetime(2026,1,1,tzinfo=timezone.utc))
    with pytest.raises(DatabaseError):
        db.record_capture(run_id,attempt,replace(capture,received_at=datetime(2026,2,1,tzinfo=timezone.utc)))
    with db.connection() as conn:
        assert conn.execute("SELECT count(*) AS n FROM capture").fetchone()["n"]==1
        assert str(conn.execute("SELECT capture_id FROM capture_identity").fetchone()["capture_id"])==capture_id


@pytest.mark.integration
def test_capture_uuid_and_sample_timestamp_cannot_duplicate_across_partitions(scratch_database):
    db,_=scratch_database
    capture_id,_,_,_=store(db,datetime(2026,1,1,tzinfo=timezone.utc))
    with db.connection() as conn:
        storage.ensure_month(conn,datetime(2026,2,1,tzinfo=timezone.utc))
    with pytest.raises(DatabaseError):
        with db.connection() as conn:
            conn.execute("""INSERT INTO player_sample SELECT capture_id,app_id,'2026-02-01 UTC'::timestamptz,
                player_count,parser_version FROM player_sample WHERE capture_id=%s""",(capture_id,))
    with db.connection() as conn:
        assert conn.execute("SELECT count(*) AS n FROM player_sample").fetchone()["n"]==1


@pytest.mark.integration
def test_identity_registry_cannot_commit_without_payload(scratch_database):
    db,_=scratch_database
    run_id=db.start_run([570],[players.SOURCE])
    attempt=db.reserve_attempt(run_id,570,players.SOURCE,"webapi",90000,1)
    with pytest.raises(DatabaseError):
        with db.connection() as conn:
            conn.execute("INSERT INTO capture_identity(capture_id,attempt_id,received_at) VALUES(%s,%s,clock_timestamp())",(str(uuid.uuid4()),attempt))
    with db.connection() as conn:
        assert conn.execute("SELECT count(*) AS n FROM capture_identity").fetchone()["n"]==0


@pytest.mark.integration
def test_bounded_maintenance_is_idempotent_and_never_removes_old_capture(scratch_database):
    db,settings=scratch_database
    store(db,datetime(2020,1,1,tzinfo=timezone.utc))
    before=db.rebuild()
    planned=storage.plan(settings,db,start_month="2027-12",months=2)
    assert planned["maximum_new_tables"]==4
    first=storage.maintain(settings,db,start_month="2027-12",months=2)
    assert len(first["created_partitions"])==4
    assert storage.maintain(settings,db,start_month="2027-12",months=2)["created_partitions"]==[]
    assert before==db.rebuild()
    with pytest.raises(DatabaseError,match="storage.max_partition_months"):
        storage.maintain(settings,db,months=settings.storage.max_partition_months+1)
    with pytest.raises(DatabaseError,match="YYYY-MM"):
        storage.plan(settings,db,start_month="2027-13")


@pytest.mark.integration
def test_concurrent_first_captures_create_one_partition_pair(scratch_database):
    db,_=scratch_database
    at=datetime(2022,4,1,tzinfo=timezone.utc)
    runs=[]
    for count in (1,2):
        run_id=db.start_run([570],[players.SOURCE])
        attempt=db.reserve_attempt(run_id,570,players.SOURCE,"webapi",90000,1)
        runs.append((run_id,attempt,fixture_capture(at,count)))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda args:db.record_capture(*args),runs))
    assert len(set(results))==2
    with db.connection() as conn:
        assert conn.execute("SELECT count(*) AS n FROM capture WHERE tableoid='capture_p202204'::regclass").fetchone()["n"]==2


@pytest.mark.integration
def test_new_month_writer_and_cached_reader_share_lock_order(scratch_database,monkeypatch):
    from game_census.cache import lock_app
    db,_=scratch_database
    reader_locked,writer_started,ddl_finished=Event(),Event(),Event()
    original=storage.ensure_month
    def observed_partition_ddl(conn,received_at):
        result=original(conn,received_at)
        ddl_finished.set()
        return result
    monkeypatch.setattr(storage,"ensure_month",observed_partition_ddl)
    run_id=db.start_run([570],[players.SOURCE])
    attempt=db.reserve_attempt(run_id,570,players.SOURCE,"webapi",90000,1)
    capture=fixture_capture(datetime(2023,5,1,tzinfo=timezone.utc))
    def reader():
        with db.connection() as conn:
            lock_app(conn,570)
            reader_locked.set()
            assert writer_started.wait(5)
            # A correctly ordered writer waits on the app lock before DDL. An
            # inverted writer reaches DDL and then waits on this reader's lock.
            ddl_finished.wait(1)
            conn.execute("SELECT count(*) FROM player_sample").fetchone()
    def writer():
        assert reader_locked.wait(5)
        writer_started.set()
        return db.record_capture(run_id,attempt,capture)
    failures=[]
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures=[pool.submit(reader),pool.submit(writer)]
        for future in futures:
            try:
                future.result(timeout=20)
            except Exception as error:
                failures.append(type(error).__name__)
    assert failures==[],f"new-month collection and cached reading must not deadlock: {failures}"


@pytest.mark.integration
def test_noop_projection_replay_preserves_warm_cache_exactly(scratch_database):
    from game_census.cache import rebuild as rebuild_cache
    db,settings=scratch_database
    store(db,datetime.now(timezone.utc)-timedelta(hours=2))
    rebuild_cache(db,settings,app_id=570,hours=4)
    with db.connection() as conn:
        before=conn.execute("SELECT * FROM player_rollup_cache ORDER BY app_id,bucket_start").fetchall()
    assert before and all(row["valid"] for row in before)
    db.rebuild()
    with db.connection() as conn:
        after=conn.execute("SELECT * FROM player_rollup_cache ORDER BY app_id,bucket_start").fetchall()
    assert after==before,"a projection which already exists must not fire invalidation during replay"


def legacy_database(db):
    """Build only released legacy migrations in a fresh retained scratch schema."""
    schema="game_census_legacy_test_"+uuid.uuid4().hex
    with db.connection() as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    legacy=SchemaDatabase(db._dsn,schema)
    path=Path(storage.__file__).parent/"migrations"
    with legacy.connection() as conn:
        for name in ("001_initial.sql","002_source_cooldown.sql","003_scheduler.sql"):
            conn.execute((path/name).read_text())
        conn.execute("INSERT INTO app(app_id) VALUES(570)")
        conn.execute("INSERT INTO tracking_interval(app_id,interval_seconds) VALUES(570,300)")
        # Legacy fixture uses retained canonical bytes, exactly the old insert
        # contract. The production initializer performs the additive upgrade.
        run_id=str(uuid.uuid4())
        attempt=str(uuid.uuid4())
        capture_id=str(uuid.uuid4())
        capture=fixture_capture(datetime(2024,12,31,23,59,59,tzinfo=timezone.utc),42)
        conn.execute("INSERT INTO collection_run(run_id,app_ids,sources) VALUES(%s,'[570]',%s)",(run_id,Jsonb([players.SOURCE])))
        conn.execute("INSERT INTO request_attempt(attempt_id,run_id,app_id,source,host_group) VALUES(%s,%s,570,%s,'webapi')",(attempt,run_id,players.SOURCE))
        conn.execute("""INSERT INTO capture(capture_id,attempt_id,run_id,app_id,source,source_version,request_started_at,
            received_at,http_status,parameters,payload,checksum,capture_form) VALUES(%s,%s,%s,570,%s,%s,%s,%s,200,%s,%s,%s,%s)""",
                     (capture_id,attempt,run_id,players.SOURCE,players.VERSION,capture.request_started_at,capture.received_at,Jsonb(capture.parameters),capture.payload,capture.checksum,capture.capture_form))
        conn.execute("INSERT INTO player_sample(capture_id,app_id,observed_at,player_count,parser_version) VALUES(%s,570,%s,42,%s)",(capture_id,capture.received_at,players.VERSION))
        conn.execute("INSERT INTO request_result(attempt_id,status,http_status) VALUES(%s,'succeeded',200)",(attempt,))
        conn.execute("INSERT INTO run_completion(run_id,status,report) VALUES(%s,'succeeded',%s)",(run_id,Jsonb({"run_id":run_id,"status":"succeeded"})))
    legacy.initialize([570],300)
    return legacy


@pytest.mark.integration
def test_populated_legacy_upgrade_is_additive_and_keeps_reads_working(scratch_database):
    db,settings=scratch_database
    legacy=legacy_database(db)
    assert storage.plan(settings,legacy)["migration_requires_verified_backup"]
    assert storage.plan(settings,legacy)["layout"]=="legacy"
    assert legacy.app_detail(570,settings)["player_count"]==42
    assert legacy.rebuild()["captures_replayed"]==1
    with pytest.raises(DatabaseError,match="verified storage migration"):
        storage.maintain(settings,legacy)


@pytest.mark.integration
def test_populated_cutover_copies_exact_payloads_and_freezes_derived_legacy(scratch_database,monkeypatch):
    db,settings=scratch_database
    legacy=legacy_database(db)
    # Only the partition mechanics are isolated here. Recovery's integration
    # suite supplies actual immutable backup/restore proof for end-to-end use.
    import game_census.recovery as recovery
    seen=[]
    def verified(database,proof_id,*,conn=None,timeout_seconds=None):
        assert database is legacy and conn is not None
        seen.append(proof_id)
        return {"status":"verified","fixture":True}
    monkeypatch.setattr(recovery,"require_verified_snapshot",verified)
    before=legacy.rebuild()
    result=storage.migrate(settings,legacy,proof_id="fixture-proof")
    assert seen==["fixture-proof"]
    assert result["source_manifests"]==result["copied_manifests"]
    assert legacy.rebuild()==before
    assert legacy.app_detail(570,settings)["player_count"]==42
    assert storage.plan(settings,legacy)["layout"]=="partitioned"
    with pytest.raises(DatabaseError):
        with legacy.connection() as conn:
            conn.execute("UPDATE capture_legacy_004 SET source_version='fixture-attempted-edit'")
    legacy.initialize([570],300)
    store(legacy,datetime(2025,1,1,tzinfo=timezone.utc),43)
    assert legacy.rebuild()["captures_replayed"]==2
    rebuilt=storage.verify_legacy(settings,legacy)
    assert rebuilt["captures_replayed"]==1
    assert rebuilt["source_manifests"]==rebuilt["rebuilt_manifests"]
    assert rebuilt["scratch_schema"].startswith("game_census_legacy_verify_")


@pytest.mark.integration
def test_failed_proof_and_copy_conflict_leave_legacy_owner_intact(scratch_database,monkeypatch):
    db,settings=scratch_database
    legacy=legacy_database(db)
    import game_census.recovery as recovery
    def invalid(*args,**kwargs):
        raise DatabaseError("The verified backup does not match current source contents.")
    monkeypatch.setattr(recovery,"require_verified_snapshot",invalid)
    with pytest.raises(DatabaseError,match="does not match"):
        storage.migrate(settings,legacy,proof_id="stale")
    assert storage.plan(settings,legacy)["layout"]=="legacy"
    assert legacy.rebuild()["captures_replayed"]==1
    monkeypatch.setattr(recovery,"require_verified_snapshot",lambda *args,**kwargs:{"status":"verified","fixture":True})
    original_manifest=storage._copy_manifest
    def mismatched_copy(conn,table):
        result=original_manifest(conn,table)
        if table.endswith("_partitioned"):
            return {**result,"sha256":"0"*64}
        return result
    monkeypatch.setattr(storage,"_copy_manifest",mismatched_copy)
    with pytest.raises(DatabaseError,match="copies differ"):
        storage.migrate(settings,legacy,proof_id="fixture-copy-conflict")
    assert storage.plan(settings,legacy)["layout"]=="legacy"
    with legacy.connection() as conn:
        assert conn.execute("SELECT count(*) AS n FROM capture_identity").fetchone()["n"]==0
        assert conn.execute("SELECT count(*) AS n FROM capture_partitioned").fetchone()["n"]==0


@pytest.mark.integration
def test_native_verified_restore_authorizes_exact_legacy_cutover(scratch_database,tmp_path):
    from game_census import recovery
    db,settings=scratch_database
    legacy=legacy_database(db)
    settings.storage.backup_path=str(tmp_path/"legacy-recovery")
    before=legacy.rebuild()
    saved=recovery.backup(settings,legacy)
    proof=recovery.restore_verify(settings,legacy,saved["backup_id"])
    result=storage.migrate(settings,legacy,proof_id=proof["proof_id"])
    assert result["verified_backup"]["source_snapshot_sha256"]==saved["source_snapshot_sha256"]
    assert legacy.rebuild()==before
    assert storage.plan(settings,legacy)["layout"]=="partitioned"
    rebuilt=storage.verify_legacy(settings,legacy)
    assert rebuilt["source_manifests"]==rebuilt["rebuilt_manifests"]
    assert rebuilt["captures_replayed"]==1
