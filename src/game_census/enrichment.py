"""Bounded enrichment interfaces over retained, replayable source snapshots."""
from datetime import datetime, timezone
import base64
import json
import uuid
from .db import DatabaseError, QueryLimitError, iso
from .sources import enrichment as source, SourceError


def require_credentials(adapters):
    if any(a.kind == "achievement_schema" and (a.key is None or not a.key.get_secret_value().strip())
           for a in adapters if hasattr(a, "kind")):
        raise SourceError("missing_schema_key", "sources.catalog_api_key is required for achievement schema.",
                          "Provide the Steam Web API key through local configuration before collecting this source.")


def plan(settings, app_id, kinds):
    from .sources.base import validate_app_id
    validate_app_id(app_id)
    if not kinds or len(kinds) != len(set(kinds)) or any(kind not in source.KINDS for kind in kinds):
        raise DatabaseError("Select one or more unique enrichment source kinds: store, reviews, achievements, achievement_schema, news.")
    adapters = source.configured(settings, kinds)
    errors = []
    try:
        require_credentials(adapters)
    except SourceError as error:
        errors.append(error.as_dict())
    return {"app_id": app_id, "admitted": not errors, "errors": errors,
            "maximum_requests": len(adapters), "steam_requests_in_plan": 0,
            "writes": "one retained run; at most one charged attempt and capture per selected source; no tracking enrollment",
            "sources": [{"source": a.SOURCE, "parameters": a.parameters(app_id), "host_group": a.HOST_GROUP} for a in adapters]}


def history(settings, db, app_id, kind, *, limit=None, cursor=None):
    from .sources.base import validate_app_id
    validate_app_id(app_id)
    if kind not in source.KINDS:
        raise DatabaseError("Unknown enrichment source kind.")
    maximum = settings.enrichment.history_limit
    limit = maximum if limit is None else limit
    if type(limit) is not int or not 1 <= limit <= maximum:
        raise QueryLimitError("History limit exceeds enrichment.history_limit. Request a smaller page.")
    before, before_id = None, None
    if cursor is not None:
        try:
            if len(cursor)>256:
                raise ValueError()
            at, capture_id, cursor_app, cursor_kind = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
            before, before_id = datetime.fromisoformat(at), uuid.UUID(capture_id)
            if before.tzinfo is None or cursor_app != app_id or cursor_kind != kind:
                raise ValueError()
        except (ValueError, TypeError, UnicodeError):
            raise QueryLimitError("Invalid enrichment cursor. Use the next_cursor returned by this app and source history.") from None
    adapter = source.configured(settings, [kind])[0]
    query_hash = source.identity(adapter.parameters(app_id))
    with db.connection() as conn:
        rows = conn.execute("""SELECT capture_id,observed_at,value FROM discovery_snapshot
            WHERE source=%s AND value->>'app_id'=%s AND (%s::timestamptz IS NULL OR (observed_at,capture_id)<(%s,%s))
            ORDER BY observed_at DESC,capture_id DESC LIMIT %s""",
            (adapter.SOURCE, str(app_id), before, before, before_id, limit+1)).fetchall()
        # Full-history metrics remain qualified, even when the visible page is bounded.
        series = []
        if kind == 'store':
            series = conn.execute("""SELECT value->>'country' country,value->>'currency' currency,value->>'product' product,
                min(observed_at) started_at,max(observed_at) latest_at,count(*) observations,
                min((value->'price'->>'final')::bigint) lowest_observed_minor
                FROM discovery_snapshot WHERE source=%s AND value->>'app_id'=%s
                GROUP BY value->>'country',value->>'currency',value->>'product'
                ORDER BY min(observed_at),value->>'country',value->>'currency',value->>'product' LIMIT 1001""", (adapter.SOURCE,str(app_id))).fetchall()
            if len(series)>1000:
                raise DatabaseError("Price history exceeds 1000 series. Inspect the configured country policies before querying.")
        attempt = conn.execute("""SELECT a.dispatched_at,r.status,r.error FROM request_attempt a
            LEFT JOIN request_result r USING(attempt_id) WHERE a.source=%s AND
            (a.app_id=%s OR a.run_id IN (SELECT run_id FROM run_completion WHERE report->>'requested_app_id'=%s))
            ORDER BY a.dispatched_at DESC LIMIT 1""", (adapter.SOURCE,app_id,str(app_id))).fetchone()
        completion = conn.execute("""SELECT finished_at,report FROM run_completion
            WHERE report->>'requested_app_id'=%s ORDER BY finished_at DESC LIMIT 1""", (str(app_id),)).fetchone()
        if completion and (attempt is None or completion['finished_at'] >= attempt['dispatched_at']):
            outcomes = [row for row in completion['report'].get('sources',[]) if row['source'] == adapter.SOURCE]
            if outcomes and outcomes[-1]['status'] == 'failed':
                attempt = {'dispatched_at': completion['finished_at'], 'status': 'failed', 'error': outcomes[-1].get('error')}
        observations = []
        for row in rows[:limit]:
            value = row['value']
            item = {'capture_id': str(row['capture_id']), 'observed_at': iso(row['observed_at']), 'value': value,
                    'matches_current_query': value['query_hash'] == query_hash, 'net_delta': None}
            if kind == 'reviews':
                previous = conn.execute("""SELECT observed_at,value FROM discovery_snapshot WHERE source=%s
                    AND value->>'app_id'=%s AND value->>'query_hash'=%s AND (observed_at,capture_id)<(%s,%s)
                    ORDER BY observed_at DESC,capture_id DESC LIMIT 1""",
                    (adapter.SOURCE,str(app_id),value['query_hash'],row['observed_at'],row['capture_id'])).fetchone()
                if previous:
                    item['net_delta'] = {key: value[key]-previous['value'][key] for key in ('total_reviews','total_positive','total_negative')}
                    item['net_delta']['since'] = iso(previous['observed_at'])
            observations.append(item)
    for row in series:
        row['started_at'], row['latest_at'] = iso(row['started_at']), iso(row['latest_at'])
    latest = rows[0] if rows else None
    stale = latest is not None and (datetime.now(timezone.utc)-latest['observed_at']).total_seconds() >= adapter.interval_seconds*2
    return {'app_id': app_id, 'kind': kind, 'source': adapter.SOURCE, 'source_version': adapter.VERSION,
            'current_query': adapter.parameters(app_id), 'current_query_hash': query_hash,
            'availability': 'no_observations' if latest is None else 'stale' if stale else latest['value']['state'],
            'last_attempt': None if attempt is None else {'at': iso(attempt['dispatched_at']), 'status': attempt['status'] or 'uncertain', 'error': attempt['error']},
            'observations': observations, 'series': series, 'has_more': len(rows)>limit,
            'next_cursor': base64.urlsafe_b64encode(json.dumps([iso(rows[limit-1]['observed_at']),str(rows[limit-1]['capture_id']),app_id,kind]).encode()).decode() if len(rows)>limit else None,
            'methodology': 'Retained observations only. Review deltas are net changes within the same query, not newly written reviews. Prices use integer minor units within country/currency/product series.'}
