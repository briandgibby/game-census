"""Immutable PostgreSQL backups and verified restores into new read-only databases.

The pinned PostgreSQL clients own native schema/data restoration. This module
owns selection, bounds, independent content checks and the restore proof used
before a storage cutover. It never drops a database or overwrites a backup.
"""
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time
import uuid

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row

from . import __version__, projections
from .db import Database, DatabaseError
from .sources import REGISTRY, players


FORMAT_VERSION = 1
MAX_TABLES = 128
MAX_COLUMNS = 128
MAX_MANIFEST_BYTES = 1048576
MAX_BACKUPS = 10000
_BACKUP_ID = re.compile(r"backup-[0-9a-f]{32}\Z")
_PROOF_NAME = re.compile(r"verification-[0-9a-f]{32}\.json\Z")
_SCRATCH_NAME = re.compile(r"game_census_restore_[0-9a-f]{32}\Z")
_DEADLINE = ContextVar("recovery_deadline", default=None)
_ACTIVE_CONNECTIONS = ContextVar("recovery_active_connections", default=None)


@contextmanager
def _bounded(seconds):
    if type(seconds) not in (int, float) or not 0 < seconds <= 86400:
        raise DatabaseError("storage.recovery_timeout_seconds is invalid. Use its configured bounds before recovery.")
    deadline = time.monotonic() + seconds
    connections, cancellation_failures, mutex = [], [], threading.Lock()
    def cancel_active():
        with mutex:
            active = list(connections)
        for conn in active:
            try:
                conn.cancel()
            except psycopg.Error:
                with mutex:
                    cancellation_failures.append("PostgreSQL cancellation channel failed")
    timer = threading.Timer(seconds, cancel_active)
    timer.daemon = True
    token = _DEADLINE.set(deadline)
    connection_token = _ACTIVE_CONNECTIONS.set((connections, mutex))
    timer.start()
    try:
        yield
        _remaining()
    except DatabaseError:
        if time.monotonic() >= deadline:
            detail = " PostgreSQL cancellation could not be confirmed; inspect active database queries." if cancellation_failures else ""
            raise DatabaseError("storage.recovery_timeout_seconds was exceeded. The source and incomplete recovery output are retained; inspect them before retrying." + detail) from None
        raise
    finally:
        timer.cancel()
        _ACTIVE_CONNECTIONS.reset(connection_token)
        _DEADLINE.reset(token)


@contextmanager
def _watch_connection(conn):
    state = _ACTIVE_CONNECTIONS.get()
    if state is None:
        yield conn
        return
    connections, mutex = state
    _remaining()
    with mutex:
        connections.append(conn)
    try:
        yield conn
    finally:
        with mutex:
            connections.remove(conn)


def _remaining():
    deadline = _DEADLINE.get()
    if deadline is None:
        return None
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise DatabaseError("storage.recovery_timeout_seconds was exceeded. The source and incomplete recovery output are retained; inspect them before retrying.")
    return remaining


def _json_bytes(value):
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _digest(value):
    return hashlib.sha256(_json_bytes(value)).hexdigest()


def _file_hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1048576):
            _remaining()
            digest.update(block)
    return digest.hexdigest()


def _write_json(path, value):
    payload = _json_bytes(value)
    if len(payload) > MAX_MANIFEST_BYTES:
        raise DatabaseError("Recovery metadata exceeds its size bound. Inspect the application schema before retrying.")
    with path.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    path.chmod(0o600)


