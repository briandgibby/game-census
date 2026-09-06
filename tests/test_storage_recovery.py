"""Full native backup and read-only scratch recovery, using isolated source schemas."""
import json
import io
import shutil
import time
from pathlib import Path
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
import threading
import uuid
import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
import pytest
from game_census import recovery
from game_census.collector import collect_once
from game_census.config import Settings
from game_census.db import DatabaseError
from test_bootstrap import scratch_database, transport


def configuration(tmp_path):
    return Settings.model_validate({"storage": {"database_url": "postgresql://fixture:fixture_secret@db:5432/fixture",
                                               "backup_path": str(tmp_path)}})


@pytest.mark.parametrize("value", ["../backup-other", "/tmp/backup-other", "", "backup-" + "a" * 31, "backup-" + "z" * 32])
def test_backup_selector_rejects_path_traversal_and_invalid_ids(tmp_path, value):
    with pytest.raises(DatabaseError, match="backup_id must be an ID"):
        recovery._backup_directory(configuration(tmp_path), value)


@pytest.mark.parametrize("selector", [recovery._selected, recovery.latest_proof], ids=["selected_backup", "latest_proof"])
def test_explicit_empty_backup_id_never_selects_latest(tmp_path, monkeypatch, selector):
    monkeypatch.setattr(recovery, "latest_backup", lambda settings: pytest.fail("An explicit empty backup ID must not select latest"))
    with pytest.raises(DatabaseError, match="backup_id must be an ID"):
        selector(configuration(tmp_path), backup_id="")


def test_client_environment_keeps_credentials_out_of_command_arguments(monkeypatch):
    monkeypatch.setenv("PGOPTIONS", "untrusted inherited setting")
    monkeypatch.setenv("PGPASSWORD", "wrong inherited password")
    env = recovery._environment("postgresql://fixture:fixture_secret@db:5432/fixture", database="game_census_restore_" + "a" * 32)
    assert env["PGPASSWORD"] == "fixture_secret"
    assert env["PGDATABASE"] == "game_census_restore_" + "a" * 32
    assert "PGOPTIONS" not in env


@pytest.mark.parametrize("payload", [b"{", b"[]", b"x" * (recovery.MAX_MANIFEST_BYTES + 1)], ids=["malformed", "nonobject", "oversize"])
def test_invalid_or_oversize_metadata_is_actionable(tmp_path, payload):
    path = tmp_path / "manifest.json"
    path.write_bytes(payload)
    with pytest.raises(DatabaseError, match="Recovery metadata"):
        recovery._read_json(path)


def test_json_artifacts_are_never_overwritten(tmp_path):
    path = tmp_path / "manifest.json"
    recovery._write_json(path, {"first": True})
    before = path.read_bytes()
    with pytest.raises(FileExistsError):
        recovery._write_json(path, {"second": True})
    assert path.read_bytes() == before


def test_missing_backup_selector_does_not_make_state(tmp_path):
    settings = configuration(tmp_path / "absent")
    with pytest.raises(DatabaseError, match="No completed backup exists"):
        recovery.latest_backup(settings)
    assert not Path(settings.storage.backup_path).exists()


def test_elapsed_recovery_limit_cancels_active_database_query():
    cancelled = []
    class Connection:
        def cancel(self):
            cancelled.append(True)
    with pytest.raises(DatabaseError, match="recovery_timeout_seconds was exceeded"):
        with recovery._bounded(.02), recovery._watch_connection(Connection()):
            time.sleep(.05)
    assert cancelled == [True]


def test_native_client_error_is_recorded_without_exposing_secret(monkeypatch):
    captured = []
    class Process:
        def __init__(self, command, **options):
            captured.append((command, options))
            self.stderr = io.BytesIO(b"password authentication failed: fixture_secret")
        def poll(self):
            return 1
        def wait(self, **kwargs):
            return 1
    monkeypatch.setattr(recovery, "_client", lambda name: name)
    monkeypatch.setattr(recovery.subprocess, "Popen", Process)
    with pytest.raises(DatabaseError, match="authentication failure") as caught:
        recovery._run_client("pg_dump", ["--format=custom"], "postgresql://fixture:fixture_secret@db:5432/fixture", timeout=10)
    assert "fixture_secret" not in str(caught.value)
    assert captured[0][0] == ["pg_dump", "--format=custom"]
    assert captured[0][1]["env"]["PGPASSWORD"] == "fixture_secret"


