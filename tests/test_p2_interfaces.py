"""Operator activation and public history contracts; all data is synthetic."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from game_census import cli, metrics, scheduler
from game_census.config import ConfigurationError, Settings, Storage, load_settings
from test_api import fixture_app
from test_config_cli import write_config


@pytest.mark.parametrize(("section", "setting", "value"), [
    ("scheduler", "lease_seconds", 4), ("scheduler", "lease_seconds", 601),
    ("scheduler", "max_run_seconds", 4), ("scheduler", "max_run_seconds", 86401),
    ("scheduler", "poll_seconds", 0), ("scheduler", "poll_seconds", 61),
    ("scheduler", "retry_reserve", -1), ("scheduler", "retry_reserve", 10001),
    ("scheduler", "poll_seconds", True), ("metrics", "min_coverage_ratio", -.1),
    ("metrics", "min_coverage_ratio", 1.1), ("web", "max_history_samples", 99),
    ("web", "max_history_samples", 1000001),
])
def test_p2_settings_reject_untrusted_bounds(tmp_path, section, setting, value):
    with pytest.raises(ConfigurationError, match=section + r"\." + setting):
        load_settings(write_config(tmp_path, **{section: {setting: value}}))


def test_old_configuration_inherits_safe_scheduler_defaults(tmp_path):
    settings = load_settings(write_config(tmp_path))
    assert settings.scheduler.max_run_seconds == 3600
    assert "enabled" not in settings.scheduler.model_dump()
    assert settings.metrics.min_coverage_ratio == .9


def test_schedule_plan_is_read_only_and_prints_bounds(tmp_path, monkeypatch, capsys):
    # This object has no DB methods: any attempted I/O fails the test.
    monkeypatch.setattr(cli, "Database", lambda _: object())
    assert cli.main(["--config", str(write_config(tmp_path)), "schedule", "plan"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["app_ids"] == [570]
    assert result["one_cycle_attempt_ceiling"] == 2
    assert len(result["plan_hash"]) == 64


def test_infeasible_plan_fails_without_database_or_network(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "Database", lambda _: object())
    path = write_config(tmp_path, quota={"webapi_rolling_24h": 1})
    assert cli.main(["--config", str(path), "schedule", "plan"]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["admitted"] is False
    assert any("quota.webapi_rolling_24h" in error for error in result["errors"])


def test_acknowledgment_requires_explicit_watched_argument(capsys):
    with pytest.raises(SystemExit) as error:
        cli.main(["schedule", "acknowledge"])
    assert error.value.code == 2
    assert "--watched" in capsys.readouterr().err


def test_acknowledgment_passes_requested_run_to_scheduler(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "Database", lambda _: object())
    calls = []
    monkeypatch.setattr(scheduler, "acknowledge", lambda settings, db, run_id: calls.append(run_id) or {"status": "acknowledged"})
    assert cli.main(["--config", str(write_config(tmp_path)), "schedule", "acknowledge", "--watched", "--run-id", "fixture-run"]) == 0
    assert calls == ["fixture-run"]
    assert json.loads(capsys.readouterr().out)["status"] == "acknowledged"


def test_worker_rechecks_configuration_before_dispatch(tmp_path, monkeypatch, capsys):
    path = write_config(tmp_path)
    monkeypatch.setattr(cli, "Database", lambda _: object())

    def worker(settings, db, *, max_cycles, cancelled):
        assert not cancelled()
        contents = json.loads(path.read_text())
        contents["tracking"] = {"app_ids": [730]}
        path.write_text(json.dumps(contents))  # A disposable test config, owned here.
        assert cancelled()
        return {"status": "cancelled"}

    monkeypatch.setattr(scheduler, "run", worker)
    assert cli.main(["--config", str(path), "schedule", "run"]) == 1
    output = capsys.readouterr().out
    assert "effective collection plan changed" in output


@pytest.mark.parametrize("cycles", [0, 289])
def test_worker_cycle_limit_is_rejected_before_execution(tmp_path, monkeypatch, capsys, cycles):
    monkeypatch.setattr(cli, "Database", lambda _: object())
    monkeypatch.setattr(scheduler, "run", lambda *args, **kwargs: pytest.fail("Must reject before running"))
    assert cli.main(["--config", str(write_config(tmp_path)), "schedule", "run", "--max-cycles", str(cycles)]) == 1
    assert "max-cycles" in capsys.readouterr().err


def fixture_history():
    start = datetime(2026, 9, 4, tzinfo=timezone.utc)
    end = start + timedelta(hours=1)
    tracking = [{"started_at": start-timedelta(hours=1), "interval_seconds": 300}]
    samples = [{"observed_at": start+timedelta(seconds=offset), "player_count": 10 if offset < 0 else 20}
               for offset in range(-3600, 3600, 300)]
    calculated = metrics.calculate(samples, start, end, tracking)
    return {"app_id": 570, "from": start, "to": end,
            **metrics.downsample(samples, start, end, tracking, 10), **calculated,
            "growth": metrics.growth(samples, start, end, tracking),
            "source": "steam_current_players", "source_version": "1"}


def test_api_retains_rollups_growth_integrals_and_tracking_coverage(fixture_app):
    client, db, _ = fixture_app
    db.history_value = fixture_history()
    response = client.get("/api/v1/apps/570/history?hours=1&resolution=auto")
    assert response.status_code == 200
    body = response.json()
    assert body["resolution"] == "bucketed"
    assert body["growth"]["status"] == "available"
    assert body["growth"]["percentage_change"] == 100
    assert body["coverage"]["tracked_seconds"] == 3600
    assert body["metrics"]["integral_player_seconds"] == 72000
    assert sum(row["integral_player_seconds"] for row in body["rollups"]) == 72000
    assert body["metrics"]["observed_peak_at"]
    assert body["growth"]["metric_policy_version"]


def test_auto_resolution_reaches_database_and_bad_mode_is_rejected(fixture_app, monkeypatch):
    client, db, _ = fixture_app
    calls = []
    existing = db.history
    def read(app_id, settings, hours=24, resolution="raw"):
        calls.append(resolution)
        return existing(app_id, settings, hours, resolution)
    monkeypatch.setattr(db, "history", read)
    assert client.get("/api/v1/apps/570/history?resolution=auto").status_code == 200
    assert client.get("/api/v1/apps/570/history").status_code == 200
    assert client.get("/apps/570").status_code == 200
    assert calls == ["auto", "raw", "auto"]
    assert client.get("/api/v1/apps/570/history?resolution=arbitrary").status_code == 422
    assert len(calls) == 3


def test_chart_explains_reduction_and_growth_with_table_and_json(fixture_app):
    client, db, _ = fixture_app
    db.history_value = fixture_history()
    html = client.get("/apps/570?hours=1").text
    assert "original observations" in html
    assert "+100.0%" in html
    assert "Window statistics use the full retained series" in html
    assert "View observation data" in html
    assert "resolution=auto" in html


def test_low_coverage_never_displays_growth_percentage(fixture_app):
    client, db, _ = fixture_app
    db.history_value = fixture_history()
    db.history_value["growth"].update(status="insufficient_coverage", percentage_change=None, absolute_change=None, older_coverage_ratio=.1)
    html = client.get("/apps/570").text
    assert "Unavailable until both adjacent windows" in html
    assert "+100.0%" not in html


def test_status_exposes_activation_without_claiming_worker_is_running(fixture_app, monkeypatch):
    client, db, settings = fixture_app
    monkeypatch.setattr(db, "status", lambda: {"scheduler": "enabled", "scheduler_state": {"plan_hash": scheduler.plan(settings)["plan_hash"]}, "private_config": "never-public"})
    body = client.get("/api/v1/status").json()
    assert body["schedule_state"] == "enabled"
    assert body["collection_mode"] == "scheduled"
    html = client.get("/status").text
    assert "separately started, bounded collector command" in html
    assert "never-public" not in html


def test_status_invalidates_enabled_plan_after_configuration_changes(fixture_app, monkeypatch):
    client, db, settings = fixture_app
    fingerprint = scheduler.plan(settings)["plan_hash"]
    monkeypatch.setattr(db, "status", lambda: {"scheduler": "enabled", "scheduler_state": {"plan_hash": fingerprint}})
    settings.tracking.interval_seconds = 600
    response = client.get("/api/v1/status")
    assert response.status_code == 200
    assert response.json()["schedule_state"] == "plan_changed"


def test_manual_announcement_does_not_claim_schedule_is_disabled(tmp_path, monkeypatch, capsys):
    from game_census import collector
    monkeypatch.setattr(cli, "Database", lambda _: object())
    monkeypatch.setattr(collector, "collect_once", lambda *args: {"status": "succeeded"})
    assert cli.main(["--config", str(write_config(tmp_path)), "collect", "--once"]) == 0
    announcement, _ = json.JSONDecoder().raw_decode(capsys.readouterr().out)
    assert announcement.get("collection_mode") == "manual"
    assert "scheduler" not in announcement
