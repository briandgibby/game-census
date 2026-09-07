"""Monthly PostgreSQL storage with guarded, atomic legacy conversion.

The capture ledger is canonical. capture_identity is its derived routing index.
After conversion the frozen *_legacy_004 tables are derived migration copies;
they are never read by the application and never deleted by maintenance.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import uuid

from psycopg import sql
from psycopg.types.json import Jsonb

from .db import DatabaseError


_MONTH = re.compile(r"([0-9]{4})-(0[1-9]|1[0-2])\Z")


def month_start(value):
    if not isinstance(value,datetime) or value.tzinfo is None:
        raise DatabaseError("Partition timestamps must include a UTC offset. Correct the capture timestamp before storage.")
    value=value.astimezone(timezone.utc)
    if not 1970 <= value.year <= 9998:
        raise DatabaseError("Partition timestamps must be in years 1970 through 9998. Inspect the source timestamp.")
    return value.replace(day=1,hour=0,minute=0,second=0,microsecond=0)


def add_months(value,months):
    year, month = divmod(value.year*12+value.month-1+months,12)
    if not 1970 <= year <= 9999:
        raise DatabaseError("Partition month range exceeds supported UTC years. Request a smaller storage range.")
    return value.replace(year=year,month=month+1)


def _layout(conn):
    return conn.execute("SELECT layout FROM storage_layout WHERE singleton").fetchone()["layout"]


def _tables(conn):
    return (("capture","received_at"),("player_sample","observed_at")) if _layout(conn)=="partitioned" else (
        ("capture_partitioned","received_at"),("player_sample_partitioned","observed_at"))


def ensure_month(conn, received_at):
    """Ensure only one receipt month, including an explicitly late capture.

    The schema lock serializes partition creation. There is no DEFAULT partition
    that could silently accumulate an unbounded unmaintained catch-all table.
    """
    start=month_start(received_at)
    end=add_months(start,1)
    conn.execute("SELECT pg_advisory_xact_lock(734801004)")
    created=[]
    for parent,_ in _tables(conn):
        # Child names remain stable when a staged parent is renamed at cutover.
        stem="capture" if parent.startswith("capture") else "player_sample"
        name=f"{stem}_p{start.year:04d}{start.month:02d}"
        existing=conn.execute("SELECT to_regclass(%s) AS relation",(name,)).fetchone()["relation"]
        if existing is not None:
            attached=conn.execute("SELECT 1 FROM pg_inherits WHERE inhrelid=%s::regclass AND inhparent=%s::regclass",(name,parent)).fetchone()
            if attached is None:
                raise DatabaseError("A partition name belongs to another table. Inspect storage plan before correcting schema ownership.")
            continue
        conn.execute(sql.SQL("CREATE TABLE {} PARTITION OF {} FOR VALUES FROM ({}) TO ({})").format(
            sql.Identifier(name),sql.Identifier(parent),sql.Literal(start),sql.Literal(end)))
        created.append(name)
    return created


def register_capture(conn,capture_id,attempt_id,capture):
    """Prepare a canonical capture insert in the caller's atomic transaction."""
    if _layout(conn)!="partitioned":
        return
    from .cache import lock_app
    # Cached readers take this app lock before touching source parents. A new
    # month's DDL must use the same order before acquiring exclusive parent locks.
    if capture.app_id is not None:
        lock_app(conn,capture.app_id)
    ensure_month(conn,capture.received_at)
    conn.execute("INSERT INTO capture_identity(capture_id,attempt_id,received_at) VALUES(%s,%s,%s)",
                 (capture_id,attempt_id,capture.received_at))


def _retarget_capture_references(conn):
    # Old constraints enforced UUID existence in the former capture table. The
    # global registry now enforces that identity across every receipt partition.
    for table in ("app_name","scheduled_job","discovery_snapshot"):
        if conn.execute("SELECT to_regclass(%s) AS relation", (table,)).fetchone()["relation"] is None:
            continue
        constraints=conn.execute("""SELECT conname FROM pg_constraint WHERE conrelid=%s::regclass
            AND contype='f' AND confrelid='capture_legacy_004'::regclass""",(table,)).fetchall()
        for constraint in constraints:
            conn.execute(sql.SQL("ALTER TABLE {} DROP CONSTRAINT {}").format(sql.Identifier(table),sql.Identifier(constraint["conname"])))
        conn.execute(sql.SQL("ALTER TABLE {} ADD CONSTRAINT {} FOREIGN KEY(capture_id) REFERENCES capture_identity(capture_id)").format(
            sql.Identifier(table),sql.Identifier(table+"_capture_identity_fk")))


