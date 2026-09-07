"""Compatibility of both preserved releases, using bounded synthetic inputs only."""
from datetime import datetime, timezone
from pathlib import Path
import uuid

import httpx
import pytest
from psycopg import sql
from psycopg.types.json import Jsonb

from game_census import projections, recovery, storage
from game_census.collector import collect_discovery, collect_once
from game_census.db import DatabaseError
from game_census.sources import players
from game_census.sources.discovery import ADAPTERS, PLAYED
from game_census.sources.http import _LIMITS
from test_bootstrap import SchemaDatabase, scratch_database, transport
from test_details import response as detail_response
from test_discovery import chart_html
from test_storage import fixture_capture


def charts(settings, db):
    return collect_discovery(settings, db, 'charts', transport=httpx.MockTransport(
        lambda request: httpx.Response(200, content=chart_html())))


def historical_database(base, layout):
    schema = 'game_census_integration_' + uuid.uuid4().hex
    with base.connection() as conn:
        conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
    db = SchemaDatabase(base._dsn, schema)
    directory = Path(storage.__file__).parent / 'migrations'
    with db.connection() as conn:
        for name in ('001_initial.sql', '002_source_cooldown.sql'):
            conn.execute((directory / name).read_text())
        if layout == 'main':
            conn.execute((Path(__file__).parent / 'fixtures/main_discovery.sql').read_text())
        else:
            for name in ('003_scheduler.sql', '004_storage.sql'):
                conn.execute((directory / name).read_text())
            storage.create_empty_layout(conn)
            conn.execute((directory / '005_rollups.sql').read_text())
        conn.execute('INSERT INTO app(app_id) VALUES(570)')
        conn.execute('INSERT INTO tracking_interval(app_id,interval_seconds) VALUES(570,300)')
    return db