def test_native_dump_stops_at_configured_archive_size(monkeypatch, tmp_path):
    path = tmp_path / "fixture.dump"
    path.write_bytes(b"123456")
    killed = []
    class Process:
        stderr = io.BytesIO()
        def poll(self):
            return -9 if killed else None
        def kill(self):
            killed.append(True)
        def wait(self, **kwargs):
            return -9
    monkeypatch.setattr(recovery, "_client", lambda name: name)
    monkeypatch.setattr(recovery.subprocess, "Popen", lambda *args, **kwargs: Process())
    with pytest.raises(DatabaseError, match="backup_max_bytes was exceeded"):
        recovery._run_client("pg_dump", [], "postgresql://fixture:fixture_secret@db:5432/fixture", timeout=10,
                             watched_file=path, max_bytes=5)
    assert killed == [True]
    assert path.read_bytes() == b"123456"


@pytest.mark.integration
def test_full_backup_preserves_operational_ledgers_and_read_only_scratch(scratch_database, tmp_path):
    db, settings = scratch_database
    settings.storage.backup_path = str(tmp_path)
    collect_once(settings, db, transport=transport(count=0))
    collect_once(settings, db, transport=transport(status=503))
    with db.connection() as conn:
        conn.execute("UPDATE schedule_state SET enabled=true,plan_hash=%s,epoch=epoch+1 WHERE singleton", ("a" * 64,))
        conn.execute("INSERT INTO schedule_event(action,plan_hash,details) VALUES('enabled',%s,%s::jsonb)",
                     ("a" * 64, json.dumps({"app_ids": [570], "cadence_seconds": 300, "epoch": 1})))
    before = recovery.current_manifest(db)
    saved = recovery.backup(settings, db)
    manifest = recovery._read_json(Path(saved["backup_path"]) / "manifest.json")
    assert saved["restore_verified"] is False
    assert manifest["snapshot"]["contents_sha256"] == before["contents_sha256"]
    tables = {row["name"]: row["rows"] for row in manifest["snapshot"]["tables"]}
    assert tables["collection_run"] == tables["request_attempt"] == tables["request_result"] == 2
    assert tables["schedule_event"] == 1
    restored = recovery.restore_verify(settings, db, saved["backup_id"])
    assert restored["scratch_read_only"] is True
    assert restored["captures_replayed"] == restored["projections_verified"] == 1
    assert recovery.require_verified_snapshot(db, restored["proof_id"])["status"] == "verified"
    assert recovery.latest_backup(settings) == saved["backup_id"]
    assert recovery.latest_proof(settings) == restored["proof_id"]
    assert recovery.current_manifest(db)["contents_sha256"] == before["contents_sha256"]
    scratch = recovery.ScratchDatabase(recovery._scratch_dsn(db._dsn, restored["scratch_database"]), db.schema)
    with scratch.connection() as conn:
        assert conn.execute("SELECT enabled FROM schedule_state WHERE singleton").fetchone()["enabled"] is True
        assert conn.execute("SHOW default_transaction_read_only").fetchone()["default_transaction_read_only"] == "on"
    with pytest.raises(DatabaseError):
        scratch.start_run([570], ["steam_current_players_v1"])


@pytest.mark.integration
def test_source_change_invalidates_verified_cutover_proof(scratch_database, tmp_path):
    db, settings = scratch_database
    settings.storage.backup_path = str(tmp_path)
    collect_once(settings, db, transport=transport(count=7))
    saved = recovery.backup(settings, db)
    restored = recovery.restore_verify(settings, db, saved["backup_id"])
    collect_once(settings, db, transport=transport(count=8))
    with pytest.raises(DatabaseError, match="source changed after the verified backup"):
        recovery.require_verified_snapshot(db, restored["proof_id"])
    assert db.app_detail(570, settings)["sample_count"] == 2