def _freeze_legacy(conn):
    conn.execute("""CREATE OR REPLACE FUNCTION game_census_frozen_migration_copy() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN
            RAISE EXCEPTION 'Legacy migration copies are read-only; canonical history is in the partitioned capture ledger';
        END $$""")
    for table in ("capture_legacy_004","player_sample_legacy_004"):
        conn.execute(sql.SQL("CREATE TRIGGER {} BEFORE INSERT OR UPDATE OR DELETE OR TRUNCATE ON {} FOR EACH STATEMENT EXECUTE FUNCTION game_census_frozen_migration_copy()").format(
            sql.Identifier(table+"_frozen"),sql.Identifier(table)))
        conn.execute(sql.SQL("COMMENT ON TABLE {} IS {}").format(sql.Identifier(table),
                     sql.Literal("Derived frozen migration snapshot. Partitioned capture/player_sample are canonical/current projections. Retained indefinitely; never read by the application.")))


def _cutover(conn,report):
    conn.execute("ALTER TABLE capture RENAME TO capture_legacy_004")
    conn.execute("ALTER TABLE player_sample RENAME TO player_sample_legacy_004")
    conn.execute("ALTER TABLE capture_partitioned RENAME TO capture")
    conn.execute("ALTER TABLE player_sample_partitioned RENAME TO player_sample")
    _retarget_capture_references(conn)
    _freeze_legacy(conn)
    conn.execute("UPDATE storage_layout SET layout='partitioned',converted_at=clock_timestamp() WHERE singleton")
    transition_id=str(uuid.uuid4())
    report["source_manifests"]={table:_copy_manifest(conn,table+"_legacy_004") for table in ("capture","player_sample")}
    conn.execute("INSERT INTO storage_transition(transition_id,source_layout,target_layout,report) VALUES(%s,'legacy','partitioned',%s)",
                 (transition_id,Jsonb(report)))
    conn.execute("INSERT INTO storage_legacy_member(capture_id,transition_id) SELECT capture_id,%s FROM capture_legacy_004",(transition_id,))


def _lock_all_apps(conn):
    from .cache import lock_app
    if conn.execute("SELECT to_regprocedure('lock_rollup_app(bigint)') AS lock_function").fetchone()["lock_function"] is None:
        # Migration 004 runs before cache schema 005 on the first upgrade. No
        # cached reader exists yet; do not require a function not installed yet.
        if conn.execute("SELECT to_regclass('player_rollup_cache') AS cache").fetchone()["cache"] is not None:
            raise DatabaseError("The cache lock function is missing. Run initialize to repair the installed schema before storage migration.")
        return
    for row in conn.execute("SELECT app_id FROM app ORDER BY app_id").fetchall():
        lock_app(conn,row["app_id"])


def _lock_source_snapshot(conn):
    # A disable operation enters through this row before changing job/event
    # tables. Use its order before freezing the complete backup comparison set.
    conn.execute("SELECT singleton FROM schedule_state WHERE singleton FOR UPDATE")
    tables=conn.execute("""SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname=current_schema() AND c.relkind IN ('r','p')
        AND NOT EXISTS(SELECT 1 FROM pg_inherits i WHERE i.inhrelid=c.oid) ORDER BY c.relname""").fetchall()
    if tables:
        conn.execute(sql.SQL("LOCK TABLE {} IN SHARE MODE").format(sql.SQL(",").join(sql.Identifier(row["relname"]) for row in tables)))


