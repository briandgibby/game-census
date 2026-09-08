"""Bounded synthetic storage workload; never contacts Steam or changes live rows.

The default suite uses three apps/three days. The shipped wrapper's --capacity
mode selects 25 apps/90 days (648,000 observations), in a fresh scratch schema.
These measurements describe the local test host, not production capacity.
"""
from datetime import datetime, timedelta, timezone
import json
import os
import statistics
import time
import uuid

import pytest
from psycopg.types.json import Jsonb

from game_census import cache, storage
from game_census.sources import players
from test_bootstrap import scratch_database

pytestmark = pytest.mark.integration


def test_partitioned_history_capacity(scratch_database, monkeypatch):
    db, settings = scratch_database
    extended = os.environ.get("GAME_CENSUS_CAPACITY") == "1"
    app_count, days = (settings.benchmark.app_count, settings.benchmark.history_days) if extended else (3, 3)
    app_ids = [570, *range(10001, 10000 + app_count)]
    settings.tracking.app_ids = app_ids
    db.initialize(app_ids, 300)
    end = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(days=days)
    month = storage.month_start(start)
    months = (end.year - month.year) * 12 + end.month - month.month + 1
    storage.maintain(settings, db, start_month=month.strftime("%Y-%m"), months=months)
    samples_per_app = days * 288
    total_samples = app_count * samples_per_app
    print(json.dumps({"operation": "synthetic_storage_fixture", "apps": app_count, "days": days,
                      "samples": total_samples, "steam_requests": 0, "destination": db.schema}), flush=True)
    began = time.perf_counter()
    run = uuid.uuid4()
    with db.connection() as conn:
        conn.execute("UPDATE tracking_interval SET started_at=%s", (start,))
        conn.execute("INSERT INTO collection_run(run_id,started_at,app_ids,sources) VALUES(%s,%s,%s,%s)",
                     (run, start, Jsonb(app_ids), Jsonb([players.SOURCE])))
        # Fixture owner: one deterministic count for each five-minute slot.
        # The same IDs/timestamps/payloads feed all canonical and derived rows.
        conn.execute("""CREATE TEMP TABLE capacity_fixture ON COMMIT DROP AS
            SELECT gen_random_uuid() capture_id,gen_random_uuid() attempt_id,a.app_id,
                %s::timestamptz + n * interval '300 seconds' observed_at,
                100 + a.ordinality - 1 + n %% 12 player_count
            FROM unnest(%s::bigint[]) WITH ORDINALITY a(app_id,ordinality)
            CROSS JOIN generate_series(0,%s) n""", (start, app_ids, samples_per_app - 1))
        conn.execute("""INSERT INTO request_attempt(attempt_id,run_id,app_id,source,host_group,dispatched_at)
            SELECT attempt_id,%s,app_id,%s,'webapi',observed_at FROM capacity_fixture""", (run, players.SOURCE))
        conn.execute("""INSERT INTO capture_identity(capture_id,attempt_id,received_at)
            SELECT capture_id,attempt_id,observed_at FROM capacity_fixture""")
        conn.execute("""INSERT INTO capture(capture_id,attempt_id,run_id,app_id,source,source_version,
            request_started_at,received_at,http_status,parameters,payload,checksum,capture_form)
            SELECT capture_id,attempt_id,%s,app_id,%s,%s,observed_at,observed_at,200,
                   jsonb_build_object('appid',app_id),body,encode(sha256(body),'hex'),'exact_response'
            FROM (SELECT *,convert_to(json_build_object('response',json_build_object('result',1,
                'player_count',player_count))::text,'UTF8') body FROM capacity_fixture) source""", (run, players.SOURCE, players.VERSION))
        conn.execute("""INSERT INTO player_sample(capture_id,app_id,observed_at,player_count,parser_version)
            SELECT capture_id,app_id,observed_at,player_count,%s FROM capacity_fixture""", (players.VERSION,))
        conn.execute("""INSERT INTO request_result(attempt_id,completed_at,status,http_status)
            SELECT attempt_id,observed_at,'succeeded',200 FROM capacity_fixture""")
        conn.execute("INSERT INTO run_completion(run_id,finished_at,status,report) VALUES(%s,%s,'succeeded',%s)",
                     (run, end, Jsonb({"status": "succeeded", "synthetic": True, "samples": total_samples})))
    load_seconds = time.perf_counter() - began

    class FixedClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return end

    monkeypatch.setattr("game_census.db.datetime", FixedClock)
    cold_started = time.perf_counter()
    cold = db.history(570, settings, hours=days * 24, resolution="auto")
    cold_seconds = time.perf_counter() - cold_started
    assert cold["coverage"]["sample_count"] == samples_per_app
    assert cold["coverage"]["coverage_ratio"] == 1
    assert cold["metrics"]["average_observed_ccu"] == pytest.approx(105.5)
    assert cold["metrics"]["integral_player_seconds"] == pytest.approx(105.5 * days * 86400)
    assert cold["metrics"]["observed_peak"] == 111
    assert cold["metrics"]["observed_minimum"] == 100
    assert len(cold["points"]) <= settings.web.max_points
    assert not cold["gaps"]
    timings = []
    for _ in range(10):
        began = time.perf_counter()
        assert db.history(570, settings, hours=days * 24, resolution="auto") == cold
        timings.append(time.perf_counter() - began)
    began = time.perf_counter()
    apps = db.list_apps(settings)
    summary_seconds = time.perf_counter() - began
    assert len(apps) == app_count
    assert sum(app["sample_count"] for app in apps) == total_samples
    with db.connection() as conn:
        state = conn.execute("""SELECT (SELECT count(*) FROM capture) captures,
            (SELECT count(*) FROM player_sample) samples,
            (SELECT count(*) FROM capture WHERE checksum<>encode(sha256(payload),'hex')) bad_checksums""").fetchone()
        disk_bytes = conn.execute("""SELECT sum(pg_total_relation_size(c.oid))::bigint bytes
            FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname=current_schema() AND c.relkind='r'""").fetchone()["bytes"]
    assert state == {"captures": total_samples, "samples": total_samples, "bad_checksums": 0}
    if extended:
        from game_census.benchmark import measure
        from game_census.web import create_app
        from fastapi.testclient import TestClient
        from game_census import recovery
        client = TestClient(create_app(settings,db))
        def read(index):
            path = f'/api/v1/apps/{app_ids[index%app_count]}/players?hours={days*24}&resolution=auto' if index%2 else '/api/v1/apps'
            response=client.get(path)
            assert response.status_code == 200, 'Stored API query failed'
            return 'history' if index%2 else 'summary'
        load=measure(read,requests_per_second=settings.benchmark.requests_per_second,seconds=settings.benchmark.duration_seconds,workers=settings.benchmark.workers)
        print(json.dumps({'synthetic_api_load':load,'transport':'ASGI in-process; excludes socket/proxy overhead'}),flush=True)
        assert load['failed_requests']==0
        # Measured recovery uses the same full canonical dataset, with an immutable
        # archive and a fresh read-only scratch database; no primary deletion.
        from pathlib import Path
        settings.storage.backup_path=str(Path(settings.storage.backup_path)/('capacity-'+uuid.uuid4().hex))
        began=time.perf_counter(); saved=recovery.backup(settings,db)
        backup_seconds=time.perf_counter()-began
        began=time.perf_counter(); restored=recovery.restore_verify(settings,db,saved['backup_id'])
        print(json.dumps({'synthetic_full_size_recovery':{'backup_seconds':backup_seconds,'restore_seconds':time.perf_counter()-began,
            'archive_bytes':saved['archive_bytes'],'captures_verified':restored['captures_replayed'],'source_rows_lost':0,
            'backup_path':saved['backup_path'],'proof_id':restored['proof_id']}}),flush=True)
        assert restored['captures_replayed']==total_samples
    print(json.dumps({"status": "succeeded", "workload": "extended" if extended else "default",
                      "apps": app_count, "days": days, "samples": total_samples,
                      "fixture_load_seconds": load_seconds, "cold_history_seconds": cold_seconds,
                      "warm_history_p95_seconds": sorted(timings)[-1],
                      "warm_history_median_seconds": statistics.median(timings),
                      "summary_seconds": summary_seconds, "table_and_index_bytes": disk_bytes,
                      "bad_capture_checksums": 0, "production_performance_target_claimed": False}), flush=True)