@pytest.mark.integration
def test_backups_are_distinct_and_corrupt_copy_cannot_create_scratch(scratch_database, tmp_path, monkeypatch):
    db, settings = scratch_database
    settings.storage.backup_path = str(tmp_path)
    collect_once(settings, db, transport=transport(count=11))
    first = recovery.backup(settings, db)
    original = (Path(first["backup_path"]) / "archive.dump").read_bytes()
    second = recovery.backup(settings, db)
    assert first["backup_id"] != second["backup_id"]
    assert (Path(first["backup_path"]) / "archive.dump").read_bytes() == original
    # Corrupt only the second disposable export; the first archive stays intact.
    with (Path(second["backup_path"]) / "archive.dump").open("ab") as stream:
        stream.write(b"invalid trailing bytes")
    monkeypatch.setattr(recovery, "_client", lambda name: pytest.fail("corrupt archive must fail before client/database work"))
    with pytest.raises(DatabaseError, match="archive checksum check"):
        recovery.restore_verify(settings, db, second["backup_id"])


@pytest.mark.integration
def test_warm_derived_cache_survives_exact_backup_and_projection_replay(scratch_database, tmp_path):
    from datetime import timedelta
    from game_census import cache
    from test_rollup_cache import series
    db, settings = scratch_database
    settings.storage.backup_path = str(tmp_path)
    start, end, _ = series(db, settings, hours=2)
    cache.history(db, settings, 570, start + timedelta(hours=1), end, "auto")
    with db.connection() as conn:
        assert conn.execute("SELECT count(*) AS n FROM player_rollup_cache WHERE valid").fetchone()["n"] == 2
    before = recovery.current_manifest(db)
    saved = recovery.backup(settings, db)
    restored = recovery.restore_verify(settings, db, saved["backup_id"])
    assert restored["source_snapshot_sha256"] == before["contents_sha256"]
    assert recovery.require_verified_snapshot(db, restored["proof_id"])["status"] == "verified"


@pytest.mark.integration
def test_failed_replay_keeps_new_scratch_database_read_only(scratch_database, tmp_path, monkeypatch):
    db, settings = scratch_database
    settings.storage.backup_path = str(tmp_path)
    collect_once(settings, db, transport=transport(count=19))
    with db.connection() as conn:
        conn.execute("UPDATE schedule_state SET enabled=true WHERE singleton")
    saved = recovery.backup(settings, db)
    def fail_replay(*args, **kwargs):
        raise DatabaseError("Injected replay verification failure.")
    monkeypatch.setattr(recovery, "_replay", fail_replay)
    with pytest.raises(DatabaseError, match="Injected replay verification failure"):
        recovery.restore_verify(settings, db, saved["backup_id"])
    failure_path = next(Path(saved["backup_path"]).glob("failure-*.json"))
    failure = recovery._read_json(failure_path)
    scratch = recovery.ScratchDatabase(recovery._scratch_dsn(db._dsn, failure["scratch_database"]), db.schema)
    with scratch.connection() as conn:
        assert conn.execute("SELECT enabled FROM schedule_state WHERE singleton").fetchone()["enabled"] is True
        assert conn.execute("SHOW default_transaction_read_only").fetchone()["default_transaction_read_only"] == "on"


@pytest.mark.integration
def test_backup_and_disable_follow_the_same_lock_order(scratch_database, tmp_path):
    from game_census import scheduler
    db, settings = scratch_database
    settings.storage.backup_path = str(tmp_path)
    held, release, backup_pid = threading.Event(), threading.Event(), []
    class Proxy:
        def __init__(self, conn, label):
            self.conn, self.label = conn, label
        def __getattr__(self, name):
            return getattr(self.conn, name)
        def execute(self, statement, parameters=None):
            if self.label == "backup" and isinstance(statement, str) and "pg_advisory_xact_lock(734801001)" in statement:
                backup_pid.append(self.conn.info.backend_pid)
            result = self.conn.execute(statement, parameters)
            if self.label == "disable" and isinstance(statement, str) and statement.startswith("UPDATE schedule_state"):
                held.set()
                assert release.wait(10), "Backup did not reach its lock barrier"
            return result
    class WrappedDatabase(recovery.Database):
        def __init__(self, label):
            super().__init__(db._dsn)
            self.label = label
        @contextmanager
        def connection(self):
            with db.connection() as conn:
                yield Proxy(conn, self.label)
    with ThreadPoolExecutor(max_workers=2) as pool:
        disabling = pool.submit(scheduler.disable, settings, WrappedDatabase("disable"))
        assert held.wait(5)
        backing_up = pool.submit(recovery.backup, settings, WrappedDatabase("backup"))
        waiting = False
        deadline = time.monotonic() + 5
        try:
            while time.monotonic() < deadline and not waiting:
                if backup_pid:
                    with db.connection() as conn:
                        waiting = conn.execute("SELECT wait_event_type='Lock' AS waiting FROM pg_stat_activity WHERE pid=%s", (backup_pid[0],)).fetchone()["waiting"]
                if not waiting:
                    threading.Event().wait(.02)
            assert waiting, "Backup did not reach a lock wait"
        finally:
            release.set()
        assert disabling.result(timeout=10)["status"] == "disabled"
        assert backing_up.result(timeout=10)["status"] == "completed"