def create_empty_layout(conn,*,partitioned=True):
    """Complete empty bootstrap after migration 004, before cache migration 005."""
    if not partitioned or _layout(conn)=="partitioned":
        return {"layout":_layout(conn),"changed":False}
    _lock_all_apps(conn)
    conn.execute("SELECT pg_advisory_xact_lock(734801004)")
    conn.execute("LOCK TABLE capture,player_sample,app_name,scheduled_job IN ACCESS EXCLUSIVE MODE")
    if conn.execute("SELECT 1 FROM capture LIMIT 1").fetchone() is not None:
        return {"layout":"legacy","changed":False,
                "next_action":"Run storage plan, create and verify a recovery backup, then storage migrate."}
    counts=conn.execute("SELECT (SELECT count(*) FROM player_sample)+(SELECT count(*) FROM app_name)+(SELECT count(*) FROM scheduled_job WHERE capture_id IS NOT NULL) AS n").fetchone()["n"]
    if counts:
        raise DatabaseError("Empty capture storage has dependent source projections. Verify a scratch restore before initialization.")
    now=conn.execute("SELECT clock_timestamp() AS now").fetchone()["now"]
    ensure_month(conn,now)
    _cutover(conn,{"status":"succeeded","reason":"empty_bootstrap","captures":0,
                   "restore_proof":"No pre-existing canonical captures or dependent projections exist."})
    return {"layout":"partitioned","changed":True}


def _range(settings,conn,start_month,months):
    if start_month is None:
        start=month_start(conn.execute("SELECT clock_timestamp() AS now").fetchone()["now"])
    else:
        match=_MONTH.fullmatch(start_month) if isinstance(start_month,str) else None
        if match is None or not 1970 <= int(match.group(1)) <= 9998:
            raise DatabaseError("start_month must be YYYY-MM in years 1970 through 9998. Correct the storage maintenance range.")
        start=datetime(int(match.group(1)),int(match.group(2)),1,tzinfo=timezone.utc)
    months=1+settings.storage.partition_months_ahead if months is None else months
    if type(months) is not int or not 1 <= months <= settings.storage.max_partition_months:
        raise DatabaseError("months must be from 1 through storage.max_partition_months. Request a smaller bounded maintenance range.")
    return [add_months(start,offset) for offset in range(months)]


def _partition_rows(conn):
    parents=[table for table,_ in _tables(conn)]
    return conn.execute("""SELECT p.relname AS parent,c.relname AS partition,
        pg_get_expr(c.relpartbound,c.oid) AS bounds,pg_total_relation_size(c.oid) AS total_bytes
        FROM pg_inherits i JOIN pg_class c ON c.oid=i.inhrelid JOIN pg_class p ON p.oid=i.inhparent
        WHERE i.inhparent=ANY(%s::regclass[]) ORDER BY p.relname,c.relname""",(parents,)).fetchall()


def plan(settings,db,*,start_month=None,months=None):
    """Print bounded writes, source ownership, size and the conversion gate."""
    with db.connection() as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        layout=_layout(conn)
        requested=_range(settings,conn,start_month,months)
        partitions=_partition_rows(conn)
        captures=conn.execute("""SELECT count(*) AS n FROM (SELECT 1 FROM capture LIMIT %s) bounded""",
                              (settings.storage.migration_max_captures+1,)).fetchone()["n"]
        exact=captures <= settings.storage.migration_max_captures
        scope=conn.execute("SELECT min(received_at) AS first,max(received_at) AS last FROM capture").fetchone()
        observed_months=conn.execute("""SELECT DISTINCT date_trunc('month',received_at AT TIME ZONE 'UTC') AT TIME ZONE 'UTC' AS month
            FROM capture ORDER BY month LIMIT %s""",(settings.storage.max_partition_months+1,)).fetchall()
        registry_bytes=conn.execute("SELECT pg_total_relation_size('capture_identity') AS n").fetchone()["n"]
        legacy_bytes=conn.execute("""SELECT COALESCE(sum(pg_total_relation_size(c.oid)),0) AS n FROM pg_class c
            WHERE c.oid IN(to_regclass('capture_legacy_004'),to_regclass('player_sample_legacy_004'))""").fetchone()["n"]
    existing={row["partition"] for row in partitions}
    needed=[f"{parent}_p{at.year:04d}{at.month:02d}" for at in requested for parent in ("capture","player_sample")
            if f"{parent}_p{at.year:04d}{at.month:02d}" not in existing]
    return {"status":"planned","layout":layout,"canonical_owner":"capture",
            "retention":"indefinite; maintenance never detaches or deletes source partitions",
            "capture_count":captures if exact else None,"capture_count_exceeds_migration_limit":not exact,
            "first_capture_at":scope["first"].isoformat() if scope["first"] else None,
            "last_capture_at":scope["last"].isoformat() if scope["last"] else None,
            "partitions":partitions,"requested_months":[at.strftime("%Y-%m") for at in requested],
            "would_create":needed,"maximum_new_tables":len(needed),
            "identity_registry_bytes":registry_bytes,"retained_legacy_copy_bytes":int(legacy_bytes),
            "migration_receipt_months":[row["month"].strftime("%Y-%m") for row in observed_months],
            "migration_month_limit_exceeded":len(observed_months)>settings.storage.max_partition_months,
            "migration_requires_verified_backup":layout=="legacy" and captures>0,
            "migration_admitted":exact and len(observed_months)<=settings.storage.max_partition_months,
            "next_action":("Maintain partitions for the configured bounded range." if layout=="partitioned" else
                           "Create a backup, restore and verify it in scratch, then migrate using that proof; current captures remain readable.")}


