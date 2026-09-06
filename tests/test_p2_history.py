"""Independent P2 fixtures for occurrence alignment and lossless metric rollups."""
from datetime import datetime, timedelta, timezone

import pytest

from game_census import metrics
from test_bootstrap import scratch_database

START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def sample(seconds, count):
    return {"observed_at": START + timedelta(seconds=seconds), "player_count": count}


def policy(seconds=0, interval=300):
    return {"started_at": START + timedelta(seconds=seconds), "interval_seconds": interval}


def test_expected_occurrences_follow_cadence_anchor_inside_half_open_window():
    result = metrics.calculate([], START + timedelta(seconds=1), START + timedelta(seconds=299), [policy()])
    assert result["coverage"]["expected_samples"] == 0


def test_occurrence_alignment_across_policy_change_and_exact_edges():
    result = metrics.calculate([], START + timedelta(seconds=299), START + timedelta(seconds=901),
                               [policy(), policy(600, 600)])
    # The only due occurrences are t=300 and the new policy's t=600.
    assert result["coverage"]["expected_samples"] == 2
    assert metrics.calculate([], START, START + timedelta(seconds=300), [policy()])["coverage"]["expected_samples"] == 1


def test_integral_extrema_times_and_equal_observations_remain_inspectable():
    result = metrics.calculate([sample(0, 10), sample(300, 10), sample(1500, 100)],
                               START, START + timedelta(seconds=1800), [policy()])
    assert result["metrics"]["integral_player_seconds"] == 39000
    assert result["coverage"]["covered_seconds"] == 1200
    assert result["coverage"]["sample_count"] == 3
    assert result["metrics"]["observed_peak_at"] == sample(1500, 0)["observed_at"].isoformat()
    assert result["metrics"]["observed_minimum_at"] == START.isoformat()
    assert result["metrics"]["observed_minimum"] == 10


@pytest.mark.parametrize("older,newer,absolute,percentage,status", [
    (10, 15, 5, 50, "available"), (20, 10, -10, -50, "available"),
    (0, 5, 5, None, "zero_baseline"), (0, 0, 0, None, "zero_baseline"),
])
def test_growth_uses_adjacent_average_ccu_windows(older, newer, absolute, percentage, status):
    result = metrics.growth([sample(0, older), sample(300, newer)],
                            START + timedelta(seconds=300), START + timedelta(seconds=600), [policy()])
    assert result["older_average"] == older
    assert result["newer_average"] == newer
    assert result["absolute_change"] == absolute
    assert result["percentage_change"] == percentage
    assert result["status"] == status
    assert result["older_coverage_ratio"] == result["newer_coverage_ratio"] == 1
    assert result["comparison_to"] == result["from"]


def test_growth_requires_coverage_of_entire_requested_window_including_pre_enrollment():
    samples = [sample(600, 10), sample(1200, 20), sample(1800, 20)]
    result = metrics.growth(samples, START + timedelta(seconds=1200), START + timedelta(seconds=2400), [policy(600)])
    assert result["older_average"] == 10
    assert result["newer_average"] == 20
    assert result["older_coverage_ratio"] == 0.5
    assert result["newer_coverage_ratio"] == 1
    assert result["status"] == "insufficient_coverage"
    assert result["absolute_change"] is result["percentage_change"] is None
    allowed = metrics.growth(samples, START + timedelta(seconds=1200), START + timedelta(seconds=2400),
                             [policy(600)], minimum_coverage_ratio=0.5)
    assert allowed["status"] == "available"
    assert allowed["percentage_change"] == 100
    assert allowed["metric_policy_version"] != result["metric_policy_version"]


def test_growth_without_covered_observations_stays_null_even_at_zero_threshold():
    result = metrics.growth([], START, START + timedelta(seconds=300), [policy()], minimum_coverage_ratio=0)
    assert result["status"] == "no_observations"
    assert result["older_average"] is result["newer_average"] is None
    assert result["absolute_change"] is result["percentage_change"] is None


@pytest.mark.parametrize("threshold", [-0.01, 1.01, float("nan")])
def test_growth_rejects_invalid_threshold(threshold):
    with pytest.raises(ValueError, match="metrics.min_coverage_ratio"):
        metrics.growth([], START, START + timedelta(seconds=300), [], minimum_coverage_ratio=threshold)


