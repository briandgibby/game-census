"""Bounded cohort policy and adoption; all source responses are synthetic."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from pydantic import ValidationError

from game_census import cohort, scheduler
from game_census.config import Cohort, Settings, Storage
from game_census.db import DatabaseError
from test_bootstrap import scratch_database
from test_catalog import collect, page

NOW = datetime(2026, 9, 1, tzinfo=timezone.utc)


def settings_for(**changes):
    settings = Settings(storage=Storage(database_url="postgresql://fixture:fixture@localhost/fixture"),
                        cohort=Cohort(enabled=True, **changes))
    settings.http.timeout_seconds = 1
    settings.sources.store_metadata_enabled = False
    return settings


def member(app_id, role, age=7200):
    return {"app_id": app_id, "role": role, "since": (NOW-timedelta(seconds=age)).isoformat()}


def previous(*members, age=7200, cursor=0):
    return {"members": list(members), "recorded_at": (NOW-timedelta(seconds=age)).isoformat(), "exploration_cursor": cursor}


def counts(*values, age=0):
    return [{"observed_at": NOW-timedelta(seconds=age+(len(values)-index-1)*300), "player_count": count}
            for index, count in enumerate(values)]


def test_empty_policy_pins_configured_game_and_reserves_bounded_exploration():
    settings = settings_for()
    result = cohort.select(settings, None, [10, 20, 30], {}, NOW)
    assert result["admitted"] and result["app_ids"] == [10, 570]
    assert {row["app_id"]: row["role"] for row in result["members"]} == {10: "exploration", 570: "pinned"}
    assert result["exploration_cursor"] == 10
    assert result["exploration_slots_reserved"] == result["exploration_slots_filled"] == 1
    assert settings.tracking.app_ids == [570]


def test_empty_catalog_remains_explicit_and_does_not_invent_a_game():
    result = cohort.select(settings_for(), None, [], {}, NOW)
    assert result["app_ids"] == [570] and result["candidate_count"] == 0
    assert result["exploration_slots_filled"] == 0


def test_promotion_and_demotion_replace_one_polling_slot_with_hysteresis():
    old = previous(member(570, "pinned"), member(620, "active"), member(400, "exploration"))
    result = cohort.select(settings_for(), old, [400, 730], {620: counts(0, 0, 0), 400: counts(1001, 1200, 1100)}, NOW)
    assert result["admitted"] and result["app_ids"] == [400, 570, 730]
    assert {row["app_id"]: row["role"] for row in result["members"]} == {400: "active", 570: "pinned", 730: "exploration"}
    assert {(row["app_id"], row["action"]) for row in result["changes"]} == {(620, "remove"), (400, "promote"), (730, "explore")}


@pytest.mark.parametrize("samples", [counts(0), counts(0, 200, 0), counts(100, 100, 100), counts(0, 0, 0, age=4000), []])
def test_insufficient_stale_or_middle_band_evidence_does_not_demote(samples):
    old = previous(member(570, "pinned"), member(620, "active"), member(400, "exploration", age=300))
    result = cohort.select(settings_for(), old, [], {620: samples}, NOW)
    assert 620 in result["app_ids"] and not result["changes"]


def test_residence_and_post_adoption_evidence_are_required():
    old = previous(member(570, "pinned"), member(400, "exploration", age=300))
    result = cohort.select(settings_for(min_residency_seconds=300), old, [400, 730], {400: counts(2000, 2000, 2000, age=301)}, NOW)
    assert all(row["action"] != "promote" for row in result["changes"])
    held = cohort.select(settings_for(), old, [400, 730], {400: counts(2000, 2000, 2000)}, NOW)
    assert held["app_ids"] == [400, 570] and not held["changes"]


def test_low_pinned_game_is_not_demoted_or_used_as_a_replacement():
    result = cohort.select(settings_for(), previous(member(570, "pinned")), [400], {570: counts(0, 0, 0)}, NOW)
    assert next(row for row in result["members"] if row["app_id"] == 570)["role"] == "pinned"


def test_duplicate_received_times_do_not_count_as_multiple_confirmations():
    samples = counts(2000)[0]
    result = cohort.select(settings_for(), previous(member(570, "pinned"), member(400, "exploration")), [], {400: [samples]*3}, NOW)
    assert all(row["action"] != "promote" for row in result["changes"])


def test_bounded_demotions_progress_instead_of_rejecting_all_low_members():
    settings = settings_for(max_apps=4)
    old = previous(member(570, "pinned"), member(610, "active"), member(620, "active"), member(400, "exploration"))
    result = cohort.select(settings, old, [400, 730], {610: counts(0, 0, 0), 620: counts(0, 0, 0)}, NOW)
    assert result["admitted"] and len([row for row in result["changes"] if row["action"] == "remove"]) == 1
    assert result["deferred"] and 400 in result["app_ids"]


def test_reduced_exploration_bound_never_returns_an_oversized_proposal():
    settings = settings_for(max_apps=2, max_replacements=25)
    old = previous(member(570, "pinned"), member(400, "exploration"), member(410, "exploration"), member(420, "exploration"))
    result = cohort.select(settings, old, [730, 740, 750], {}, NOW)
    assert len(result["members"]) <= settings.cohort.max_apps
    assert result["exploration_slots_filled"] <= settings.cohort.exploration_slots


def test_unpinning_changes_role_without_losing_existing_observations():
    settings = settings_for()
    settings.tracking.app_ids = [620]
    result = cohort.select(settings, previous(member(570, "pinned")), [400], {}, NOW)
    assert {row["app_id"]: row["role"] for row in result["members"]} == {400: "exploration", 570: "active", 620: "pinned"}


def test_reconcile_wait_and_unsafe_input_are_explicit_errors():
    old = previous(member(570, "pinned"), age=10)
    result = cohort.select(settings_for(), old, [], {}, NOW)
    assert not result["admitted"] and "cohort.reconcile_interval_seconds" in result["errors"][0]
    with pytest.raises(DatabaseError, match="duplicates"):
        cohort.select(settings_for(), None, [400, 400], {}, NOW)
    with pytest.raises(DatabaseError, match="malformed"):
        cohort.select(settings_for(), None, [400], {400: counts(-1)}, NOW)
    with pytest.raises(DatabaseError, match="max_apps"):
        settings = settings_for(max_apps=2)
        settings.tracking.app_ids = [570, 620]
        cohort.policy(settings)


@pytest.mark.parametrize("field,value", [("max_apps", 26), ("exploration_slots", 0), ("candidate_limit", 10001),
    ("max_evidence_samples", 49), ("min_samples", 1), ("evidence_max_age_seconds", 299),
    ("min_residency_seconds", 299), ("reconcile_interval_seconds", 299), ("promote_above", 0),
    ("demote_below", 1000), ("max_replacements", 0), ("discovery_webapi_reserve", 10001)])
def test_cohort_configuration_names_and_rejects_invalid_setting(field, value):
    with pytest.raises(ValidationError, match=field):
        Cohort(**{field: value})


def test_capacity_reserves_include_discovery_and_extra_watched_runs():
    settings = settings_for()
    value = scheduler.plan(settings)
    assert value["admitted"]
    assert value["budgets"]["webapi"]["cohort_attempt_reserve"] == 20 + 25
    assert value["budgets"]["store"]["cohort_attempt_reserve"] == 2
    settings.quota.webapi_rolling_24h = 330
    assert not scheduler.plan(settings)["admitted"]


@pytest.mark.integration
def test_plan_is_read_only_and_adoption_resolves_runtime_without_rewriting_config(scratch_database):
    db, settings = scratch_database
    collect(db, settings, lambda _: httpx.Response(200, json=page((400, 620, 730))))
    settings.cohort = Cohort(enabled=True)
    before = db.status()
    proposal = cohort.plan(settings, db)
    assert proposal["admitted"] and proposal["app_ids"] == [400, 570]
    assert cohort.latest(db) is None and db.status()["captures"] == before["captures"]
    adopted = cohort.adopt(settings, db)
    assert adopted["app_ids"] == [400, 570]
    assert settings.tracking.app_ids == [570]
    assert cohort.effective(settings, db).tracking.app_ids == [400, 570]
    assert db.status()["scheduler"] == "disabled"
    assert db.rebuild()["captures_replayed"] == before["captures"]
    with pytest.raises(DatabaseError, match="reconcile_interval_seconds"):
        cohort.adopt(settings, db)
    settings.cohort.promote_above += 1
    with pytest.raises(DatabaseError, match="policy changed"):
        cohort.effective(settings, db)


@pytest.mark.integration
def test_tracking_start_comes_from_its_interval_not_app_identity_creation(scratch_database, monkeypatch):
    db, settings = scratch_database
    collect(db, settings, lambda _: httpx.Response(200, json=page((400, 620))))
    settings.cohort = Cohort(enabled=True)
    at = datetime.now(timezone.utc)+timedelta(minutes=1)
    monkeypatch.setattr(cohort, "_now", lambda conn: at)
    cohort.adopt(settings, db)
    assert datetime.fromisoformat(db.app_detail(400, settings)["tracking_started_at"]) == at


@pytest.mark.integration
def test_stop_and_reopen_change_warm_tracking_coverage_without_deleting_history(scratch_database, monkeypatch):
    db, settings = scratch_database
    collect(db, settings, lambda _: httpx.Response(200, json=page((400, 620))))
    settings.cohort = Cohort(enabled=True)
    initial = datetime.now(timezone.utc)+timedelta(minutes=1)
    clock = [initial]
    monkeypatch.setattr(cohort, "_now", lambda conn: clock[0])
    cohort.adopt(settings, db)
    start = initial.replace(minute=0, second=0, microsecond=0)
    end = start+timedelta(hours=4)

    class FixedClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return end+timedelta(seconds=1)

    monkeypatch.setattr("game_census.db.datetime", FixedClock)
    warm = db.history_range(400, settings, start, end, "auto")
    assert warm["coverage"]["tracked_seconds"] > 3600
    clock[0] += timedelta(hours=1)
    rotated = cohort.adopt(settings, db)
    assert rotated["app_ids"] == [570, 620]
    stopped = db.history_range(400, settings, start, end, "auto")
    assert stopped["coverage"]["tracked_seconds"] == 3600
    assert db.app_detail(400, settings)["availability"] == "not_tracked"
    clock[0] += timedelta(hours=1)
    reopened = cohort.adopt(settings, db)
    assert reopened["app_ids"] == [400, 570]
    result = db.history_range(400, settings, start, end, "auto")
    assert result["coverage"]["tracked_seconds"] == pytest.approx(3600+(end-clock[0]).total_seconds())
    with db.connection() as conn:
        assert conn.execute("SELECT count(*) AS n FROM tracking_interval WHERE app_id=400").fetchone()["n"] == 2
        assert conn.execute("SELECT count(*) AS n FROM capture").fetchone()["n"] == 1


@pytest.mark.integration
def test_disabling_policy_requires_reconciliation_before_collecting(scratch_database):
    from game_census.collector import collect_once
    db, settings = scratch_database
    collect(db, settings, lambda _: httpx.Response(200, json=page((400,))))
    settings.cohort = Cohort(enabled=True)
    cohort.adopt(settings, db)
    settings.cohort.enabled = False
    with pytest.raises(DatabaseError, match="reconcile"):
        collect_once(settings, db, transport=httpx.MockTransport(lambda _: pytest.fail("Must reconcile before dispatch")))


@pytest.mark.integration
def test_disabled_reconciliation_stops_explorers_and_preserves_pinned_tracking(scratch_database, monkeypatch):
    db, settings = scratch_database
    collect(db, settings, lambda _: httpx.Response(200, json=page((400,))))
    settings.cohort = Cohort(enabled=True)
    at = datetime.now(timezone.utc)+timedelta(minutes=1)
    monkeypatch.setattr(cohort, "_now", lambda conn: at)
    cohort.adopt(settings, db)
    settings.cohort.enabled = False
    at += timedelta(hours=1)
    stopped = cohort.adopt(settings, db)
    assert stopped["app_ids"] == [570]
    assert db.app_detail(400, settings)["tracking_ended_at"] == at.isoformat()
    assert db.app_detail(570, settings)["tracking_ended_at"] is None


def test_small_catalog_pages_eventually_explore_every_candidate():
    settings = settings_for(candidate_limit=1)
    old, selected, now = None, [], NOW
    for _ in range(8):
        cursor = old["exploration_cursor"] if old else 0
        candidates = [value for value in (400, 570, 620, 730) if value > cursor]
        proposal = cohort.select(settings, old, (candidates or [400])[:1], {}, now)
        assert proposal["admitted"] and len(proposal["app_ids"]) <= 3
        selected.extend(row["app_id"] for row in proposal["members"] if row["role"] == "exploration")
        old = {**proposal, "recorded_at": now.isoformat()}
        now += timedelta(hours=1)
    assert set(selected) == {400, 620, 730}


@pytest.mark.integration
def test_adoption_is_atomic_and_serialized_with_collection(scratch_database, monkeypatch):
    db, settings = scratch_database
    collect(db, settings, lambda _: httpx.Response(200, json=page((400,))))
    settings.cohort = Cohort(enabled=True)
    with db.collection_lock(), pytest.raises(DatabaseError, match="Another collector"):
        cohort.adopt(settings, db)
    connection = db.connection

    class RejectTracking:
        def __init__(self, conn):
            self.conn = conn

        def execute(self, statement, *args, **kwargs):
            if isinstance(statement, str) and statement.startswith("INSERT INTO tracking_interval"):
                raise DatabaseError("Injected tracking write failure")
            return self.conn.execute(statement, *args, **kwargs)

    @contextmanager
    def fail_connection():
        with connection() as conn:
            yield RejectTracking(conn)

    monkeypatch.setattr(db, "connection", fail_connection)
    with pytest.raises(DatabaseError, match="Injected tracking"):
        cohort.adopt(settings, db)
    assert cohort.latest(db) is None
    assert [row["app_id"] for row in db.list_apps(settings)] == [570]
    assert db.status()["captures"] == 1


@pytest.mark.integration
def test_adopted_membership_drives_manual_scheduler_and_read_contracts(scratch_database, monkeypatch):
    from fastapi.testclient import TestClient
    from game_census.collector import collect_once
    from game_census.web import create_app
    from test_bootstrap import SchemaDatabase, transport
    from test_scheduler import prepare
    db, settings = scratch_database
    collect(db, settings, lambda _: httpx.Response(200, json=page((400, 620))))
    settings.cohort = Cohort(enabled=True)
    first = cohort.adopt(settings, db)
    restarted = SchemaDatabase(db._dsn, db.schema)
    assert cohort.effective(settings, restarted).tracking.app_ids == [400, 570]
    with pytest.raises(DatabaseError, match="adopted cohort"):
        collect_once(settings, db, [620], transport=transport())
    manual = prepare(db, settings)  # Mocked evidence; never a live acceptance claim.
    assert [row["app_id"] for row in manual["apps"]] == [400, 570]
    scheduled = scheduler.run(settings, db, transport=transport(count=29))
    assert scheduled["status"] == "succeeded" and scheduled["request_count"] == 2
    with pytest.raises(DatabaseError, match="schedule disable"):
        cohort.adopt(settings, db)
    client = TestClient(create_app(settings, db))
    state = client.get("/api/v1/status").json()
    assert state["schedule_state"] == "enabled" and state["tracked_apps"] == 2
    assert {row["app_id"]: row["role"] for row in state["cohort"]["members"]} == {400: "exploration", 570: "pinned"}
    assert {row["app_id"] for row in client.get("/api/v1/rankings").json()["items"]} == {400, 570}
    scheduler.disable(settings, db)
    at = datetime.fromisoformat(cohort.latest(db)["recorded_at"]) + timedelta(hours=1)
    monkeypatch.setattr(cohort, "_now", lambda conn: at)
    with pytest.raises(DatabaseError, match="changed after preview"):
        cohort.adopt(settings, db, expected_previous_id=first["event_id"]+1)
    assert cohort.latest(db)["id"] == first["event_id"]
    cohort.adopt(settings, db, expected_previous_id=first["event_id"])
    with pytest.raises(DatabaseError, match="watched-run acknowledgment"):
        scheduler.enable(settings, db)
    assert [row["app_id"] for row in collect_once(settings, db, transport=transport())["apps"]] == [570, 620]
    summary = client.get("/api/v1/apps/400").json()
    assert summary["availability"] == "not_tracked" and summary["sample_count"] == 2
    assert summary["tracking_ended_at"] == at.isoformat().replace("+00:00", "Z")
    series = client.get("/api/v1/compare?app_ids=400,620&hours=1").json()["series"]
    assert series[0]["availability"] == "not_tracked" and len(series[0]["history"]["points"]) == 2
    assert all("fixture-key" not in client.get(url).text for url in ("/api/v1/status", "/status", "/search", "/apps/400", "/compare?app_ids=400,620"))
    assert "Tracking stopped" in client.get("/status").text
    assert "Tracking stopped" in client.get("/search?q=400").text
    assert "Recorded observations remain available" in client.get("/apps/400").text


@pytest.mark.integration
def test_canonical_membership_and_tracking_stops_survive_verified_restore(scratch_database, tmp_path, monkeypatch):
    from game_census import recovery
    from game_census.collector import collect_once
    from test_bootstrap import transport
    db, settings = scratch_database
    settings.storage.backup_path = str(tmp_path)
    collect(db, settings, lambda _: httpx.Response(200, json=page((400, 620))))
    settings.cohort = Cohort(enabled=True)
    cohort.adopt(settings, db)
    collect_once(settings, db, transport=transport(count=7))
    at = datetime.fromisoformat(cohort.latest(db)["recorded_at"]) + timedelta(hours=1)
    monkeypatch.setattr(cohort, "_now", lambda conn: at)
    cohort.adopt(settings, db)
    before = recovery.current_manifest(db)
    saved = recovery.backup(settings, db)
    proof = recovery.restore_verify(settings, db, saved["backup_id"])
    scratch = recovery.ScratchDatabase(recovery._scratch_dsn(db._dsn, proof["scratch_database"]), db.schema)
    assert recovery.current_manifest(scratch)["contents_sha256"] == before["contents_sha256"]
    assert cohort.latest(scratch) == cohort.latest(db)
    assert cohort.effective(settings, scratch).tracking.app_ids == [570, 620]
    assert scratch.app_detail(400, settings)["tracking_ended_at"] == at.isoformat()
    assert scratch.app_detail(400, settings)["sample_count"] == 1
    assert proof["captures_replayed"] == proof["projections_verified"] == 3
    row = deepcopy(db.latest_cohort())
    row["members"][0]["role"] = "exploration"
    monkeypatch.setattr(db, "latest_cohort", lambda: row)
    with pytest.raises(DatabaseError, match="checksum"):
        cohort.effective(settings, db)


@pytest.mark.integration
def test_cli_plan_dry_run_apply_and_status_use_the_same_adoption(scratch_database, monkeypatch, capsys):
    import json
    from game_census import cli
    db, settings = scratch_database
    collect(db, settings, lambda _: httpx.Response(200, json=page((400,))))
    settings.cohort = Cohort(enabled=True)
    monkeypatch.setattr(cli, "load_settings", lambda _: settings)
    monkeypatch.setattr(cli, "Database", lambda _: db)
    assert cli.main(["cohort", "plan"]) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["app_ids"] == [400, 570] and preview["steam_requests"] == 0
    assert cli.main(["cohort", "reconcile", "--once", "--dry-run"]) == 0
    assert json.loads(capsys.readouterr().out)["app_ids"] == preview["app_ids"]
    assert cohort.latest(db) is None
    assert cli.main(["cohort", "reconcile", "--once", "--apply"]) == 0
    assert '"status": "adopted"' in capsys.readouterr().out
    assert cli.main(["cohort", "status"]) == 0
    assert json.loads(capsys.readouterr().out)["adoption"]["id"] == cohort.latest(db)["id"]
    assert cli.main(["schedule", "plan"]) == 0
    assert json.loads(capsys.readouterr().out)["app_ids"] == [400, 570]


@pytest.mark.integration
@pytest.mark.parametrize("surface", ["catalog", "summary"])
def test_enrollment_preserves_discovered_game_name(scratch_database, surface):
    db, settings = scratch_database
    collect(db, settings, lambda _: httpx.Response(200, json=page((400,), name="Fixture name")))
    settings.cohort = Cohort(enabled=True)
    cohort.adopt(settings, db)
    if surface == "catalog":
        assert [row["app_id"] for row in db.catalog("Fixture name")["items"]] == [400]
    else:
        assert db.app_detail(400, settings)["name"] == "Fixture name"


@pytest.mark.integration
@pytest.mark.parametrize("capacity", [2, 3])
def test_first_adoption_accounts_for_preexisting_static_tracking(scratch_database, capacity):
    db, settings = scratch_database
    db.initialize([400], 300)
    collect(db, settings, lambda _: httpx.Response(200, json=page((620,))))
    settings.cohort = Cohort(enabled=True, max_apps=capacity)
    result = cohort.adopt(settings, db)
    if capacity == 3:
        assert result["app_ids"] == [400, 570, 620]
        assert next(row for row in cohort.latest(db)["members"] if row["app_id"] == 400)["role"] == "active"
    else:
        assert result["app_ids"] == [570, 620]
        assert db.app_detail(400, settings)["tracking_ended_at"] is not None
