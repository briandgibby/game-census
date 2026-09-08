"""Source status, cardinality bounds, cheap readiness and archive restore proofs."""
from datetime import datetime, timezone
from pathlib import Path
import json
import httpx
import pytest
from fastapi.testclient import TestClient

from game_census import archive, recovery, operations
from game_census.collector import collect_once
from game_census.db import DatabaseError
from game_census.sources import REGISTRY
from game_census.web import create_app
from test_bootstrap import scratch_database, transport
from test_enrichment import fetch, retain


@pytest.mark.integration
def test_all_sources_and_fixed_cardinality_metrics_are_stored_reads(scratch_database, monkeypatch):
    db, settings = scratch_database
    collect_once(settings, db, transport=transport(status=503))
    retain(db, fetch('reviews'))
    monkeypatch.setattr(httpx.Client, 'stream', lambda *a, **kw: pytest.fail('Read endpoint must not contact Steam'))
    report = operations.report(settings, db)
    assert len(report['sources']) == len(REGISTRY)
    assert sum(s['attempts_24h']['failed'] for s in report['sources']) == 1
    assert sum(s['attempts_24h']['succeeded'] for s in report['sources']) == 1
    metrics = operations.metrics(report)
    assert len([line for line in metrics.splitlines() if not line.startswith('#')]) == len(REGISTRY)*4+1
    assert 'appid=' not in metrics and 'fixture-key' not in metrics
    client = TestClient(create_app(settings, db))
    assert client.get('/health/ready').json() == {'status':'ready'}
    assert client.get('/api/v1/operations').status_code == 200
    assert client.get('/metrics').status_code == 200
    monkeypatch.setattr(db, 'status', lambda: pytest.fail('Readiness must not scan complete capture history'))
    assert client.get('/health/ready').status_code == 200


@pytest.mark.parametrize('month',['2026-99','../backup','',datetime.now(timezone.utc).strftime('%Y-%m')])
def test_archive_requires_closed_utc_month(month):
    with pytest.raises(DatabaseError, match='closed UTC month'):
        archive.bounds(month)


@pytest.mark.integration
def test_archive_adoption_requires_restore_and_preserves_query_copy(scratch_database, tmp_path):
    db, settings = scratch_database
    settings.storage.backup_path = str(tmp_path)
    retain(db, fetch('reviews'), datetime(2025,1,2,tzinfo=timezone.utc))
    preview = archive.plan(settings,db,'2025-01')
    assert preview['manifest']['capture_count'] == 1
    with pytest.raises(DatabaseError):
        archive.adopt(settings,db,'2025-01',str(tmp_path/'missing-proof.json'))
    saved = recovery.backup(settings,db)
    restored = recovery.restore_verify(settings,db,saved['backup_id'])
    result = archive.adopt(settings,db,'2025-01',restored['proof_id'])
    assert result['status'] == 'adopted' and result['primary_query_copies_retained']
    assert archive.status(db)['owners'][0]['backup_id'] == saved['backup_id']
    assert db.rebuild()['captures_replayed'] == 1
    assert db.enrichment_history(570,settings,'reviews')['observations'][0]['value']['total_reviews'] == 100
    with pytest.raises(DatabaseError, match='already has an archive owner'):
        archive.adopt(settings,db,'2025-01',restored['proof_id'])
    with pytest.raises(DatabaseError):
        retain(db, fetch('reviews'), datetime(2025,1,3,tzinfo=timezone.utc))
    # The actual regenerate command remains usable after adoption.
    again = recovery.restore_verify(settings,db,saved['backup_id'])
    assert again['captures_replayed'] == again['projections_verified'] == 1


@pytest.mark.integration
def test_archive_rejects_stale_restore_proof(scratch_database, tmp_path):
    db, settings = scratch_database
    settings.storage.backup_path = str(tmp_path)
    retain(db, fetch('reviews'), datetime(2025,1,2,tzinfo=timezone.utc))
    saved = recovery.backup(settings,db)
    restored = recovery.restore_verify(settings,db,saved['backup_id'])
    retain(db, fetch('reviews'), datetime(2025,1,3,tzinfo=timezone.utc))
    with pytest.raises(DatabaseError, match='source changed'):
        archive.adopt(settings,db,'2025-01',restored['proof_id'])
    assert archive.status(db)['owners'] == []


@pytest.mark.integration
def test_extended_capacity_tool_with_small_configured_workload(scratch_database, monkeypatch):
    from test_capacity import test_partitioned_history_capacity
    db, settings = scratch_database
    settings.benchmark.app_count=1
    settings.benchmark.history_days=1
    settings.benchmark.requests_per_second=1
    settings.benchmark.duration_seconds=2
    settings.benchmark.workers=1
    monkeypatch.setenv('GAME_CENSUS_CAPACITY','1')
    test_partitioned_history_capacity((db,settings),monkeypatch)


def test_benchmark_failures_stay_in_denominator():
    from game_census.benchmark import measure
    value=measure(lambda index: (_ for _ in ()).throw(ValueError('fixture-secret')),requests_per_second=1,seconds=1,workers=1)
    assert value['requests']==value['failed_requests']==1
    assert 'fixture-secret' not in json.dumps(value)
    assert value['error_types']==['ValueError']
