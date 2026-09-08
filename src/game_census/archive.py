"""Explicit closed-month ownership after a fresh, lossless scratch restore.

The verified immutable backup owns the selected month's captures. Primary rows
remain a query copy; this command does not prune either captures or projections.
"""
from datetime import datetime, timezone
import hashlib
from pathlib import Path
from psycopg.types.json import Jsonb

from . import recovery, storage
from .db import DatabaseError, iso


def bounds(month):
    try:
        if not isinstance(month,str) or not storage._MONTH.fullmatch(month):
            raise ValueError()
        start = datetime.strptime(month,'%Y-%m').replace(tzinfo=timezone.utc)
        end = storage.add_months(start,1)
        if end>storage.month_start(datetime.now(timezone.utc)):
            raise ValueError()
        return start,end
    except (ValueError,TypeError):
        raise DatabaseError('Archive month must be a closed UTC month in YYYY-MM format. The current month remains owned by primary storage.') from None


def month_manifest(conn, start, end):
    digest, count, versions = hashlib.sha256(),0,set()
    with conn.cursor(name='archive_month_manifest') as cursor:
        cursor.execute('SELECT capture_id,source,source_version,payload,checksum FROM capture WHERE received_at>=%s AND received_at<%s ORDER BY capture_id',(start,end))
        for row in cursor:
            if hashlib.sha256(bytes(row['payload'])).hexdigest()!=row['checksum']:
                raise DatabaseError('Capture checksum failed. Keep primary history and inspect a verified restore.')
            digest.update(f"{row['capture_id']}:{row['checksum']}\n".encode())
            versions.add((row['source'],row['source_version'])); count+=1
    return {'from':iso(start),'to':iso(end),'capture_count':count,'capture_sha256':digest.hexdigest(),
            'source_versions':[list(pair) for pair in sorted(versions)]}


def status(db):
    with db.connection() as conn:
        rows = conn.execute('SELECT * FROM archive_owner ORDER BY month').fetchall()
    return {'owners':[{**row,'month':row['month'].strftime('%Y-%m'),'adopted_at':iso(row['adopted_at'])} for row in rows],
            'unregistered_months_owner':'primary database','primary_query_copies_retained':True}


def plan(settings, db, month):
    start,end=bounds(month)
    with db.connection() as conn:
        manifest=month_manifest(conn,start,end)
        owner=conn.execute('SELECT backup_id FROM archive_owner WHERE month=%s',(start.date(),)).fetchone()
    return {'operation':'archive_adopt','month':month,'manifest':manifest,
            'existing_owner':owner['backup_id'] if owner else 'primary database',
            'requires':'matching immutable backup, verified read-only scratch restore, unchanged source snapshot, disabled schedule',
            'writes':'one ownership record; primary captures and projections remain available',
            'steam_requests':0,'canonical_history_deleted':False}


def adopt(settings, db, month, proof_id):
    start,end=bounds(month)
    with db.collection_lock(), db.connection() as conn:
        storage._lock_all_apps(conn)
        storage._lock_source_snapshot(conn)
        if conn.execute('SELECT enabled FROM schedule_state WHERE singleton').fetchone()['enabled']:
            raise DatabaseError('Disable scheduling before an archive ownership transition; keep the existing source and worker evidence.')
        if conn.execute('SELECT 1 FROM archive_owner WHERE month=%s',(start.date(),)).fetchone():
            raise DatabaseError('This month already has an archive owner. Inspect archive status; ownership records are not overwritten.')
        proof = recovery.require_verified_snapshot(db, proof_id, conn=conn, timeout_seconds=settings.storage.recovery_timeout_seconds)
        directory, full_manifest = recovery._selected(settings, proof['backup_id'])
        if Path(proof_id).resolve().parent != directory.resolve():
            raise DatabaseError('The restore proof is outside storage.backup_path. Use the verified proof for this configured archive location.')
        manifest = month_manifest(conn,start,end)
        if not manifest['capture_count']:
            raise DatabaseError('The selected month has no captures. Choose a closed month with recorded history.')
        scratch = recovery.ScratchDatabase(recovery._scratch_dsn(db._dsn,proof['scratch_database']),proof['scratch_schema'])
        with scratch.connection() as restored:
            if month_manifest(restored,start,end)!=manifest:
                raise DatabaseError('The selected archive month does not match restored history. Keep the primary copy.')
        manifest.update(archive_sha256=full_manifest['archive_sha256'],schema_version=full_manifest['snapshot']['schema_version'])
        conn.execute('INSERT INTO archive_owner(month,backup_id,proof_id,archive_path,manifest) VALUES(%s,%s,%s,%s,%s)',
                     (start.date(),proof['backup_id'],str(Path(proof_id).resolve()),str(directory/'archive.dump'),Jsonb(manifest)))
    return {'status':'adopted','month':month,'owner':proof['backup_id'],'manifest':manifest,
            'primary_query_copies_retained':True,'restore_command':f"backup restore --backup-id {proof['backup_id']}"}
