"""Catalog lifecycle checks use inspectable synthetic Steam pages and scratch DBs."""
from datetime import datetime, timedelta, timezone
import json

import httpx
import pytest
from pydantic import SecretStr

from game_census import catalog, cli, recovery, storage
from game_census.collector import collect_discovery
from game_census.config import ConfigurationError, load_settings
from game_census.db import DatabaseError
from game_census.sources import SourceError, REGISTRY
from game_census.sources import catalog as source
from game_census.sources.discovery import CATALOG as LEGACY_SOURCE
from test_bootstrap import scratch_database
from test_config_cli import write_config
from test_p2_integration import historical_database, old_capture
from test_storage import fixture_capture


def test_empty_historical_upgrade_repairs_frozen_catalog_reference(scratch_database, monkeypatch, tmp_path):
    base, settings = scratch_database
    db = historical_database(base, "main")
    original = storage._retarget_capture_references

    def released_retarget(conn):
        original(conn)
        # Reproduce the old cutover that left discovery pointing at its frozen copy.
        conn.execute("ALTER TABLE discovery_snapshot DROP CONSTRAINT discovery_snapshot_capture_identity_fk")
        conn.execute("ALTER TABLE discovery_snapshot ADD CONSTRAINT discovery_snapshot_capture_id_fkey FOREIGN KEY(capture_id) REFERENCES capture_legacy_004(capture_id)")

    monkeypatch.setattr(storage, "_retarget_capture_references", released_retarget)
    db.initialize([570], 300)
    first = collect(db, settings, lambda _: httpx.Response(200, json=page(more=True)), max_pages=1)
    assert first["status"] == "partial", first
    assert len(first["sources"]) == 1
    assert catalog.state(db)["last_appid"] == 400
    db.initialize([570], 300)
    assert collect(db, settings, lambda _: httpx.Response(200, json=page((620,))))["status"] == "succeeded"
    assert db.rebuild()["captures_replayed"] == 2
    with db.connection() as conn:
        assert conn.execute("SELECT count(*) AS n FROM capture_legacy_004").fetchone()["n"] == 0
    settings.storage.backup_path = str(tmp_path)
    saved = recovery.backup(settings, db)
    assert recovery.restore_verify(settings, db, saved["backup_id"])["captures_replayed"] == 2


def test_unexpected_catalog_failure_records_safe_diagnostic(scratch_database, monkeypatch):
    from test_scheduler_diagnostics import fail_storage, SECRET
    db, settings = scratch_database
    monkeypatch.setattr(db, "record_capture", fail_storage)
    report = collect(db, settings, lambda _: httpx.Response(200, json=page()))
    assert report["status"] == "failed"
    diagnostic = report["error"]["diagnostic"]
    assert diagnostic["stage"] == "record_capture"
    assert [row["type"] for row in diagnostic["exception_chain"]] == ["DatabaseError", "ConnectionFailure"]
    assert diagnostic["exception_chain"][1]["sqlstate"] == "08006"
    assert SECRET not in json.dumps(report)
    assert db.last_run()["error"] == report["error"]
    assert db.status()["uncertain_attempts"] == 1


def test_catalog_finalization_failure_emits_safe_diagnostic(scratch_database, monkeypatch, capsys):
    from test_scheduler_diagnostics import fail_storage, SECRET
    db, settings = scratch_database
    monkeypatch.setattr(db, "finish_run", fail_storage)
    with pytest.raises(DatabaseError, match="report could not be persisted"):
        collect(db, settings, lambda _: httpx.Response(200, json=page()))
    event = json.loads(capsys.readouterr().err)
    assert event["report_persisted"] is False
    assert event["error"]["diagnostic"]["stage"] == "persist_report"
    assert event["error"]["diagnostic"]["exception_chain"][1]["sqlstate"] == "08006"
    assert SECRET not in json.dumps(event)


def page(ids=(400,), more=False, cursor=None, name="Fixture"):
    return {"response": {"apps": [{"appid": app, "name": name, "last_modified": 100,
                                   "price_change_number": 5} for app in ids],
                         "have_more_results": more, "last_appid": cursor if cursor is not None else max(ids, default=0)}}


def collect(db, settings, response, **kwargs):
    settings.sources.catalog_api_key = SecretStr("fixture-key")
    return collect_discovery(settings, db, "catalog", transport=httpx.MockTransport(response), **kwargs)


def test_old_parser_remains_registered_and_new_fields_are_versioned():
    payload = json.dumps(page()).encode()
    assert REGISTRY[LEGACY_SOURCE].parse(payload)["items"] == [{"app_id": 400, "name": "Fixture"}]
    assert REGISTRY[source.SOURCE].parse(payload)["items"][0]["price_change_number"] == 5