def _read_json(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_MANIFEST_BYTES:
        raise DatabaseError("Recovery metadata is missing, linked, or exceeds its size bound. Select an intact backup created by backup create.")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (ValueError, UnicodeError, RecursionError):
        raise DatabaseError("Recovery metadata is invalid. Select an intact backup created by backup create.") from None


def _root(settings):
    return Path(settings.storage.backup_path).expanduser().resolve()


def _backup_directory(settings, backup_id):
    if not isinstance(backup_id, str) or not _BACKUP_ID.fullmatch(backup_id):
        raise DatabaseError("backup_id must be an ID returned by backup create. Use the latest-backup selector or copy a listed ID.")
    root = _root(settings)
    target = root / backup_id
    if target.is_symlink() or target.resolve().parent != root:
        raise DatabaseError("The selected backup is outside storage.backup_path. Select an intact backup inside the configured root.")
    return target


def latest_backup(settings):
    root = _root(settings)
    if not root.is_dir():
        raise DatabaseError("No completed backup exists in storage.backup_path. Run backup create first.")
    candidates = []
    for index, item in enumerate(root.iterdir()):
        if index >= MAX_BACKUPS:
            raise DatabaseError("The backup directory exceeds the selection bound. Specify a backup ID explicitly.")
        if _BACKUP_ID.fullmatch(item.name) and not item.is_symlink() and (item / "manifest.json").is_file():
            manifest = _read_json(item / "manifest.json")
            if (manifest.get("status") != "completed" or manifest.get("backup_id") != item.name
                    or not isinstance(manifest.get("completed_at"), str)):
                raise DatabaseError("A completed backup manifest is inconsistent. Inspect the named backup directory before selecting the latest backup.")
            candidates.append((manifest["completed_at"], item.name))
    if not candidates:
        raise DatabaseError("No completed backup exists in storage.backup_path. Run backup create first; unfinished directories are retained for inspection.")
    return max(candidates)[1]


def latest_proof(settings, backup_id=None):
    directory = _backup_directory(settings, latest_backup(settings) if backup_id is None else backup_id)
    candidates = []
    for index, path in enumerate(directory.iterdir()):
        if index >= MAX_BACKUPS:
            raise DatabaseError("The backup directory exceeds the proof selection bound. Specify a proof path explicitly.")
        if _PROOF_NAME.fullmatch(path.name):
            proof = _read_json(path)
            if proof.get("status") == "verified" and isinstance(proof.get("verified_at"), str):
                candidates.append((proof["verified_at"], str(path)))
    if not candidates:
        raise DatabaseError("This backup has no verified scratch restore. Run backup restore for the selected backup before storage migrate.")
    return max(candidates)[1]


def _source_schema(conn):
    row = conn.execute("""SELECT current_schema() AS schema,current_database() AS database,
        (SELECT oid::bigint FROM pg_database WHERE datname=current_database()) AS database_oid,
        (SELECT oid::bigint FROM pg_namespace WHERE nspname=current_schema()) AS schema_oid,
        current_setting('server_version_num')::integer AS server_version_num""").fetchone()
    if not row or not row["schema"]:
        raise DatabaseError("The configured connection has no application schema. Run initialize before backup create.")
    return row


def _table_names(conn, schema):
    rows = conn.execute("""SELECT c.relname AS name FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname=%s AND c.relkind IN ('r','p') AND NOT c.relispartition ORDER BY c.relname""", (schema,)).fetchall()
    if not rows or len(rows) > MAX_TABLES or "capture" not in {r["name"] for r in rows}:
        raise DatabaseError("The application table inventory is empty, incomplete, or exceeds its bound. Initialize the intended database or inspect its schema before backup.")
    return [row["name"] for row in rows]


def _canonical_settings(conn):
    conn.execute("SET LOCAL TIME ZONE 'UTC'")
    conn.execute("SET LOCAL DateStyle TO 'ISO, YMD'")
    conn.execute("SET LOCAL extra_float_digits TO 3")
    conn.execute("SET LOCAL bytea_output TO 'hex'")


def _capture_integrity(conn, schema):
    digest, count, first, last, versions = hashlib.sha256(), 0, None, None, set()
    with conn.cursor(name="recovery_captures_" + uuid.uuid4().hex, row_factory=dict_row) as cursor:
        cursor.execute(sql.SQL("SELECT capture_id,received_at,source,source_version,payload,checksum FROM {}.capture ORDER BY capture_id").format(sql.Identifier(schema)))
        for row in cursor:
            _remaining()
            if hashlib.sha256(bytes(row["payload"])).hexdigest() != row["checksum"]:
                raise DatabaseError("A canonical capture payload does not match its checksum. Stop collection and inspect retained backups before changing storage.")
            count += 1
            digest.update(f"{row['capture_id']}:{row['checksum']}\n".encode())
            when = row["received_at"]
            first = when if first is None else min(first, when)
            last = when if last is None else max(last, when)
            versions.add((row["source"], row["source_version"]))
    return {"count": count, "sha256": digest.hexdigest(),
            "first_received_at": first.isoformat() if first else None,
            "last_received_at": last.isoformat() if last else None,
            "source_versions": [list(value) for value in sorted(versions)]}


def current_manifest(db, *, conn=None):
    """Hash all root-table rows and generator states, including operational ledgers."""
    with (db.connection() if conn is None else nullcontext(conn)) as conn, _watch_connection(conn):
        _canonical_settings(conn)
        identity = _source_schema(conn)
        schema = identity["schema"]
        names = _table_names(conn, schema)
        tables = []
        for name in names:
            _remaining()
            columns = conn.execute("""SELECT a.attname AS name,format_type(a.atttypid,a.atttypmod) AS type,
                a.attnotnull AS not_null,a.attidentity AS identity,
                pg_get_expr(d.adbin,d.adrelid) AS default
                FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid
                JOIN pg_namespace n ON n.oid=c.relnamespace LEFT JOIN pg_attrdef d ON d.adrelid=c.oid AND d.adnum=a.attnum
                WHERE n.nspname=%s AND c.relname=%s AND a.attnum>0 AND NOT a.attisdropped ORDER BY a.attnum""", (schema, name)).fetchall()
            if not columns or len(columns) > MAX_COLUMNS:
                raise DatabaseError("An application table exceeds the recovery column bound. Inspect the schema before backing it up.")
            keys = conn.execute("""SELECT a.attname AS name FROM pg_index i
                JOIN pg_class c ON c.oid=i.indrelid JOIN pg_namespace n ON n.oid=c.relnamespace
                CROSS JOIN LATERAL unnest(i.indkey) WITH ORDINALITY AS k(attnum,position)
                JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=k.attnum
                WHERE n.nspname=%s AND c.relname=%s AND i.indisprimary ORDER BY k.position""", (schema, name)).fetchall()
            ordering = [row["name"] for row in keys] or [column["name"] for column in columns]
            count = conn.execute(sql.SQL("SELECT count(*) AS n FROM {}.{}").format(sql.Identifier(schema), sql.Identifier(name))).fetchone()["n"]
            digest = hashlib.sha256()
            query = sql.SQL("COPY (SELECT * FROM {}.{} ORDER BY {}) TO STDOUT WITH (FORMAT text, ENCODING 'UTF8')").format(
                sql.Identifier(schema), sql.Identifier(name), sql.SQL(",").join(map(sql.Identifier, ordering)))
            with conn.cursor().copy(query) as copy:
                for block in copy:
                    _remaining()
                    digest.update(block)
            tables.append({"name": name, "columns": columns, "primary_key": [row["name"] for row in keys],
                           "rows": count, "sha256": digest.hexdigest()})
        sequences = []
        for row in conn.execute("""SELECT c.relname AS name FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname=%s AND c.relkind='S' ORDER BY c.relname""", (schema,)).fetchall():
            state = conn.execute(sql.SQL("SELECT last_value,is_called FROM {}.{}").format(sql.Identifier(schema), sql.Identifier(row["name"]))).fetchone()
            sequences.append({"name": row["name"], **state})
        version = conn.execute(sql.SQL("SELECT max(version) AS version FROM {}.schema_migration").format(sql.Identifier(schema))).fetchone()["version"]
        contents = {"schema_version": version, "tables": tables, "sequences": sequences,
                    "captures": _capture_integrity(conn, schema)}
        return {"source_identity": identity, **contents, "contents_sha256": _digest(contents)}


def _environment(dsn, *, database=None):
    values = conninfo_to_dict(dsn)
    env = {key: value for key, value in os.environ.items() if not key.startswith("PG")}
    mapping = {"host": "PGHOST", "port": "PGPORT", "user": "PGUSER", "password": "PGPASSWORD", "dbname": "PGDATABASE"}
    env.update({target: values[source] for source, target in mapping.items() if source in values})
    if database is not None:
        env["PGDATABASE"] = database
    env.update(PGCONNECT_TIMEOUT="5", PGCLIENTENCODING="UTF8", PGAPPNAME="game_census_recovery")
    return env


def _client(name):
    executable = shutil.which(name)
    if executable is None:
        raise DatabaseError("Pinned PostgreSQL recovery clients are unavailable. Rebuild the shipped Docker application before backup or restore.")
    return executable


def _run_client(name, arguments, dsn, *, timeout, watched_file=None, max_bytes=None, database=None, restore_writes=False):
    """Capture only bounded diagnostics; never put the connection URL in argv."""
    command = [_client(name), *arguments]
    diagnostics = bytearray()
    environment = _environment(dsn, database=database)
    if restore_writes:
        if name != "pg_restore":
            raise DatabaseError("Only the native scratch restore may override its default read-only protection.")
        environment["PGOPTIONS"] = "-c default_transaction_read_only=off"
    proc = subprocess.Popen(command, env=environment,
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    def drain():
        while block := proc.stderr.read(8192):
            if len(diagnostics) < 65536:
                diagnostics.extend(block[:65536 - len(diagnostics)])
    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    started, reason = time.monotonic(), None
    try:
        while proc.poll() is None:
            if time.monotonic() - started > timeout:
                reason = "storage.recovery_timeout_seconds was exceeded"
            elif watched_file is not None and watched_file.exists() and watched_file.stat().st_size > max_bytes:
                reason = "storage.backup_max_bytes was exceeded"
            if reason:
                proc.kill()
                break
            time.sleep(.05)
        code = proc.wait(timeout=10)
        reader.join(timeout=10)
        if reader.is_alive():
            raise DatabaseError("The PostgreSQL recovery client did not close its diagnostic stream. Inspect database and process status before retrying.")
        if reason:
            raise DatabaseError(reason + ". The incomplete output is retained; correct configuration or reduce the source size before retrying.")
        if code:
            # Native diagnostics can contain operator-controlled object names,
            # paths and connection details. Return a safe classified failure.
            diagnostic = bytes(diagnostics).lower()
            category = ("authentication" if b"password authentication failed" in diagnostic else
                        "permissions" if b"permission denied" in diagnostic else
                        "connectivity" if b"could not connect" in diagnostic or b"connection refused" in diagnostic else
                        "native restore or dump")
            raise DatabaseError(f"PostgreSQL {category} failure (exit {code}). Check storage.database_url, source integrity and role permissions; the original database and incomplete output remain available.")
        if watched_file is not None and watched_file.stat().st_size > max_bytes:
            raise DatabaseError("storage.backup_max_bytes was exceeded. The incomplete output is retained; choose a suitable bound before retrying.")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
        proc.stderr.close()


def _failure(directory, operation, error, **details):
    _write_json(directory / ("failure-" + uuid.uuid4().hex + ".json"),
                {"status": "failed", "operation": operation, "at": datetime.now(timezone.utc).isoformat(),
                 "error_type": type(error).__name__, "error": str(error) if isinstance(error, DatabaseError) else "Recovery could not complete; inspect the command diagnostic.",
                 **details,
                 "next_action": "Inspect the command error and retained output. Retry with a new backup or scratch destination; no existing state was removed."})


def _selected(settings, backup_id=None):
    selected = latest_backup(settings) if backup_id is None else backup_id
    directory = _backup_directory(settings, selected)
    manifest = _read_json(directory / "manifest.json")
    archive = directory / "archive.dump"
    if (manifest.get("format_version") != FORMAT_VERSION or manifest.get("status") != "completed"
            or manifest.get("backup_id") != selected or not isinstance(manifest.get("snapshot"), dict)
            or not isinstance(manifest["snapshot"].get("tables"), list)
            or not 1 <= len(manifest["snapshot"]["tables"]) <= MAX_TABLES
            or not isinstance(manifest["snapshot"].get("source_identity"), dict)
            or not isinstance(manifest["snapshot"]["source_identity"].get("schema"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", str(manifest["snapshot"].get("contents_sha256", "")))
            or archive.is_symlink() or not archive.is_file()
            or archive.stat().st_size != manifest.get("archive_bytes")
            or archive.stat().st_size > settings.storage.backup_max_bytes
            or _file_hash(archive) != manifest.get("archive_sha256")):
        raise DatabaseError("The selected backup failed its manifest, size or archive checksum check. Restore an intact backup; no scratch database was created.")
    return directory, manifest


def plan(settings, db, operation="backup", backup_id=None):
    with _bounded(settings.storage.recovery_timeout_seconds):
        return _plan(settings, db, operation, backup_id)


def _plan(settings, db, operation="backup", backup_id=None):
    if operation not in ("backup", "restore"):
        raise DatabaseError("Recovery plan operation must be backup or restore.")
    if operation == "restore":
        directory, manifest = _selected(settings, backup_id)
        return {"operation": "restore", "backup_id": manifest["backup_id"], "backup_path": str(directory),
                "source_snapshot_sha256": manifest["snapshot"]["contents_sha256"],
                "tables": len(manifest["snapshot"]["tables"]), "archive_bytes": manifest["archive_bytes"],
                "new_databases": 1, "existing_databases_overwritten": 0,
                "scheduler": "new scratch database is protected read-only after verification",
                "timeout_seconds": settings.storage.recovery_timeout_seconds}
    with db.connection() as conn:
        identity = _source_schema(conn)
        names = _table_names(conn, identity["schema"])
        size = conn.execute("""SELECT coalesce(sum(pg_total_relation_size(c.oid)),0)::bigint AS bytes
            FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname=%s AND c.relkind IN ('r','p')""", (identity["schema"],)).fetchone()["bytes"]
    return {"operation": "backup", "schema": identity["schema"], "tables": len(names),
            "estimated_relation_bytes": size, "backup_path": str(_root(settings)),
            "maximum_archive_bytes": settings.storage.backup_max_bytes,
            "timeout_seconds": settings.storage.recovery_timeout_seconds,
            "writes": "one new backup directory; source table writes pause while the snapshot is captured"}


def backup(settings, db):
    with _bounded(settings.storage.recovery_timeout_seconds):
        return _backup(settings, db)


def _backup(settings, db):
    _client("pg_dump")
    root = _root(settings)
    root.mkdir(parents=True, exist_ok=True)
    directory = root / ("backup-" + uuid.uuid4().hex)
    directory.mkdir(mode=0o700)
    archive = directory / "archive.dump"
    try:
        with db.collection_lock():
            with db.connection() as conn, _watch_connection(conn):
                conn.execute("SELECT pg_advisory_xact_lock(734801001)")
                # Match enable/disable before freezing their event/job tables.
                conn.execute("SELECT singleton FROM schedule_state WHERE singleton FOR UPDATE")
                identity = _source_schema(conn)
                names = _table_names(conn, identity["schema"])
                conn.execute(sql.SQL("LOCK TABLE {} IN SHARE MODE").format(sql.SQL(",").join(
                    sql.Identifier(identity["schema"], name) for name in names)))
                snapshot = current_manifest(db, conn=conn)
                exported = conn.execute("SELECT pg_export_snapshot() AS snapshot").fetchone()["snapshot"]
                _run_client("pg_dump", ["--format=custom", "--no-owner", "--no-privileges", "--strict-names",
                                       "--schema=" + '"' + identity["schema"].replace('"', '""') + '"',
                                       "--snapshot=" + exported, "--file=" + str(archive)], db._dsn,
                            timeout=_remaining(),
                            watched_file=archive, max_bytes=settings.storage.backup_max_bytes)
        archive.chmod(0o600)
        manifest = {"format_version": FORMAT_VERSION, "status": "completed", "backup_id": directory.name,
                    "completed_at": datetime.now(timezone.utc).isoformat(), "application_version": __version__,
                    "recovery_timeout_seconds": settings.storage.recovery_timeout_seconds,
                    "archive_bytes": archive.stat().st_size, "archive_sha256": _file_hash(archive), "snapshot": snapshot}
        _write_json(directory / "manifest.json", manifest)
        return {"status": "completed", "backup_id": directory.name, "backup_path": str(directory),
                "archive_bytes": manifest["archive_bytes"], "archive_sha256": manifest["archive_sha256"],
                "source_snapshot_sha256": snapshot["contents_sha256"], "tables": len(snapshot["tables"]),
                "captures": snapshot["captures"]["count"], "restore_verified": False}
    except (Exception, KeyboardInterrupt) as error:
        _failure(directory, "backup", error)
        raise


class ScratchDatabase(Database):
    def __init__(self, dsn, schema, *, restore_writes=False):
        super().__init__(dsn)
        self.schema = schema
        self._restore_writes = restore_writes

    @contextmanager
    def connection(self):
        with super().connection() as conn:
            if self._restore_writes:
                conn.execute("SET TRANSACTION READ WRITE")
            conn.execute(sql.SQL("SET LOCAL search_path TO {}").format(sql.Identifier(self.schema)))
            yield conn


def _replay(conn, *, write):
    count = 0
    with conn.cursor(name="recovery_replay_" + uuid.uuid4().hex, row_factory=dict_row) as cursor:
        cursor.execute("SELECT * FROM capture ORDER BY received_at,capture_id")
        for row in cursor:
            _remaining()
            adapter = REGISTRY.get(row["source"])
            if adapter is None or adapter.VERSION != row["source_version"]:
                raise DatabaseError("A capture requires an unavailable parser version. Restore the matching application version before using this backup.")
            if write:
                projections.project(conn, row)
            projections.verify(conn, row)
            count += 1
    projected = projections.count(conn)
    if count != projected:
        raise DatabaseError("Restored projection counts differ from canonical captures. Inspect the restore before changing storage.")
    return {"captures_replayed": count, "projections_verified": projected}


def _scratch_dsn(dsn, name):
    if not isinstance(name, str) or not _SCRATCH_NAME.fullmatch(name):
        raise DatabaseError("The restore proof names an invalid scratch destination. Use a proof returned by backup restore.")
    return make_conninfo(dsn, dbname=name)


def _guard_read_only(dsn, schema):
    try:
        with psycopg.connect(dsn, connect_timeout=5, options="-c statement_timeout=15000") as conn, _watch_connection(conn):
            mode = conn.execute("SHOW default_transaction_read_only").fetchone()[0]
            if mode != "on":
                raise DatabaseError("Scratch read-only protection is absent. Restore into a new scratch database before using this proof.")
            try:
                conn.execute(sql.SQL("UPDATE {}.schema_migration SET version=version WHERE false").format(sql.Identifier(schema)))
            except psycopg.errors.ReadOnlySqlTransaction:
                conn.rollback()
                return True
            raise DatabaseError("Scratch read-only protection did not reject writes. Do not use this restore proof.")
    except psycopg.Error:
        raise DatabaseError("Cannot verify the scratch database read-only guard. Check database availability and the restore proof.") from None


def restore_verify(settings, db, backup_id=None):
    with _bounded(settings.storage.recovery_timeout_seconds):
        return _restore_verify(settings, db, backup_id)


def _restore_selection(directory, scratch, schema):
    """Reuse only template0's verified empty public schema; never remove it.

    The archive remains immutable. A derived TOC retains every object except
    the CREATE entry for the public namespace that the new database owns.
    """
    if schema != "public":
        return []
    with scratch.connection() as conn, _watch_connection(conn):
        row = conn.execute("""SELECT n.oid,(SELECT count(*) FROM pg_depend d
            WHERE d.refclassid='pg_namespace'::regclass AND d.refobjid=n.oid AND d.deptype='n') AS objects
            FROM pg_namespace n WHERE n.nspname='public'""").fetchone()
        if row is None or row["objects"] != 0:
            raise DatabaseError("The new scratch database's public schema is not empty. Keep it for inspection; recovery will not replace or remove an existing schema.")
    token = uuid.uuid4().hex
    toc = directory / ("restore-toc-" + token + ".list")
    selection = directory / ("restore-selection-" + token + ".list")
    if toc.exists() or selection.exists():
        raise DatabaseError("A generated restore selection path already exists. Retry with a new selection; no existing output was overwritten.")
    _run_client("pg_restore", ["--list", "--file=" + str(toc), str(directory / "archive.dump")], scratch._dsn,
                timeout=_remaining(), watched_file=toc, max_bytes=MAX_MANIFEST_BYTES)
    contents = toc.read_bytes().splitlines(keepends=True)
    public_creation = re.compile(rb"^[0-9]+;\s+[0-9]+\s+[0-9]+\s+SCHEMA\s+-\s+public(?:\s|$)")
    selected = [line for line in contents if public_creation.match(line)]
    if len(selected) != 1:
        raise DatabaseError("The backup's public-schema creation entry could not be identified uniquely. Use an intact backup from the pinned PostgreSQL client; no schema entry was guessed.")
    with selection.open("xb") as stream:
        stream.write(b"; Game Census reuses the verified empty template0 public schema.\n")
        stream.writelines(line for line in contents if not public_creation.match(line))
        stream.flush()
        os.fsync(stream.fileno())
    toc.chmod(0o600)
    selection.chmod(0o600)
    return ["--use-list=" + str(selection)]


def _restore_verify(settings, db, backup_id=None):
    directory, manifest = _selected(settings, backup_id)
    _client("pg_restore")
    scratch_name = "game_census_restore_" + uuid.uuid4().hex
    scratch_dsn = _scratch_dsn(db._dsn, scratch_name)
    schema = manifest["snapshot"]["source_identity"]["schema"]
    scratch = ScratchDatabase(scratch_dsn, schema, restore_writes=True)
    try:
        with psycopg.connect(db._dsn, connect_timeout=5, autocommit=True, options="-c statement_timeout=15000") as control, _watch_connection(control):
            control.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(scratch_name)))
            # Set protection before any restored source state exists. Only the
            # private import/replay sessions opt into writes; failed restores
            # remain protected for every normal application connection.
            control.execute(sql.SQL("ALTER DATABASE {} SET default_transaction_read_only=on").format(sql.Identifier(scratch_name)))
        selection = _restore_selection(directory, scratch, schema)
        _run_client("pg_restore", ["--exit-on-error", "--single-transaction", "--no-owner", "--no-privileges",
                                  *selection, "--dbname=" + scratch_name, str(directory / "archive.dump")], db._dsn,
                    timeout=_remaining(), database=scratch_name, restore_writes=True)
        with scratch.connection() as conn, _watch_connection(conn):
            restored = current_manifest(scratch, conn=conn)
            if restored["contents_sha256"] != manifest["snapshot"]["contents_sha256"]:
                raise DatabaseError("Restored canonical tables, sequence states or checksums differ from the source snapshot. Keep the source; this restore cannot authorize a cutover.")
            replay = _replay(conn, write=True)
            after = current_manifest(scratch, conn=conn)
            if after["contents_sha256"] != restored["contents_sha256"]:
                raise DatabaseError("Projection replay changed the restored snapshot. Inspect missing or inconsistent projections before changing source storage.")
        _guard_read_only(scratch_dsn, schema)
        proof = {"format_version": FORMAT_VERSION, "status": "verified", "backup_id": directory.name,
                 "verified_at": datetime.now(timezone.utc).isoformat(), "scratch_database": scratch_name,
                 "scratch_schema": schema, "scratch_read_only": True, "manifest_sha256": _file_hash(directory / "manifest.json"),
                 "archive_sha256": manifest["archive_sha256"], "source_identity": manifest["snapshot"]["source_identity"],
                 "source_snapshot_sha256": manifest["snapshot"]["contents_sha256"], "replay": replay}
        proof_path = directory / ("verification-" + uuid.uuid4().hex + ".json")
        _write_json(proof_path, proof)
        return {"status": "verified", "backup_id": directory.name, "proof_id": str(proof_path),
                "scratch_database": scratch_name, "scratch_read_only": True,
                "source_snapshot_sha256": proof["source_snapshot_sha256"], "tables_verified": len(restored["tables"]), **replay}
    except psycopg.Error:
        error = DatabaseError("PostgreSQL could not create, restore or protect the new scratch database. Check storage.database_url and CREATE DATABASE permissions; the original database was not overwritten.")
        _failure(directory, "restore", error, scratch_database=scratch_name)
        raise error from None
    except (Exception, KeyboardInterrupt) as error:
        _failure(directory, "restore", error, scratch_database=scratch_name)
        raise


def require_verified_snapshot(db, proof_id, *, conn=None, timeout_seconds=None):
    path = Path(proof_id)
    if not path.is_absolute() or not _PROOF_NAME.fullmatch(path.name) or path.is_symlink():
        raise DatabaseError("proof_id must be the absolute immutable proof path returned by backup restore.")
    if timeout_seconds is None:
        timeout_seconds = _read_json(path.parent / "manifest.json").get("recovery_timeout_seconds")
    if type(timeout_seconds) is not int or not 10 <= timeout_seconds <= 86400:
        raise DatabaseError("storage.recovery_timeout_seconds is invalid in configuration or the backup manifest. Create a backup using validated configuration.")
    with _bounded(timeout_seconds):
        return _require_verified_snapshot(db, path, conn=conn)


def _require_verified_snapshot(db, proof_id, *, conn=None):
    """Revalidate the actual scratch copy and locked source, not a success label."""
    path = Path(proof_id)
    if not path.is_absolute() or not _PROOF_NAME.fullmatch(path.name) or path.is_symlink():
        raise DatabaseError("proof_id must be the absolute immutable proof path returned by backup restore.")
    proof = _read_json(path)
    manifest_path, archive = path.parent / "manifest.json", path.parent / "archive.dump"
    manifest = _read_json(manifest_path)
    if (proof.get("status") != "verified" or proof.get("format_version") != FORMAT_VERSION
            or not isinstance(proof.get("scratch_schema"), str)
            or proof.get("source_snapshot_sha256") != manifest.get("snapshot", {}).get("contents_sha256")
            or proof.get("source_identity") != manifest.get("snapshot", {}).get("source_identity")
            or proof.get("backup_id") != manifest.get("backup_id")
            or _file_hash(manifest_path) != proof.get("manifest_sha256")
            or archive.is_symlink() or not archive.is_file() or archive.stat().st_size != manifest.get("archive_bytes")
            or _file_hash(archive) != proof.get("archive_sha256")
            or manifest.get("archive_sha256") != proof.get("archive_sha256")):
        raise DatabaseError("The restore proof or retained backup changed. Create and verify a new backup before storage migrate.")
    scratch_dsn = _scratch_dsn(db._dsn, proof.get("scratch_database"))
    _guard_read_only(scratch_dsn, proof["scratch_schema"])
    scratch = ScratchDatabase(scratch_dsn, proof["scratch_schema"])
    with scratch.connection() as restored_conn, _watch_connection(restored_conn):
        restored = current_manifest(scratch, conn=restored_conn)
        _replay(restored_conn, write=False)
    if restored["contents_sha256"] != proof.get("source_snapshot_sha256"):
        raise DatabaseError("The verified scratch contents changed. Restore and verify a new scratch copy before storage migrate.")
    current = current_manifest(db, conn=conn)
    if current["source_identity"] != proof.get("source_identity") or current["contents_sha256"] != proof.get("source_snapshot_sha256"):
        raise DatabaseError("The source changed after the verified backup snapshot. Create and restore a new backup before storage migrate; no source data was removed.")
    return {"status": "verified", "proof_id": str(path), "source_snapshot_sha256": current["contents_sha256"],
            "scratch_database": proof["scratch_database"], "scratch_read_only": True,
            "tables_verified": len(current["tables"]), "captures_verified": current["captures"]["count"]}
