"""Exact virtual-time denominators; failures cannot disappear from coverage."""
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
import pytest
from game_census.config import Settings
from game_census.db import DatabaseError, QueryLimitError
from game_census.schedule_coverage import calculate, report


ZERO = datetime(2026, 1, 1, tzinfo=timezone.utc)


def at(seconds):
    return ZERO + timedelta(seconds=seconds)


def enabled(seconds=0, apps=(570,), cadence=300, plan="plan-a", epoch=1):
    return {"event_id": epoch, "recorded_at": at(seconds), "action": "enabled", "plan_hash": plan,
            "details": {"app_ids": list(apps), "cadence_seconds": cadence, "epoch": epoch}}


def sample(app_id, seconds, scheduled=None, plan="plan-a"):
    return {"app_id": app_id, "observed_at": at(seconds),
            "scheduled_at": None if scheduled is None else at(scheduled), "plan_hash": plan}


def test_virtual_day_keeps_app_with_no_samples_in_denominator():
    events = [enabled(apps=(570, 730))]
    samples = [sample(570, second, scheduled=second) for second in range(0, 86400, 300)]
    result = calculate(events, samples, at(0), at(86400), 2)
    assert result["tracked_app_seconds"] == 172800
    assert result["fresh_app_seconds"] == 86400
    assert result["freshness_ratio"] == .5
    assert result["expected_player_occurrences"] == 576
    assert result["observed_scheduled_occurrences"] == 288


def test_long_downtime_needs_no_materialized_jobs_to_count_every_missed_slot():
    result = calculate([enabled()], [], at(0), at(86400), 2)
    assert result["expected_player_occurrences"] == 288
    assert result["observed_scheduled_occurrences"] == 0
    assert result["tracked_app_seconds"] == 86400
    assert result["fresh_app_seconds"] == result["freshness_ratio"] == 0


def test_manual_carry_in_is_fresh_but_does_not_invent_scheduled_success():
    result = calculate([enabled(-3600)], [sample(570, -100)], at(0), at(3600), 2)
    assert result["fresh_app_seconds"] == 500
    assert result["expected_player_occurrences"] == 12
    assert result["observed_scheduled_occurrences"] == 0


def test_disable_and_replacement_cohort_clip_app_time_and_occurrences():
    events = [enabled(), {"event_id": 2, "recorded_at": at(600), "action": "disabled", "details": {}},
              enabled(900, apps=(730,), plan="plan-b", epoch=3)]
    result = calculate(events, [sample(570, 0), sample(570, 800), sample(730, 1000)], at(0), at(1200), 2)
    assert result["tracked_app_seconds"] == 900
    assert result["fresh_app_seconds"] == 800
    assert result["expected_player_occurrences"] == 3
    assert result["freshness_ratio"] == pytest.approx(8 / 9)


def test_cadence_change_uses_new_anchor_and_current_freshness_policy():
    events = [enabled(), enabled(300, cadence=600, plan="plan-b", epoch=2)]
    result = calculate(events, [sample(570, 0)], at(0), at(1200), 2)
    assert result["expected_player_occurrences"] == 3
    assert result["fresh_app_seconds"] == result["tracked_app_seconds"] == 1200


def test_occurrences_follow_half_open_window_and_ignore_duplicate_or_wrong_plan():
    observations = [sample(570, 100, scheduled=0), sample(570, 301, scheduled=300),
                    sample(570, 302, scheduled=300), sample(570, 320, scheduled=300, plan="wrong"),
                    sample(570, 350, scheduled=350), sample(730, 301, scheduled=300)]
    result = calculate([enabled()], observations, at(150), at(450), 2)
    assert result["expected_player_occurrences"] == result["observed_scheduled_occurrences"] == 1
    assert result["fresh_app_seconds"] == 300
    at_end = calculate([enabled()], [sample(570, 600, scheduled=600)], at(0), at(600), 2)
    assert at_end["expected_player_occurrences"] == 2
    assert at_end["observed_scheduled_occurrences"] == at_end["fresh_app_seconds"] == 0