@pytest.mark.integration
def test_public_schema_backup_restores_without_changing_the_source(scratch_database, tmp_path, monkeypatch):
    base, settings = scratch_database
    name = "game_census_test_public_" + uuid.uuid4().hex
    with psycopg.connect(base._dsn, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(name)))
    public = recovery.Database(make_conninfo(base._dsn, dbname=name))
    public.initialize([570], 300)
    settings.storage.backup_path = str(tmp_path)
    collect_once(settings, public, transport=transport(count=23))
    before = recovery.current_manifest(public)
    assert before["source_identity"]["schema"] == "public"
    saved = recovery.backup(settings, public)
    native_diagnostic = bytearray()
    original_popen = recovery.subprocess.Popen
    class DiagnosticPipe:
        def __init__(self, stream):
            self.stream = stream
        def read(self, size):
            block = self.stream.read(size)
            native_diagnostic.extend(block[:65536 - len(native_diagnostic)])
            return block
        def close(self):
            self.stream.close()
    def observe_native_error(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        process.stderr = DiagnosticPipe(process.stderr)
        return process
    monkeypatch.setattr(recovery.subprocess, "Popen", observe_native_error)
    try:
        restored = recovery.restore_verify(settings, public, saved["backup_id"])
    except DatabaseError:
        if b'schema "public" already exists' in native_diagnostic:
            raise DatabaseError('Native restore diagnostic: ERROR: schema "public" already exists') from None
        raise
    assert restored["captures_replayed"] == 1
    assert recovery.require_verified_snapshot(public, restored["proof_id"])["status"] == "verified"
    assert recovery.current_manifest(public)["contents_sha256"] == before["contents_sha256"]


def test_public_restore_selection_preserves_every_other_toc_entry(tmp_path, monkeypatch):
    contents = (b"; Archive TOC\n5; 2615 2200 SCHEMA - public pg_database_owner\n"
                b"6; 2615 9000 SCHEMA - public_extra fixture\n7; 1259 9001 TABLE public capture fixture\n"
                b"8; 0 9001 TABLE DATA public capture fixture\n9; 0 0 COMMENT - SCHEMA public fixture\n")
    class EmptyPublic:
        _dsn = "unused"
        @contextmanager
        def connection(self):
            yield self
        def execute(self, statement):
            return type("Rows", (), {"fetchone": lambda self: {"oid": 2200, "objects": 0}})()
    def toc_only(name, arguments, *args, **kwargs):
        Path(next(value.removeprefix("--file=") for value in arguments if value.startswith("--file="))).write_bytes(contents)
    monkeypatch.setattr(recovery, "_run_client", toc_only)
    result = recovery._restore_selection(tmp_path, EmptyPublic(), "public")
    selected = Path(result[0].removeprefix("--use-list=")).read_bytes()
    assert b"SCHEMA - public pg_database_owner" not in selected
    assert selected.splitlines()[1:] == [line for line in contents.splitlines() if b"SCHEMA - public pg_database_owner" not in line]


def test_public_restore_refuses_namespace_with_existing_objects(tmp_path, monkeypatch):
    class PopulatedPublic:
        @contextmanager
        def connection(self):
            yield self
        def execute(self, statement):
            return type("Rows", (), {"fetchone": lambda self: {"oid": 2200, "objects": 1}})()
    monkeypatch.setattr(recovery, "_run_client", lambda *args, **kwargs: pytest.fail("No native restore may touch a populated namespace"))
    with pytest.raises(DatabaseError, match="public schema is not empty"):
        recovery._restore_selection(tmp_path, PopulatedPublic(), "public")
    assert list(tmp_path.iterdir()) == []