@pytest.mark.parametrize("body", [page((400, 400)), page((620, 400)), page((400,), cursor=399), page((), more=True),
    {"response": {"apps": [{"appid": 400, "name": "Game", "last_modified": True}]}},
    {"response": {"apps": [{"appid": 400, "name": "Game", "price_change_number": -1}]}}])
def test_invalid_catalog_pages_are_errors(body):
    with pytest.raises(SourceError):
        source.parse(json.dumps(body).encode())


def test_repeated_page_with_falsely_advancing_cursor_cannot_commit(scratch_database):
    db, settings = scratch_database
    assert collect(db, settings, lambda _: httpx.Response(200, json=page(more=True)), max_pages=1)["status"] == "partial"
    before = catalog.state(db)
    result = collect(db, settings, lambda _: httpx.Response(200, json=page(cursor=620)), max_pages=1)
    assert result["status"] == "failed"
    assert catalog.state(db) == before
    assert db.rebuild()["captures_replayed"] == 1


def test_partial_scan_resumes_and_only_completion_advances_watermark(scratch_database):
    db, settings = scratch_database
    first = collect(db, settings, lambda _: httpx.Response(200, json=page(more=True)), max_pages=1)
    before = catalog.state(db)
    assert first["status"] == "partial" and not first["catalog_complete"]
    assert before["completed_watermark"] is None
    assert before["last_appid"] == 400
    attempts = []

    def respond(request):
        params = json.loads(request.url.params["input_json"])
        attempts.append(params)
        assert params["last_appid"] == 400
        assert "_catalog_scan" not in params
        assert "fixture-key" not in str(request.url)
        return httpx.Response(200, json=page((620,)))

    assert collect(db, settings, respond)["status"] == "succeeded"
    done = catalog.state(db)
    assert done["scan_id"] == before["scan_id"]
    assert done["completed_watermark"] == before["started_at"]
    assert done["complete"] and done["full_completed_at"]
    assert len(attempts) == 1
    assert db.rebuild()["captures_replayed"] == 2
    assert catalog.state(db) == done
    assert [row["app_id"] for row in db.list_apps(settings)] == [570]


def test_incremental_overlap_stale_watermark_and_periodic_full_scan(scratch_database, monkeypatch):
    db, settings = scratch_database
    collect(db, settings, lambda _: httpx.Response(200, json=page()))
    initial = catalog.state(db)
    first_time = datetime.fromisoformat(initial["completed_watermark"])
    full_time = datetime.fromisoformat(initial["full_completed_at"])
    assert catalog.plan(settings, db)["action"] == "idle"
    monkeypatch.setattr(catalog, "_now", lambda _: full_time+timedelta(seconds=settings.catalog.sync_interval_seconds+1))
    planned = catalog.plan(settings, db)
    assert planned["action"] == "incremental"
    assert planned["parameters"]["if_modified_since"] == int(first_time.timestamp())-settings.catalog.overlap_seconds
    failed = collect(db, settings, lambda _: httpx.Response(503))
    assert failed["status"] == "failed" and catalog.state(db) == initial
    collect(db, settings, lambda _: httpx.Response(200, json=page((620,), more=True)), max_pages=1)
    partial = catalog.state(db)
    assert partial["completed_watermark"] == initial["completed_watermark"]
    assert catalog.plan(settings, db)["action"] == "resume"
    collect(db, settings, lambda _: httpx.Response(200, json=page((730,))))
    complete = catalog.state(db)
    assert complete["completed_watermark"] == partial["started_at"]
    monkeypatch.setattr(catalog, "_now", lambda _: full_time+timedelta(seconds=settings.catalog.full_scan_interval_seconds+1))
    assert catalog.plan(settings, db)["action"] == "full"
    assert "if_modified_since" not in catalog.plan(settings, db)["parameters"]


def test_absence_and_reappearance_never_delete_prior_discovery(scratch_database):
    db, settings = scratch_database
    collect(db, settings, lambda _: httpx.Response(200, json=page((400, 620))))
    first = catalog.state(db)
    collect(db, settings, lambda _: httpx.Response(200, json=page((730,))), restart=True)
    assert db.catalog("400")["total"] == 1 and db.catalog("620")["total"] == 1
    collect(db, settings, lambda _: httpx.Response(200, json=page((400,), name="Reappeared")), restart=True)
    assert db.catalog("400")["items"][0]["name"] == "Reappeared"
    assert catalog.state(db)["scan_id"] != first["scan_id"]
    assert db.rebuild()["captures_replayed"] == 3