def maintain(settings,db,*,start_month=None,months=None):
    with db.collection_lock():
        with db.connection() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(734801004)")
            requested=_range(settings,conn,start_month,months)
            if _layout(conn)!="partitioned":
                raise DatabaseError("Storage still uses legacy tables. Run storage plan and complete verified storage migration before partition maintenance.")
            created=[]
            for at in requested:
                created.extend(ensure_month(conn,at))
    return {"status":"succeeded","months":[at.strftime("%Y-%m") for at in requested],"created_partitions":created,
            "canonical_history_removed":False}


def _copy_manifest(conn,table):
    digest=hashlib.sha256()
    rows=0
    # Stable textual COPY represents every original field including payload bytes.
    with conn.cursor().copy(sql.SQL("COPY (SELECT * FROM {} ORDER BY capture_id) TO STDOUT").format(sql.Identifier(table))) as copy:
        for chunk in copy:
            digest.update(bytes(chunk))
            rows+=bytes(chunk).count(b"\n")
    return {"rows":rows,"sha256":digest.hexdigest()}


def migrate(settings,db,*,proof_id):
    from .recovery import require_verified_snapshot
    with db.collection_lock():
        with db.connection() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(734801001)")
            _lock_all_apps(conn)
            conn.execute("SELECT pg_advisory_xact_lock(734801004)")
            _lock_source_snapshot(conn)
            conn.execute("LOCK TABLE capture,player_sample,app_name,scheduled_job IN ACCESS EXCLUSIVE MODE")
            if _layout(conn)=="partitioned":
                return {"status":"succeeded","layout":"partitioned","changed":False}
            proof=require_verified_snapshot(db,proof_id,conn=conn,timeout_seconds=settings.storage.recovery_timeout_seconds)
            count=conn.execute("SELECT count(*) AS n FROM (SELECT 1 FROM capture LIMIT %s) bounded",
                               (settings.storage.migration_max_captures+1,)).fetchone()["n"]
            if count>settings.storage.migration_max_captures:
                raise DatabaseError("Legacy conversion exceeds storage.migration_max_captures. Raise that setting only after a bounded scratch restore measurement.")
            before={table:_copy_manifest(conn,table) for table in ("capture","player_sample")}
            months=conn.execute("""SELECT DISTINCT date_trunc('month',received_at AT TIME ZONE 'UTC') AT TIME ZONE 'UTC' AS month
                FROM capture ORDER BY month LIMIT %s""",(settings.storage.max_partition_months+1,)).fetchall()
            if len(months)>settings.storage.max_partition_months:
                raise DatabaseError("Legacy history exceeds storage.max_partition_months. Raise that setting after a measured scratch conversion.")
            for row in months:
                ensure_month(conn,row["month"])
            now=conn.execute("SELECT clock_timestamp() AS now").fetchone()["now"]
            ensure_month(conn,now)
            conn.execute("INSERT INTO capture_identity(capture_id,attempt_id,received_at) SELECT capture_id,attempt_id,received_at FROM capture")
            conn.execute("INSERT INTO capture_partitioned SELECT * FROM capture")
            conn.execute("INSERT INTO player_sample_partitioned SELECT * FROM player_sample")
            after={table:_copy_manifest(conn,table+"_partitioned") for table in ("capture","player_sample")}
            if before!=after:
                raise DatabaseError("Partitioned copies differ from canonical legacy data. The transaction was rolled back; inspect the scratch restore proof.")
            # Validate the new registry/partition foreign keys before swapping names.
            conn.execute("SET CONSTRAINTS ALL IMMEDIATE")
            report={"status":"succeeded","changed":True,"layout":"partitioned","captures":count,
                    "source_manifests":before,"copied_manifests":after,
                    "verified_backup":proof,"legacy_copy_purpose":"derived frozen migration snapshot; application reads only partitioned tables",
                    "canonical_history_removed":False}
            _cutover(conn,report)
            cache_migration=Path(__file__).parent/"migrations"/"005_rollups.sql"
            if cache_migration.exists():
                from .cache import reinitialize
                reinitialize(conn)
    return report


