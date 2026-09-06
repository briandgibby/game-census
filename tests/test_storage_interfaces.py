"""Storage operator inputs and direct CLI contracts; no external requests."""
import json

import pytest

from game_census import cli
from game_census.config import ConfigurationError, load_settings
from test_config_cli import SECRET, write_config


@pytest.mark.parametrize(("section", "setting", "value"), [
    ("storage", "partition_months_ahead", 0),
    ("storage", "partition_months_ahead", 13),
    ("storage", "max_partition_months", 121),
    ("storage", "migration_max_captures", 0),
    ("storage", "migration_max_captures", 1000001),
    ("storage", "backup_max_bytes", 1048575),
    ("storage", "backup_max_bytes", 1099511627777),
    ("storage", "recovery_timeout_seconds", 9),
    ("storage", "recovery_timeout_seconds", 86401),
    ("cache", "bucket_seconds", 299),
    ("cache", "bucket_seconds", 86401),
    ("cache", "bucket_seconds", 3601),
    ("cache", "bucket_seconds", True),
    ("cache", "rebuild_max_buckets", 0),
    ("cache", "rebuild_max_buckets", 20001),
])
def test_storage_settings_reject_untrusted_bounds(tmp_path, section, setting, value):
    sections = {section: {setting: value}}
    if section == "storage":
        sections[section]["database_url"] = f"postgresql://fixture:{SECRET}@db:5432/fixture"
    with pytest.raises(ConfigurationError, match=section + r"\." + setting) as error:
        load_settings(write_config(tmp_path, **sections))
    assert SECRET not in str(error.value)


def test_old_profiles_inherit_operable_storage_defaults(tmp_path):
    settings = load_settings(write_config(tmp_path))
    assert settings.storage.partition_months_ahead == 2
    assert settings.cache.bucket_seconds == 3600
    assert settings.storage.backup_max_bytes == 10737418240
    assert settings.storage.recovery_timeout_seconds == 600


def test_partition_defaults_must_fit_configured_maintenance_bound(tmp_path):
    path = write_config(tmp_path, storage={
        "database_url": f"postgresql://fixture:{SECRET}@db:5432/fixture",
        "partition_months_ahead": 12, "max_partition_months": 12,
    })
    with pytest.raises(ConfigurationError, match=r"storage\.max_partition_months"):
        load_settings(path)


@pytest.mark.parametrize("value", [300, 3600, 86400])
def test_cache_buckets_align_with_utc_day(tmp_path, value):
    assert load_settings(write_config(tmp_path, cache={"bucket_seconds": value})).cache.bucket_seconds == value


@pytest.mark.parametrize("arguments", [["backup", "restore"], ["storage", "migrate"]])
def test_restore_and_migration_require_an_explicit_selector(capsys, arguments):
    with pytest.raises(SystemExit) as error:
        cli.parser().parse_args(arguments)
    assert error.value.code == 2
    assert "required" in capsys.readouterr().err