def test_changed_partial_policy_requires_explicit_restart(scratch_database):
    db, settings = scratch_database
    collect(db, settings, lambda _: httpx.Response(200, json=page(more=True)), max_pages=1)
    first = catalog.state(db)
    settings.catalog.include_dlc = True
    with pytest.raises(DatabaseError, match="policy changed"):
        catalog.plan(settings, db)
    assert catalog.state(db) == first
    assert catalog.plan(settings, db, restart=True)["parameters"]["include_dlc"] is True


def test_checkpoint_and_page_roll_back_together(scratch_database, monkeypatch):
    db, settings = scratch_database
    collect(db, settings, lambda _: httpx.Response(200, json=page()))
    before = catalog.state(db)
    original = catalog.project

    def fail_after_checkpoint(conn, capture, value):
        original(conn, capture, value)
        raise SourceError("fixture_failure", "Injected after checkpoint", "Retry")

    monkeypatch.setattr(catalog, "project", fail_after_checkpoint)
    assert collect(db, settings, lambda _: httpx.Response(200, json=page((620,))), restart=True)["status"] == "failed"
    assert catalog.state(db) == before and db.catalog("620")["total"] == 0


def test_checkpoint_replay_rejects_corruption_and_restores_full_snapshot(scratch_database, tmp_path):
    db, settings = scratch_database
    settings.storage.backup_path = str(tmp_path)
    collect(db, settings, lambda _: httpx.Response(200, json=page()))
    before = db.rebuild()
    with db.connection() as conn:
        with pytest.raises(DatabaseError, match="checkpoint"):
            with conn.transaction(force_rollback=True):
                conn.execute("UPDATE catalog_checkpoint SET last_appid=999")
                recovery._replay(conn, write=False)
    saved = recovery.backup(settings, db)
    restored = recovery.restore_verify(settings, db, saved["backup_id"])
    assert restored["captures_replayed"] == restored["projections_verified"] == 1
    assert db.rebuild() == before


def test_legacy_catalog_and_new_checkpoints_survive_verified_partition_cutover(scratch_database, tmp_path):
    base, settings = scratch_database
    settings.storage.backup_path = str(tmp_path)
    db = historical_database(base, "main")
    old_capture(db, fixture_capture(datetime(2026, 1, 1, tzinfo=timezone.utc), 0))
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=page()))) as client:
        legacy = REGISTRY[LEGACY_SOURCE].fetch(client, {"last_appid": 0}, 2000000, SecretStr("fixture"))
    old_capture(db, legacy)
    db.initialize([570], 300)
    assert catalog.state(db)["complete"] is True
    assert catalog.state(db)["completed_watermark"] is None
    assert catalog.plan(settings, db)["action"] == "full"
    collect(db, settings, lambda _: httpx.Response(200, json=page((620,))))
    before, checkpoint = db.rebuild(), catalog.state(db)
    saved = recovery.backup(settings, db)
    restored = recovery.restore_verify(settings, db, saved["backup_id"])
    storage.migrate(settings, db, proof_id=restored["proof_id"])
    assert db.rebuild() == before
    assert catalog.state(db) == checkpoint
    assert storage.verify_legacy(settings, db)["captures_replayed"] == 3
    assert db.catalog("400")["total"] == 1


def test_cli_plan_status_and_dry_run_do_not_collect(scratch_database, monkeypatch, capsys):
    db, settings = scratch_database
    monkeypatch.setattr(cli, "load_settings", lambda _: settings)
    monkeypatch.setattr(cli, "Database", lambda _: db)
    monkeypatch.setattr("game_census.collector.collect_discovery", lambda *a, **k: pytest.fail("Read-only command must not collect"))
    before = db.status()
    for arguments in (["catalog", "plan"], ["catalog", "status"], ["catalog", "sync", "--once", "--dry-run", "--max-pages", "2"]):
        assert cli.main(arguments) == 0
        result = json.loads(capsys.readouterr().out)
        if arguments[1] != "status":
            assert result["maximum_requests"] in (2, settings.catalog.max_pages_per_run)
            assert result["tracking_enrollment"] is False and result["key_configured"] is False
    assert db.status() == before


@pytest.mark.parametrize("setting,value", [("page_size", 0), ("page_size", 50001), ("max_pages_per_run", 21),
    ("sync_interval_seconds", 59), ("full_scan_interval_seconds", 2592001), ("overlap_seconds", 0), ("include_games", "yes")])
def test_catalog_config_rejects_invalid_bounds(tmp_path, setting, value):
    with pytest.raises(ConfigurationError, match="catalog."+setting):
        load_settings(write_config(tmp_path, catalog={setting: value}))
