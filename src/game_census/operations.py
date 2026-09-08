"""Source health and fixed-cardinality metrics derived from durable ledgers."""
from datetime import datetime, timezone
from .db import iso
from .sources import REGISTRY


def report(settings, db):
    with db.connection() as conn:
        counts = conn.execute("""SELECT a.source,COALESCE(r.status,'uncertain') status,count(*) attempts
            FROM request_attempt a LEFT JOIN request_result r USING(attempt_id)
            WHERE a.dispatched_at>clock_timestamp()-interval '24 hours'
            GROUP BY a.source,r.status""").fetchall()
        totals = {(r['source'],r['status']):r['attempts'] for r in counts}
        sources = []
        for source, adapter in sorted(REGISTRY.items()):
            latest = conn.execute("""SELECT a.dispatched_at,r.status,r.error FROM request_attempt a
                LEFT JOIN request_result r USING(attempt_id) WHERE a.source=%s
                ORDER BY a.dispatched_at DESC LIMIT 1""", (source,)).fetchone()
            capture = conn.execute('SELECT max(received_at) at FROM capture WHERE source=%s', (source,)).fetchone()
            stop = conn.execute("""SELECT s.error FROM schedule_adapter_stop s JOIN schedule_state c ON c.plan_hash=s.plan_hash
                WHERE s.source=%s AND s.resolved_at IS NULL ORDER BY s.stopped_at DESC LIMIT 1""", (source,)).fetchone()
            sources.append({'source':source,'version':adapter.VERSION,'host_group':adapter.HOST_GROUP,
                            'last_success_at':iso(capture['at']), 'stopped':stop is not None,
                            'last_attempt':None if latest is None else {'at':iso(latest['dispatched_at']), 'status':latest['status'] or 'uncertain', 'error':latest['error']},
                            'attempts_24h':{state:totals.get((source,state),0) for state in ('succeeded','failed','uncertain')},
                            'next_action':stop['error']['next_action'] if stop else 'Inspect the latest attempt before requesting another bounded collection.'})
        jobs = conn.execute('SELECT state,count(*) n FROM scheduled_job GROUP BY state').fetchall()
        size = conn.execute("""SELECT COALESCE(sum(pg_total_relation_size(c.oid)),0)::bigint bytes
            FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname=current_schema() AND c.relkind='r'""").fetchone()['bytes']
        schema = conn.execute('SELECT max(version) version FROM schema_migration').fetchone()['version']
    return {'generated_at':datetime.now(timezone.utc).isoformat(),'schema_version':schema,
            'sources':sources,'jobs':{r['state']:r['n'] for r in jobs},'table_and_index_bytes':size,
            'quota_limits':settings.quota.model_dump(),
            'unregistered_source_attempts_24h':sum(r['attempts'] for r in counts if r['source'] not in REGISTRY)}


def metrics(value):
    lines = ['# HELP game_census_source_attempts_24h Attempts in the rolling 24-hour ledger window.',
             '# TYPE game_census_source_attempts_24h gauge']
    for row in value['sources']:
        for state in ('succeeded','failed','uncertain'):
            lines.append(f'game_census_source_attempts_24h{{source="{row["source"]}",outcome="{state}"}} {row["attempts_24h"][state]}')
    lines.extend(['# HELP game_census_adapter_stopped Adapter stopped for the current admitted plan.', '# TYPE game_census_adapter_stopped gauge'])
    for row in value['sources']:
        lines.append(f'game_census_adapter_stopped{{source="{row["source"]}"}} {int(row["stopped"])}')
    lines.extend(['# HELP game_census_table_and_index_bytes Application schema physical table and index size.',
                  '# TYPE game_census_table_and_index_bytes gauge',f'game_census_table_and_index_bytes {value["table_and_index_bytes"]}'])
    return '\n'.join(lines)+'\n'