def old_capture(db, capture, *, partitioned=False):
    """Reproduce the released insert contract, not a production bootstrap step."""
    run = db.start_run([], [capture.source])
    attempt = db.reserve_attempt(run, capture.app_id, capture.source, 'webapi', 90000, 1)
    capture_id = str(uuid.uuid4())
    with db.connection() as conn:
        if partitioned:
            storage.register_capture(conn, capture_id, attempt, capture)
        conn.execute('''INSERT INTO capture(capture_id,attempt_id,run_id,app_id,source,source_version,
            request_started_at,received_at,http_status,parameters,payload,checksum,capture_form)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
            (capture_id, attempt, run, capture.app_id, capture.source, capture.source_version,
             capture.request_started_at, capture.received_at, capture.http_status, Jsonb(capture.parameters),
             capture.payload, capture.checksum, capture.capture_form))
        projections.project(conn, {'capture_id': capture_id, 'app_id': capture.app_id,
            'source': capture.source, 'source_version': capture.source_version,
            'received_at': capture.received_at, 'payload': capture.payload,
            'checksum': capture.checksum, 'parameters': capture.parameters})
        conn.execute("INSERT INTO request_result(attempt_id,status,http_status) VALUES(%s,'succeeded',200)", (attempt,))
    db.finish_run({'run_id': run, 'status': 'succeeded', 'request_count': 1})
    return capture_id


@pytest.mark.integration
def test_empty_partitioned_layout_accepts_global_captures_without_tracking(scratch_database):
    db, settings = scratch_database
    with db.connection() as conn:
        assert not conn.execute("SELECT attnotnull FROM pg_attribute WHERE attrelid='capture'::regclass AND attname='app_id'").fetchone()['attnotnull']
    assert charts(settings, db)['status'] == 'succeeded'
    assert db.rebuild()['captures_replayed'] == 2
    assert db.initialize([570], 300)['schema_version'] == 8
    with db.connection() as conn:
        assert conn.execute('SELECT app_id FROM app').fetchall() == [{'app_id': 570}]
        assert conn.execute('SELECT count(*) AS n FROM capture_identity').fetchone()['n'] == 2
        references = conn.execute("SELECT confrelid::regclass::text AS target FROM pg_constraint WHERE conrelid='discovery_snapshot'::regclass AND contype='f'").fetchall()
        assert references == [{'target': 'capture_identity'}]


@pytest.mark.integration
def test_main_upgrade_preserves_discovery_through_verified_cutover(scratch_database, tmp_path):
    base, settings = scratch_database
    settings.storage.backup_path = str(tmp_path)
    db = historical_database(base, 'main')
    old_capture(db, fixture_capture(datetime(2024, 12, 31, tzinfo=timezone.utc), 42))
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=chart_html()))) as client:
        capture = ADAPTERS[PLAYED].fetch(client, {}, 2000000)
    original = old_capture(db, capture)
    db.initialize([570], 300)
    before = db.rebuild()
    assert before['captures_replayed'] == 2
    saved = recovery.backup(settings, db)
    proof = recovery.restore_verify(settings, db, saved['backup_id'])
    changed = storage.migrate(settings, db, proof_id=proof['proof_id'])
    assert changed['source_manifests'] == changed['copied_manifests']
    assert db.rebuild() == before
    assert storage.verify_legacy(settings, db)['captures_replayed'] == 2
    assert charts(settings, db)['status'] == 'succeeded'
    with db.connection() as conn:
        assert conn.execute('SELECT capture_id FROM discovery_snapshot WHERE capture_id=%s', (original,)).fetchone()
        assert conn.execute('SELECT count(*) AS n FROM capture_legacy_004').fetchone()['n'] == 2
    assert db.rebuild()['captures_replayed'] == 4


@pytest.mark.integration
def test_p2_upgrade_preserves_player_identity_and_adds_discovery(scratch_database):
    base, settings = scratch_database
    db = historical_database(base, 'p2')
    original = old_capture(db, fixture_capture(datetime(2026, 1, 1, tzinfo=timezone.utc), 0), partitioned=True)
    before = recovery.current_manifest(db)['captures']
    assert db.initialize([570], 300)['schema_version'] == 8
    assert recovery.current_manifest(db)['captures'] == before
    assert charts(settings, db)['status'] == 'succeeded'
    assert db.rebuild()['captures_replayed'] == 3
    with db.connection() as conn:
        assert str(conn.execute('SELECT capture_id FROM player_sample').fetchone()['capture_id']) == original
        assert not conn.execute('SELECT enabled FROM schedule_state WHERE singleton').fetchone()['enabled']


@pytest.mark.integration
def test_mixed_sources_restore_and_replay_validate_every_projection(scratch_database, tmp_path):
    db, settings = scratch_database
    settings.storage.backup_path = str(tmp_path)
    settings.sources.store_metadata_enabled = True
    assert collect_once(settings, db, transport=transport(count=0))['status'] == 'succeeded'
    assert charts(settings, db)['status'] == 'succeeded'
    assert collect_discovery(settings, db, 'details', app_id=730, transport=httpx.MockTransport(detail_response))['status'] == 'succeeded'
    before = db.rebuild()
    assert before['captures_replayed'] == 8
    saved = recovery.backup(settings, db)
    proof = recovery.restore_verify(settings, db, saved['backup_id'])
    assert proof['captures_replayed'] == proof['projections_verified'] == 8
    assert recovery.require_verified_snapshot(db, proof['proof_id'])['status'] == 'verified'
    assert db.rebuild() == before


@pytest.mark.integration
def test_recovery_rejects_inconsistent_catalog_entries(scratch_database):
    db, settings = scratch_database
    charts(settings, db)
    # Rollback confines deliberate corruption to this fixture transaction.
    with db.connection() as conn:
        with pytest.raises(DatabaseError, match='projection|catalog'):
            with conn.transaction(force_rollback=True):
                conn.execute("UPDATE catalog_entry SET name='fixture corruption'")
                recovery._replay(conn, write=False)
    assert db.rebuild()['captures_replayed'] == 2


@pytest.mark.integration
def test_discovery_uses_shared_deadline_context_and_quota(scratch_database):
    db, settings = scratch_database
    inspected = []
    def respond(request):
        assert _LIMITS.get() is not None, 'Every production discovery request needs an absolute deadline'
        inspected.append(request.url.host)
        return httpx.Response(200, content=chart_html())
    settings.quota.store_rolling_24h = 1
    report = collect_discovery(settings, db, 'charts', transport=httpx.MockTransport(respond))
    assert report['status'] == 'partial'
    assert inspected == ['store.steampowered.com']
    assert report['sources'][1]['error']['code'] == 'quota_exhausted'
    assert db.status()['request_attempts_24h']['store'] == 1
