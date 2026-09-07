"""Deterministic, idempotent projection generation from versioned retained captures."""
import hashlib
from psycopg import sql
from psycopg.types.json import Jsonb
from .sources import REGISTRY, SourceError, players, store


def _parse(capture):
    payload = bytes(capture["payload"])
    if hashlib.sha256(payload).hexdigest() != capture["checksum"]:
        raise SourceError("capture_checksum_mismatch", "A retained capture checksum did not match its payload.",
                          "Stop collection and verify a scratch restore before repairing canonical history.")
    adapter = REGISTRY.get(capture["source"])
    if adapter is None or adapter.VERSION != capture["source_version"]:
        raise SourceError("unknown_parser_version", "A retained capture requires an unavailable source parser.",
                          "Restore the matching application version before rebuilding projections.")
    value = adapter.parse(payload, capture["app_id"])
    return adapter, value


def project(conn, capture: dict) -> None:
    adapter, value = _parse(capture)
    if adapter is players:
        conn.execute("""INSERT INTO player_sample(capture_id,app_id,observed_at,player_count,parser_version)
            SELECT %s,%s,%s,%s,%s WHERE NOT EXISTS(SELECT 1 FROM player_sample WHERE capture_id=%s)
            ON CONFLICT DO NOTHING""",
            (capture["capture_id"], capture["app_id"], capture["received_at"], value, adapter.VERSION,capture["capture_id"]))
    elif adapter is store:
        conn.execute("""INSERT INTO app_name(capture_id,app_id,observed_at,name,parser_version)
            SELECT %s,%s,%s,%s,%s WHERE NOT EXISTS(SELECT 1 FROM app_name WHERE capture_id=%s)
            ON CONFLICT (capture_id) DO NOTHING""",
            (capture["capture_id"], capture["app_id"], capture["received_at"], value, adapter.VERSION,capture["capture_id"]))
    else:
        conn.execute("""INSERT INTO discovery_snapshot(capture_id,source,observed_at,parameters,value,parser_version)
            VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
            (capture["capture_id"], capture["source"], capture["received_at"], Jsonb(capture["parameters"]), Jsonb(value), adapter.VERSION))
        for item in value["items"]:
            conn.execute("INSERT INTO catalog_entry(capture_id,app_id,name) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                         (capture["capture_id"], item["app_id"], item["name"]))
        from .sources import catalog as catalog_source
        if adapter is catalog_source:
            from . import catalog
            catalog.project(conn, capture, value)


def verify(conn, capture: dict) -> None:
    """One canonical comparison used by replay and scratch-restore verification."""
    from .db import DatabaseError
    adapter, expected = _parse(capture)
    if adapter in (players, store):
        table, field = ('player_sample', 'player_count') if adapter is players else ('app_name', 'name')
        row = conn.execute(sql.SQL('SELECT app_id,observed_at,parser_version,{} AS value FROM {} WHERE capture_id=%s').format(
            sql.Identifier(field), sql.Identifier(table)), (capture['capture_id'],)).fetchone()
        valid = (row is not None and row['app_id'] == capture['app_id']
                 and row['observed_at'] == capture['received_at']
                 and row['parser_version'] == adapter.VERSION and row['value'] == expected)
    else:
        row = conn.execute('SELECT * FROM discovery_snapshot WHERE capture_id=%s', (capture['capture_id'],)).fetchone()
        entries = conn.execute('SELECT app_id,name FROM catalog_entry WHERE capture_id=%s ORDER BY app_id',
                               (capture['capture_id'],)).fetchall()
        expected_entries = sorted([{'app_id': item['app_id'], 'name': item['name']} for item in expected['items']],
                                  key=lambda item: item['app_id'])
        valid = (row is not None and row['source'] == capture['source']
                 and row['parameters'] == capture['parameters'] and row['value'] == expected
                 and row['observed_at'] == capture['received_at'] and row['parser_version'] == adapter.VERSION
                 and entries == expected_entries)
    if not valid:
        raise DatabaseError('A derived projection differs from its canonical capture. Keep the source and inspect a scratch restore before changing storage.')
    from .sources import catalog as catalog_source
    if adapter is catalog_source:
        from . import catalog
        catalog.verify(conn, capture, expected)


def count(conn) -> int:
    """Count one primary projection per capture, including pre-discovery backups."""
    total = 0
    for table in ('player_sample', 'app_name', 'discovery_snapshot'):
        if table == 'discovery_snapshot' and conn.execute('SELECT to_regclass(%s) AS relation', (table,)).fetchone()['relation'] is None:
            continue
        total += conn.execute(sql.SQL('SELECT count(*) AS n FROM {}').format(sql.Identifier(table))).fetchone()['n']
    return total