def test_rollups_clip_utc_midnight_edges_and_keep_pre_window_carry():
    samples = [sample(-300, 10), sample(300, 20), sample(600, 0)]
    start, end = START - timedelta(seconds=180), START + timedelta(seconds=900)
    buckets = metrics.rollup(samples, start, end, [policy(-600)], 600)
    assert [(row["from"], row["to"]) for row in buckets] == [
        (start.isoformat(), START.isoformat()),
        (START.isoformat(), (START + timedelta(seconds=600)).isoformat()),
        ((START + timedelta(seconds=600)).isoformat(), end.isoformat()),
    ]
    assert [row["integral_player_seconds"] for row in buckets] == [1800, 9000, 0]
    assert [row["covered_seconds"] for row in buckets] == [180, 600, 300]
    assert [row["sample_count"] for row in buckets] == [0, 1, 1]
    assert buckets[0]["first"] is buckets[0]["maximum"] is None
    assert buckets[2]["minimum"]["player_count"] == 0


def test_unequal_rollups_sum_integrals_and_durations_never_bucket_averages():
    samples = [sample(0, 10), sample(600, 100)]
    end = START + timedelta(seconds=900)
    buckets = metrics.rollup(samples, START, end, [policy()], 600)
    assert [row["average_observed_ccu"] for row in buckets] == [10, 100]
    assert sum(row["integral_player_seconds"] for row in buckets) == 36000
    assert sum(row["covered_seconds"] for row in buckets) == 900
    assert metrics.calculate(samples, START, end, [policy()])["metrics"]["average_observed_ccu"] == 40


def test_rollups_replay_late_arrival_order_and_preserve_outage_integral():
    samples = [sample(3000, 100), sample(0, 10)]
    end = START + timedelta(seconds=3600)
    first = metrics.rollup(samples, START, end, [policy()], 600)
    assert first == metrics.rollup(list(reversed(samples)), START, end, [policy()], 600)
    assert sum(row["integral_player_seconds"] for row in first) == 66000
    assert sum(row["covered_seconds"] for row in first) == 1200
    assert sum(gap["seconds"] for row in first for gap in row["gaps"]) == 2400
    assert first[1]["average_observed_ccu"] is None


def test_rollups_preserve_cadence_change_boundary_and_integral():
    samples = [sample(0, 10), sample(600, 20)]
    end = START + timedelta(seconds=1800)
    tracking = [policy(), policy(600, 600)]
    buckets = metrics.rollup(samples, START, end, tracking, 700)
    assert sum(row["integral_player_seconds"] for row in buckets) == 30000
    assert sum(row["covered_seconds"] for row in buckets) == 1800
    direct = metrics.calculate(samples, START, end, tracking)
    assert direct["metrics"]["integral_player_seconds"] == 30000


def test_downsampling_retains_original_peak_minimum_first_last_and_counts():
    samples = [sample(index*60, 10) for index in range(120)]
    samples[17]["player_count"] = 999
    samples[37]["player_count"] = 0
    display = metrics.downsample(samples, START, START + timedelta(seconds=7200), [policy()], 12)
    assert display["resolution"] == "bucketed"
    assert len(display["points"]) <= 12
    assert display["points"][0]["observed_at"] == START.isoformat()
    assert display["points"][-1]["observed_at"] == samples[-1]["observed_at"].isoformat()
    assert {row["player_count"] for row in display["points"]} == {0, 10, 999}
    original = {(row["observed_at"].isoformat(), row["player_count"]) for row in samples}
    assert all((row["observed_at"], row["player_count"]) in original for row in display["points"])
    assert sum(row["sample_count"] for row in display["rollups"]) == 120


def test_downsampling_preserves_samples_adjacent_to_each_outage():
    samples = [sample(index*60, 10) for index in range(20)] + [sample(3600+index*60, 20) for index in range(20)]
    display = metrics.downsample(samples, START, START + timedelta(seconds=4800), [policy()], 10)
    times = {row["observed_at"] for row in display["points"]}
    assert sample(1140, 0)["observed_at"].isoformat() in times
    assert sample(3600, 0)["observed_at"].isoformat() in times
    assert sum(gap["seconds"] for row in display["rollups"] for gap in row["gaps"]) == 1860