def verify_legacy(settings,db):
    """Rebuild frozen migration copies into a new scratch schema and compare.

    Membership is canonical migration audit; fields and parser results derive
    solely from the current capture owner. Later captures do not enter this copy.
    """
    from .projections import project
    with db.collection_lock():
        with db.connection() as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            if _layout(conn)!="partitioned":
                raise DatabaseError("Legacy copies do not exist before conversion. Run storage plan and complete a verified migration first.")
            transition=conn.execute("SELECT transition_id,report FROM storage_transition WHERE target_layout='partitioned' ORDER BY recorded_at DESC LIMIT 1").fetchone()
            if transition is None:
                raise DatabaseError("The partitioned storage transition audit is missing. Verify a recovery backup before inspecting migration copies.")
            count=conn.execute("SELECT count(*) AS n FROM (SELECT 1 FROM storage_legacy_member LIMIT %s) bounded",
                               (settings.storage.migration_max_captures+1,)).fetchone()["n"]
            if count>settings.storage.migration_max_captures:
                raise DatabaseError("Legacy-copy verification exceeds storage.migration_max_captures. Use a measured higher bound before rebuilding this snapshot.")
            original={table:_copy_manifest(conn,table+"_legacy_004") for table in ("capture","player_sample")}
            if original!=transition["report"].get("source_manifests"):
                raise DatabaseError("A frozen legacy copy differs from its migration audit. Preserve it and inspect the verified recovery snapshot.")
            source_schema=conn.execute("SELECT current_schema() AS schema").fetchone()["schema"]
            scratch="game_census_legacy_verify_"+uuid.uuid4().hex
            conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(scratch)))
            conn.execute(sql.SQL("""CREATE TABLE {}.capture AS SELECT c.* FROM capture c
                JOIN storage_legacy_member m USING(capture_id) WHERE m.transition_id=%s""").format(sql.Identifier(scratch)),
                         (transition["transition_id"],))
            for table in ("player_sample","app_name","discovery_snapshot","catalog_entry","source_policy","catalog_checkpoint"):
                if conn.execute("SELECT to_regclass(%s) AS relation", (table,)).fetchone()["relation"] is None:
                    continue
                conn.execute(sql.SQL("CREATE TABLE {}.{} (LIKE {} INCLUDING DEFAULTS INCLUDING CONSTRAINTS INCLUDING INDEXES)").format(
                    sql.Identifier(scratch),sql.Identifier(table),sql.Identifier(table)))
            conn.execute(sql.SQL("SET LOCAL search_path TO {}").format(sql.Identifier(scratch)))
            with conn.cursor(name="legacy_replay_"+uuid.uuid4().hex) as cursor:
                cursor.execute("SELECT * FROM capture ORDER BY capture_id")
                for capture in cursor:
                    project(conn,capture)
            rebuilt={table:_copy_manifest(conn,table) for table in ("capture","player_sample")}
            metadata=conn.execute("SELECT count(*) AS n FROM app_name").fetchone()["n"]
            conn.execute(sql.SQL("SET LOCAL search_path TO {}").format(sql.Identifier(source_schema)))
            if rebuilt!=original:
                raise DatabaseError("Regenerated legacy captures or projections differ from the migration snapshot. No source changes were committed; inspect retained parser versions and recovery evidence.")
    return {"status":"succeeded","scratch_schema":scratch,"captures_replayed":count,
            "metadata_projections_verified":metadata,"source_manifests":original,"rebuilt_manifests":rebuilt,
            "canonical_history_changed":False,"legacy_copies_changed":False}