def test_storage_plan_does_not_call_a_mutator(tmp_path, monkeypatch, capsys):
    from game_census import storage
    monkeypatch.setattr(cli, "Database", lambda _: object())
    monkeypatch.setattr(storage, "plan", lambda settings, db: {"status": "planned", "would_create": []})
    monkeypatch.setattr(storage, "maintain", lambda *a, **k: pytest.fail("dry run wrote state"))
    monkeypatch.setattr(storage, "migrate", lambda *a, **k: pytest.fail("dry run migrated state"))
    assert cli.main(["--config", str(write_config(tmp_path)), "storage", "plan"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "planned"


def test_maintenance_scope_is_visible_before_execution(tmp_path, monkeypatch, capsys):
    from game_census import storage
    monkeypatch.setattr(cli, "Database", lambda _: object())

    def maintain(settings, db, *, start_month, months):
        announced = json.loads(capsys.readouterr().out)
        assert announced["operation"] == "storage_maintain"
        assert announced["start_month"] == start_month == "2026-09"
        assert announced["months"] == months == 3
        return {"status": "succeeded", "created_partitions": []}

    monkeypatch.setattr(storage, "maintain", maintain)
    assert cli.main(["--config", str(write_config(tmp_path)), "storage", "maintain", "--start-month", "2026-09", "--months", "3"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "succeeded"


def test_cache_rebuild_invalid_window_stops_before_replaying(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "Database", lambda _: object())
    assert cli.main(["--config", str(write_config(tmp_path)), "aggregate", "rebuild", "--hours", "0"]) == 1
    assert "aggregate --hours" in capsys.readouterr().err


def test_cache_rebuild_invalid_app_stops_before_replaying(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "Database", lambda _: object())
    assert cli.main(["--config", str(write_config(tmp_path)), "aggregate", "rebuild", "--app-id", "730"]) == 1
    assert "tracking.app_ids" in capsys.readouterr().err


def test_backup_restore_previews_selected_snapshot_before_creating_scratch(tmp_path, monkeypatch, capsys):
    from game_census import recovery
    selected = "backup-" + "a" * 32
    monkeypatch.setattr(cli, "Database", lambda _: object())
    monkeypatch.setattr(recovery, "latest_backup", lambda settings: selected)
    monkeypatch.setattr(recovery, "plan", lambda settings, db, operation, backup_id:
                        {"operation": operation, "backup_id": backup_id, "new_databases": 1})

    def restore(settings, db, backup_id):
        assert json.loads(capsys.readouterr().out) == {"operation": "restore", "backup_id": selected, "new_databases": 1}
        assert backup_id == selected
        return {"status": "verified", "scratch_read_only": True}

    monkeypatch.setattr(recovery, "restore_verify", restore)
    assert cli.main(["--config", str(write_config(tmp_path)), "backup", "restore", "--latest-backup"]) == 0
    assert json.loads(capsys.readouterr().out)["scratch_read_only"] is True


def test_backup_plan_reads_without_creating_a_backup(tmp_path, monkeypatch, capsys):
    from game_census import recovery
    monkeypatch.setattr(cli, "Database", lambda _: object())
    monkeypatch.setattr(recovery, "plan", lambda settings, db, operation, backup_id: {"operation": operation})
    monkeypatch.setattr(recovery, "backup", lambda *a, **k: pytest.fail("Plan created a backup"))
    assert cli.main(["--config", str(write_config(tmp_path)), "backup", "plan"]) == 0
    assert json.loads(capsys.readouterr().out)["operation"] == "backup"


def test_migration_resolves_and_prints_proof_before_execution(tmp_path, monkeypatch, capsys):
    from game_census import recovery, storage
    proof = str(tmp_path / "verification-fixture.json")
    monkeypatch.setattr(cli, "Database", lambda _: object())
    monkeypatch.setattr(recovery, "latest_proof", lambda settings: proof)
    monkeypatch.setattr(storage, "plan", lambda settings, db: {"layout": "legacy", "capture_count": 4})

    def migrate(settings, db, proof_id):
        announced = json.loads(capsys.readouterr().out)
        assert announced["proof_id"] == proof_id == proof
        assert announced["plan"]["capture_count"] == 4
        return {"status": "succeeded", "layout": "partitioned"}

    monkeypatch.setattr(storage, "migrate", migrate)
    assert cli.main(["--config", str(write_config(tmp_path)), "storage", "migrate", "--latest-backup"]) == 0
    assert json.loads(capsys.readouterr().out)["layout"] == "partitioned"


def test_recovery_failure_has_nonzero_exit_and_safe_diagnostic(tmp_path, monkeypatch, capsys):
    from game_census import recovery
    from game_census.db import DatabaseError
    monkeypatch.setattr(cli, "Database", lambda _: object())

    def failed(*args, **kwargs):
        raise DatabaseError("No completed backup exists. Run backup create first.")

    monkeypatch.setattr(recovery, "latest_backup", failed)
    assert cli.main(["--config", str(write_config(tmp_path)), "backup", "restore", "--latest-backup"]) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert json.loads(output.err)["status"] == "failed"
    assert SECRET not in output.err


def test_aggregate_scope_names_per_app_bound_and_actual_replay_count(tmp_path, monkeypatch, capsys):
    from game_census import cache

    class ReplayDatabase:
        def status(self):
            return {"captures": 4, "tracked_apps": 2}

        def rebuild(self):
            scope = json.loads(capsys.readouterr().out)
            assert scope["capture_count"] == 4
            assert scope["cache_app_count"] == 2
            assert scope["maximum_cache_buckets_per_app"] == 8760
            return {"status": "succeeded", "captures_replayed": 4}

    monkeypatch.setattr(cli, "Database", lambda _: ReplayDatabase())
    monkeypatch.setattr(cache, "rebuild", lambda *a, **k: {"status": "succeeded"})
    assert cli.main(["--config", str(write_config(tmp_path)), "aggregate", "rebuild"]) == 0
    assert json.loads(capsys.readouterr().out)["captures_replayed"] == 4
