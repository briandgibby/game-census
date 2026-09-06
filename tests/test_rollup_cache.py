"""Persisted-bucket proofs use isolated PostgreSQL schemas and retained fixtures."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import threading

import pytest

from game_census import cache, metrics
from game_census.db import DatabaseError, QueryLimitError
from test_bootstrap import scratch_database
from test_p2_history import _record_points

pytestmark = pytest.mark.integration


def series(db, settings, hours=48, cadence=300):
    start = cache._floor(datetime.now(timezone.utc),3600)-timedelta(days=3)
    with db.connection() as conn:
        conn.execute("UPDATE tracking_interval SET started_at=%s,interval_seconds=%s WHERE app_id=570", (start,cadence))
    counts = [10+index % 12 for index in range(hours*3600//cadence)]
    _record_points(db,start,counts,interval=cadence)
    settings.web.max_points = 10
    return start,start+timedelta(hours=hours),counts


def expected(db, settings, start, end):
    with db.connection() as conn:
        rows = conn.execute("SELECT capture_id,observed_at,player_count FROM player_sample WHERE app_id=570 ORDER BY observed_at,capture_id").fetchall()
        tracking = conn.execute("SELECT started_at,interval_seconds FROM tracking_interval WHERE app_id=570 ORDER BY started_at,id").fetchall()
    return metrics.calculate(rows,start,end,tracking,settings.metrics.gap_cap_multiplier)


def test_warm_cache_consumes_exact_buckets_without_loading_source_values(scratch_database,monkeypatch):
    db,settings = scratch_database
    start,end,counts = series(db,settings)
    middle = start+timedelta(hours=24)
    cold_stats = {}
    cold = cache.history(db,settings,570,middle,end,"auto",cold_stats)
    exact = expected(db,settings,middle,end)
    assert cold["metrics"] == exact["metrics"]
    assert cold["coverage"] == exact["coverage"]
    assert cold["gaps"] == exact["gaps"]
    assert cold["metrics"]["average_observed_ccu"] == 15.5
    assert cold["growth"]["percentage_change"] == 0
    assert cold_stats["rebuilt_buckets"] == 48
    assert cold_stats["input_samples_loaded"] == len(counts)
    settings.web.max_history_samples = 100
    monkeypatch.setattr(cache,"_read_samples",lambda *args,**kwargs: pytest.fail("aligned warm auto history must consume persisted buckets"))
    warm_stats = {}
    warm = cache.history(db,settings,570,middle,end,"auto",warm_stats)
    assert warm == cold
    assert warm_stats["reused_buckets"] == 48
    assert warm_stats["input_samples_loaded"] == 0
    assert warm_stats["bounded_count_limit"] == 11


def test_raw_partial_edges_and_cached_interior_equal_direct_integral(scratch_database):
    db,settings = scratch_database
    start,end,_ = series(db,settings)
    middle = start+timedelta(hours=24)
    cache.history(db,settings,570,middle,end,"auto")
    start_window,end_window = middle-timedelta(seconds=127),end-timedelta(seconds=127)
    stats = {}
    result = cache.history(db,settings,570,start_window,end_window,"auto",stats)
    direct = expected(db,settings,start_window,end_window)
    assert result["metrics"] == direct["metrics"]
    assert result["coverage"] == direct["coverage"]
    assert result["gaps"] == direct["gaps"]
    assert sum(bucket["integral_player_seconds"] for bucket in result["rollups"]) == direct["metrics"]["integral_player_seconds"]
    assert stats["reused_buckets"] >= 46
    assert stats["raw_edge_pieces"] == 4
    assert stats["input_samples_loaded"] < 30


def test_late_arrival_invalidates_only_affected_buckets_and_preserves_peak(scratch_database):
    db,settings = scratch_database
    start,end,_ = series(db,settings)
    middle = start+timedelta(hours=24)
    before = cache.history(db,settings,570,middle,end,"auto")
    _record_points(db,start+timedelta(hours=28,seconds=30),[999])
    with db.connection() as conn:
        invalid = conn.execute("SELECT count(*) AS n FROM player_rollup_cache WHERE NOT valid").fetchone()["n"]
    assert invalid == 1
    stats = {}
    after = cache.history(db,settings,570,middle,end,"auto",stats)
    assert stats["rebuilt_buckets"] == 1
    assert stats["reused_buckets"] == 47
    assert after["metrics"] == expected(db,settings,middle,end)["metrics"]
    assert after["metrics"]["observed_peak"] == 999
    assert any(point["player_count"] == 999 for point in after["points"])
    assert before["coverage"]["sample_count"]+1 == after["coverage"]["sample_count"]


def test_empty_successor_bucket_is_invalidated_when_carry_changes(scratch_database):
    db,settings = scratch_database
    start = cache._floor(datetime.now(timezone.utc),3600)-timedelta(days=1)
    with db.connection() as conn:
        conn.execute("UPDATE tracking_interval SET started_at=%s,interval_seconds=3600 WHERE app_id=570", (start,))
    _record_points(db,start+timedelta(minutes=59),[10])
    middle,end = start+timedelta(hours=2),start+timedelta(hours=3)
    before = cache.history(db,settings,570,middle,end,"auto")
    assert before["coverage"]["sample_count"] == 0
    assert before["coverage"]["covered_seconds"] == 3540
    _record_points(db,start+timedelta(hours=1,seconds=30),[20])
    after = cache.history(db,settings,570,middle,end,"auto")
    assert after["coverage"]["sample_count"] == 0
    assert after["coverage"]["covered_seconds"] == 3600
    assert after["metrics"]["average_observed_ccu"] == 20


def test_tracking_and_metric_policy_changes_rebuild_from_the_owner(scratch_database):
    db,settings = scratch_database
    start,end,_ = series(db,settings,hours=2)
    middle = start+timedelta(hours=1)
    first = cache.history(db,settings,570,middle,end,"auto")
    with db.connection() as conn:
        old_revisions = [row["input_revision"] for row in conn.execute("SELECT input_revision FROM player_rollup_cache ORDER BY bucket_start").fetchall()]
        conn.execute("UPDATE tracking_interval SET interval_seconds=600 WHERE app_id=570")
        assert conn.execute("SELECT count(*) AS n FROM player_rollup_cache WHERE valid").fetchone()["n"] == 0
    second = cache.history(db,settings,570,middle,end,"auto")
    assert second["coverage"]["expected_samples"] == 6
    assert first["coverage"]["expected_samples"] == 12
    with db.connection() as conn:
        assert [row["input_revision"] for row in conn.execute("SELECT input_revision FROM player_rollup_cache ORDER BY bucket_start").fetchall()] != old_revisions
    settings.metrics.gap_cap_multiplier = 1.0
    stats = {}
    third = cache.history(db,settings,570,middle,end,"auto",stats)
    assert stats["rebuilt_buckets"] == 2
    assert third["metrics"]["metric_policy_version"] != second["metrics"]["metric_policy_version"]


def test_rebuild_recovers_corrupt_or_deleted_cache_without_changing_captures(scratch_database):
    db,settings = scratch_database
    start,end,_ = series(db,settings,hours=2)
    middle = start+timedelta(hours=1)
    expected_history = cache.history(db,settings,570,middle,end,"auto")
    manifest = db.rebuild()
    # First prove the regeneration command, then remove the derived cache.
    proof = cache.rebuild(db,settings,app_id=570,hours=72)
    assert proof["canonical_history_changed"] is False
    with db.connection() as conn:
        conn.execute("DELETE FROM player_rollup_cache")
    cache.rebuild(db,settings,app_id=570,hours=72)
    assert cache.history(db,settings,570,middle,end,"auto") == expected_history
    with db.connection() as conn:
        conn.execute("UPDATE player_rollup_cache SET payload='{}'::jsonb WHERE bucket_start=%s", (middle,))
    with pytest.raises(DatabaseError,match="integrity check"):
        cache.history(db,settings,570,middle,end,"auto")
    cache.rebuild(db,settings,app_id=570,hours=72)
    assert cache.history(db,settings,570,middle,end,"auto") == expected_history
    assert db.rebuild() == manifest


def test_cache_and_source_changes_serialize_without_publishing_a_stale_bucket(scratch_database,monkeypatch):
    db,settings = scratch_database
    start,end,_ = series(db,settings,hours=2)
    middle = start+timedelta(hours=1)
    reading,release,writing,finished = (threading.Event() for _ in range(4))
    original = cache._raw_part

    def pause(*args,**kwargs):
        if not reading.is_set():
            reading.set()
            assert release.wait(5), "test did not release the bounded cache builder"
        return original(*args,**kwargs)

    def writer():
        writing.set()
        _record_points(db,middle+timedelta(seconds=30),[999])
        finished.set()

    monkeypatch.setattr(cache,"_raw_part",pause)
    with ThreadPoolExecutor(max_workers=2) as pool:
        reader = pool.submit(cache.history,db,settings,570,middle,end,"auto")
        assert reading.wait(5)
        pending = pool.submit(writer)
        assert writing.wait(5)
        assert not finished.wait(0.1)
        release.set()
        reader.result(timeout=8)
        pending.result(timeout=8)
    current = cache.history(db,settings,570,middle,end,"auto")
    assert current["metrics"]["observed_peak"] == 999
    assert current["metrics"] == expected(db,settings,middle,end)["metrics"]


def test_cache_rebuild_limit_fails_without_partial_bucket_success(scratch_database):
    db,settings = scratch_database
    series(db,settings,hours=2)
    settings.cache.rebuild_max_buckets = 1
    with pytest.raises(QueryLimitError,match="cache.rebuild_max_buckets"):
        cache.rebuild(db,settings,app_id=570,hours=3)
    with db.connection() as conn:
        assert conn.execute("SELECT count(*) AS n FROM player_rollup_cache").fetchone()["n"] == 0


def test_projection_update_delete_and_replay_invalidate_cached_inputs(scratch_database):
    db,settings = scratch_database
    start,end,_ = series(db,settings,hours=2)
    middle = start+timedelta(hours=1)
    original = cache.history(db,settings,570,middle,end,"auto")
    manifest = db.rebuild()  # Prove regeneration before altering derived fixtures.
    with db.connection() as conn:
        identity = conn.execute("SELECT capture_id FROM player_sample WHERE observed_at=%s", (middle,)).fetchone()["capture_id"]
        conn.execute("UPDATE player_sample SET player_count=999 WHERE capture_id=%s", (identity,))
    changed = cache.history(db,settings,570,middle,end,"auto")
    assert changed["metrics"]["observed_peak"] == 999
    with db.connection() as conn:
        conn.execute("DELETE FROM player_sample WHERE capture_id=%s", (identity,))
    missing = cache.history(db,settings,570,middle,end,"auto")
    assert missing["coverage"]["sample_count"] == original["coverage"]["sample_count"]-1
    assert db.rebuild() == manifest
    assert cache.history(db,settings,570,middle,end,"auto") == original