def test_too_many_required_gap_boundaries_fail_instead_of_hiding_outages():
    samples = [sample(index*1200, index) for index in range(12)]
    with pytest.raises(ValueError, match="gap boundaries exceed web.max_points"):
        metrics.downsample(samples, START, START + timedelta(seconds=14400), [policy()], 10)


def test_already_bounded_auto_history_returns_all_raw_observations():
    display = metrics.downsample([sample(0, 0), sample(300, 0)], START, START + timedelta(seconds=600), [policy()], 10)
    assert display["resolution"] == "raw"
    assert len(display["points"]) == 2
    assert display["bucket_seconds"] is None
    assert display["rollups"] == []


def _record_points(db, start, counts, interval=300):
    """Use shipped persistence boundaries in an isolated schema, no live HTTP."""
    import json
    from game_census.sources import players
    from game_census.sources.base import Capture

    run = db.start_run([570], [players.SOURCE])
    for index, count in enumerate(counts):
        at = start + timedelta(seconds=index*interval)
        payload = json.dumps({"response": {"result": 1, "player_count": count}}).encode()
        attempt = db.reserve_attempt(run, 570, players.SOURCE, "webapi", 1000, 1)
        db.record_capture(run, attempt, Capture(players.SOURCE, players.VERSION, 570, at, at, 200,
                                               {"appid": 570}, payload, "exact_response", count))


@pytest.mark.integration
def test_database_auto_history_preserves_peak_growth_and_replay(scratch_database, monkeypatch):
    from game_census.db import QueryLimitError

    db, settings = scratch_database
    settings.web.max_points = 10
    start = datetime.now(timezone.utc) + timedelta(seconds=60)
    _record_points(db, start, [10]*12 + [20]*5 + [100] + [20]*6)

    class FixedClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return start + timedelta(hours=2)

    monkeypatch.setattr("game_census.db.datetime", FixedClock)
    before = db.rebuild()
    with pytest.raises(QueryLimitError, match="resolution=auto"):
        db.history(570, settings, hours=1)
    result = db.history(570, settings, hours=1, resolution="auto")
    assert result["resolution"] == "bucketed"
    assert len(result["points"]) <= 10
    assert result["coverage"]["sample_count"] == 12
    assert result["metrics"]["observed_peak"] == 100
    assert result["metrics"]["integral_player_seconds"] == 96000
    assert result["metrics"]["average_observed_ccu"] == pytest.approx(80/3)
    assert result["growth"]["older_average"] == 10
    assert result["growth"]["percentage_change"] == pytest.approx(500/3)
    assert sum(row["sample_count"] for row in result["rollups"]) == 12
    assert sum(row["integral_player_seconds"] for row in result["rollups"]) == 96000
    assert db.rebuild() == before
    assert db.history(570, settings, hours=1, resolution="auto") == result


@pytest.mark.integration
@pytest.mark.parametrize("carry_seconds", [0, 1])
def test_database_source_read_guard_counts_the_comparison_window(scratch_database, monkeypatch, carry_seconds):
    from game_census.db import QueryLimitError

    db, settings = scratch_database
    settings.web.max_history_samples = 100
    start = datetime.now(timezone.utc) + timedelta(seconds=60)
    # Either all 101 records lie in the comparison window, or 100 lie there
    # and one contributes pre-window carry. Both exceed the source-read bound.
    _record_points(db, start, [10]*101, interval=1)

    class FixedClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return start + timedelta(hours=2, seconds=carry_seconds)

    monkeypatch.setattr("game_census.db.datetime", FixedClock)
    with pytest.raises(QueryLimitError, match="comparison window exceed web.max_history_samples"):
        db.history(570, settings, hours=1, resolution="auto")


@pytest.mark.integration
def test_database_history_rejects_unknown_resolution(scratch_database):
    from game_census.db import QueryLimitError

    db, settings = scratch_database
    with pytest.raises(QueryLimitError, match="resolution must be raw or auto"):
        db.history(570, settings, hours=1, resolution="average")