def test_never_enabled_has_no_denominator_and_no_fabricated_ratio():
    result = calculate([], [sample(570, 0)], at(0), at(3600), 2)
    assert result["tracked_app_seconds"] == result["expected_player_occurrences"] == 0
    assert result["freshness_ratio"] is None


@pytest.mark.parametrize("bad", [{}, {"app_ids": [], "cadence_seconds": 300},
                                 {"app_ids": [570, 570], "cadence_seconds": 300},
                                 {"app_ids": [570], "cadence_seconds": 0}])
def test_invalid_canonical_enable_event_fails_instead_of_shrinking_denominator(bad):
    event = enabled()
    event["details"] = bad
    with pytest.raises(ValueError, match="enable event"):
        calculate([event], [], at(0), at(3600), 2)


class SnapshotDatabase:
    """Fixed read responses make snapshot ordering and bounds inspectable."""
    def __init__(self, event_count=0, sample_count=0, previous=None, carries=None):
        self.event_count, self.sample_count = event_count, sample_count
        self.previous, self.carries = previous, carries or []
        self.statements = []

    @contextmanager
    def connection(self):
        yield self

    def execute(self, sql, params=None):
        self.statements.append(sql)
        if len(self.statements) == 1:
            assert sql == "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
            return None
        if "clock_timestamp() AS now" in sql:
            rows = [{"now": at(3600)}]
        elif "count(*) AS n FROM schedule_event" in sql:
            rows = [{"n": self.event_count}]
        elif "ORDER BY recorded_at DESC" in sql:
            rows = [] if self.previous is None else [self.previous]
        elif "FROM schedule_event" in sql:
            rows = []
        elif "count(*) AS n FROM player_sample" in sql:
            rows = [{"n": self.sample_count}]
        elif "FROM unnest" in sql:
            rows = self.carries
        elif "FROM player_sample p LEFT JOIN" in sql:
            rows = []
        else:
            raise AssertionError("Unexpected SQL read")
        return type("Rows", (), {"fetchone": lambda self: rows[0] if rows else None,
                                  "fetchall": lambda self: rows})()


def configuration():
    return Settings.model_validate({"storage": {"database_url": "postgresql://fixture:fixture@db:5432/fixture"},
                                    "web": {"max_history_samples": 100}})


def test_report_uses_one_read_snapshot_with_carry_and_database_clock():
    db = SnapshotDatabase(previous=enabled(-3600), carries=[sample(570, -100)])
    result = report(configuration(), db, hours=1)
    assert result["from"] == at(0).isoformat()
    assert result["to"] == at(3600).isoformat()
    assert result["fresh_app_seconds"] == 500
    assert result["expected_player_occurrences"] == 12
    assert all(not sql.lstrip().startswith(("INSERT", "UPDATE", "DELETE")) for sql in db.statements)


def test_event_limit_fails_before_fetching_the_event_window():
    db = SnapshotDatabase(event_count=101)
    with pytest.raises(QueryLimitError, match="Schedule events exceed web.max_history_samples"):
        report(configuration(), db, hours=1)
    assert len(db.statements) == 4


def test_sample_limit_fails_without_returning_a_partial_denominator():
    db = SnapshotDatabase(previous=enabled(-3600), sample_count=101)
    with pytest.raises(QueryLimitError, match="observations exceed web.max_history_samples"):
        report(configuration(), db, hours=1)
    assert not any("FROM player_sample p LEFT JOIN" in sql for sql in db.statements)


def test_invalid_retained_event_reports_safe_actionable_storage_failure():
    bad = enabled()
    bad["details"] = {"app_ids": [], "cadence_seconds": 300}
    db = SnapshotDatabase(previous=bad)
    with pytest.raises(DatabaseError, match="no denominator was omitted"):
        report(configuration(), db, hours=1)


@pytest.mark.parametrize("hours", [0, True, -1, 8761])
def test_report_rejects_invalid_window_before_database_read(hours):
    db = SnapshotDatabase()
    with pytest.raises(QueryLimitError, match="hours must be an integer"):
        report(configuration(), db, hours=hours)
    assert db.statements == []
