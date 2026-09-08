"""Enrichment contracts: privacy, policy boundaries, replay and series identity."""
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import httpx
import pytest
from pydantic import SecretStr, ValidationError

from game_census.config import Enrichment, Settings, Storage
from game_census.sources import enrichment as source, SourceError
from test_bootstrap import scratch_database
from test_details import metadata_body


def settings():
    return Settings(storage=Storage(database_url=SecretStr('postgresql://test:test@db:5432/test')))


def body(kind, app_id=570):
    if kind == 'store':
        return metadata_body(app_id)
    if kind == 'reviews':
        return {'success': 1, 'query_summary': {'total_positive': 80, 'total_negative': 20, 'total_reviews': 100,
            'review_score': 8, 'review_score_desc': 'Very Positive', 'num_reviews': 1},
            'reviews': [{'author': {'steamid': 'private-author-id'}, 'review': 'private-review-body'}]}
    if kind == 'news':
        return {'appnews': {'appid': app_id, 'newsitems': [{'gid': '123', 'date': 1788497305,
            'title': '<b>Update</b>', 'feedname': 'steam_community_announcements', 'contents': 'private-article-body'}]}}
    if kind == 'achievements':
        return {'achievementpercentages': {'achievements': [{'name': 'FIRST', 'percent': '0.125'}]}}
    return {'game': {'availableGameStats': {'achievements': [{'name': 'FIRST', 'displayName': 'First steps',
            'description': '<script>bad</script>A step', 'hidden': 0, 'icon': 'javascript:bad'}]}}}


def fetch(kind, response=None, config=None):
    cfg = config or settings()
    cfg.sources.catalog_api_key = SecretStr('fixture-key')
    adapter = source.configured(cfg, [kind])[0]
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body(kind) if response is None else response))) as client:
        return adapter.fetch(client, 570, 2000000)


@pytest.mark.integration
def test_profile_refresh_retains_no_reviewer_records(scratch_database):
    from game_census.collector import collect_discovery
    db, cfg = scratch_database
    def response(request):
        kind = 'store' if 'appdetails' in request.url.path else 'reviews' if 'appreviews' in request.url.path else 'news' if 'ISteamNews' in request.url.path else None
        return httpx.Response(200, json=body(kind) if kind else {'response': {'result': 1, 'player_count': 1}})
    assert collect_discovery(cfg, db, 'details', app_id=570, transport=httpx.MockTransport(response))['status'] == 'succeeded'
    with db.connection() as conn:
        payloads = b' '.join(bytes(r['payload']) for r in conn.execute('SELECT payload FROM capture').fetchall())
    assert b'private-author-id' not in payloads
    assert b'private-review-body' not in payloads
    assert b'private-article-body' not in payloads


@pytest.mark.parametrize('kind', list(source.KINDS))
def test_versioned_capture_replays_without_current_configuration(kind):
    capture = fetch(kind)
    assert source.ADAPTERS[capture.source].parse(capture.payload, 570) == capture.value
    assert capture.value['app_id'] == 570
    assert 'fixture-key' not in capture.payload.decode() + json.dumps(capture.parameters)
    if kind in ('reviews', 'news'):
        assert b'private-' not in capture.payload


@pytest.mark.parametrize('mode,expected', [('paid', 'priced'), ('free', 'free'), ('missing', 'unknown'), ('unavailable', 'unavailable')])
def test_price_states_do_not_infer_free_from_missing_price(mode, expected):
    value = body('store')
    if mode in ('free', 'missing'):
        value['570']['data'].pop('price_overview')
        value['570']['data']['is_free'] = mode == 'free'
    elif mode == 'unavailable':
        value = {'570': {'success': False}}
    assert fetch('store', value).value['state'] == expected


@pytest.mark.parametrize('field,value', [('discount_percent', 101), ('final', True), ('currency', 'usd'), ('initial', -1)])
def test_malformed_price_is_a_failure(field, value):
    data = body('store'); data['570']['data']['price_overview'][field] = value
    with pytest.raises(SourceError):
        fetch('store', data)


def test_review_zero_negative_scope_and_retained_query():
    data = body('reviews')
    data['query_summary'].update(total_positive=0, total_negative=0, total_reviews=0)
    assert fetch('reviews', data).value['positive_percent'] is None
    cfg = settings(); cfg.enrichment.reviews.purchase_type = 'steam'
    first, second = fetch('reviews'), fetch('reviews', config=cfg)
    assert first.value['query_hash'] != second.value['query_hash']
    assert second.parameters['purchase_type'] == 'steam' and second.parameters['cursor'] == '*'
    assert second.parameters['num_per_page'] == 1
    data['query_summary']['total_reviews'] = 1
    with pytest.raises(SourceError): fetch('reviews', data)


@pytest.mark.parametrize('value', ['NaN', 'Infinity', '-0.1', '100.01', True])
def test_achievement_percentage_is_finite_decimal_in_bounds(value):
    data = body('achievements'); data['achievementpercentages']['achievements'][0]['percent'] = value
    with pytest.raises(SourceError): fetch('achievements', data)


def test_schema_key_is_required_before_network_and_never_in_parameters():
    adapter = source.configured(settings(), ['achievement_schema'])[0]
    with httpx.Client(transport=httpx.MockTransport(lambda r: pytest.fail('No dispatch without key'))) as client:
        with pytest.raises(SourceError, match='sources.catalog_api_key'):
            adapter.fetch(client, 570, 2000000)
    assert 'key' not in adapter.parameters(570)


def test_unsupported_and_malformed_achievements_are_distinct():
    assert fetch('achievements', {'achievementpercentages': {'achievements': []}}).value['state'] == 'unsupported'
    assert fetch('achievement_schema', {'game': {'availableGameStats': {}}}).value['state'] == 'unsupported'
    for kind in ('achievement_schema', 'achievements'):
        with pytest.raises(SourceError): fetch(kind, {})


def test_news_rejects_malformed_items_instead_of_returning_partial_success():
    data = body('news'); data['appnews']['newsitems'].append({'gid': 'broken'})
    with pytest.raises(SourceError): fetch('news', data)


@pytest.mark.parametrize('config', [{'store': {'country': '../us'}}, {'reviews': {'day_range': 366}}, {'news': {'count': 21}}, {'achievement_schema': {'language': '<html>'}}])
def test_enrichment_policy_rejects_untrusted_values(config):
    with pytest.raises(ValidationError): Enrichment.model_validate(config)


def retain(db, capture, at=None):
    if at:
        capture = replace(capture, request_started_at=at, received_at=at)
    run = db.start_run([570], [capture.source])
    adapter = source.ADAPTERS[capture.source]
    attempt = db.reserve_attempt(run, 570, capture.source, adapter.HOST_GROUP, 10000, 1)
    db.record_capture(run, attempt, capture)
    return run


@pytest.mark.integration
def test_price_series_reviews_delta_pagination_and_replay(scratch_database):
    from game_census import enrichment
    db, cfg = scratch_database
    at = datetime.now(timezone.utc)
    retain(db, fetch('store'), at)
    euro = body('store'); euro['570']['data']['price_overview'].update(currency='EUR', final=500)
    retain(db, fetch('store', euro), at+timedelta(seconds=1))
    cheap = body('store'); cheap['570']['data']['price_overview']['final'] = 750
    retain(db, fetch('store', cheap), at+timedelta(seconds=2))
    series = enrichment.history(cfg, db, 570, 'store', limit=1)
    assert {row['currency']: row['lowest_observed_minor'] for row in series['series']} == {'USD': 750, 'EUR': 500}
    assert series['has_more'] and len(series['observations']) == 1
    page = enrichment.history(cfg, db, 570, 'store', limit=1, cursor=series['next_cursor'])
    assert page['observations'][0]['value']['currency'] == 'EUR'
    retain(db, fetch('reviews'), at)
    changed_query = cfg.model_copy(deep=True); changed_query.enrichment.reviews.language = 'english'
    retain(db, fetch('reviews', config=changed_query), at)
    smaller = body('reviews'); smaller['query_summary'].update(total_positive=75, total_reviews=95)
    retain(db, fetch('reviews', smaller), at+timedelta(seconds=1))
    reviews = enrichment.history(cfg, db, 570, 'reviews', limit=1)
    assert reviews['observations'][0]['net_delta']['total_reviews'] == -5
    second = enrichment.history(cfg, db, 570, 'reviews', limit=1, cursor=reviews['next_cursor'])
    third = enrichment.history(cfg, db, 570, 'reviews', limit=1, cursor=second['next_cursor'])
    assert len({r['observations'][0]['capture_id'] for r in (reviews, second, third)}) == 3
    assert not third['has_more']
    assert db.rebuild()['projections_verified'] == 6


@pytest.mark.integration
def test_enrichment_api_is_stored_typed_bounded_and_profile_renders(scratch_database, monkeypatch):
    from fastapi.testclient import TestClient
    from game_census.web import create_app
    db, cfg = scratch_database
    for kind in source.KINDS:
        retain(db, fetch(kind))
    monkeypatch.setattr(httpx.Client, 'stream', lambda *a, **kw: pytest.fail('GET must not contact Steam'))
    client = TestClient(create_app(cfg, db))
    for path in ('prices','reviews','achievements','achievement-schema','news'):
        response = client.get('/api/v1/apps/570/'+path)
        assert response.status_code == 200, response.text
        assert len(response.json()['observations']) == 1
        assert 'private-' not in response.text and 'fixture-key' not in response.text
    assert client.get('/api/v1/apps/570/reviews?limit=1001').status_code == 422
    assert client.get('/api/v1/apps/570/reviews?cursor=bad').status_code == 422
    assert client.get('/api/v1/apps/999/prices').status_code == 404
    page = client.get('/apps/570')
    assert page.status_code == 200
    for text in ('Price series since collection began','0.125%','First steps','Update','Enrichment history'):
        assert text in page.text
    assert 'private-' not in page.text and '<script>bad' not in page.text


def configured_all(cfg):
    cfg.http.timeout_seconds = 1
    cfg.sources.catalog_api_key = SecretStr('fixture-key')
    for kind in source.KINDS:
        getattr(cfg.enrichment, kind).enabled = True
    return cfg


def all_responses(request):
    if 'appdetails' in request.url.path:
        return httpx.Response(200, json=body('store'))
    for fragment, kind in [('appreviews','reviews'), ('GetNewsForApp','news'), ('GetSchemaForGame','achievement_schema'), ('GetGlobalAchievement','achievements')]:
        if fragment in request.url.path:
            if kind == 'achievement_schema': assert request.headers['x-webapi-key'] == 'fixture-key'
            return httpx.Response(200, json=body(kind))
    return httpx.Response(200, json={'response': {'result': 1, 'player_count': 123}})


def test_schedule_sums_shared_host_work_and_hashes_query_and_cadence():
    from game_census import scheduler
    cfg = configured_all(settings())
    value = scheduler.plan(cfg)
    assert value['admitted'], value['errors']
    assert value['budgets']['webapi']['scheduled_attempt_ceiling'] == 289+25*3
    assert value['budgets']['store']['scheduled_attempt_ceiling'] == 289+25*2
    original = value['plan_hash']
    cfg.enrichment.store.country = 'gb'
    assert scheduler.plan(cfg)['plan_hash'] != original
    cfg.enrichment.reviews.interval_seconds = 301
    assert not scheduler.plan(cfg)['admitted']
    cfg.enrichment.reviews.interval_seconds = 3600
    cfg.quota.store_rolling_24h = 300
    assert not scheduler.plan(cfg)['admitted']


@pytest.mark.integration
def test_configured_manual_attestation_and_scheduled_dispatch(scratch_database):
    from game_census.collector import collect_once
    from game_census import scheduler
    db, cfg = scratch_database
    configured_all(cfg)
    evidence = collect_once(cfg, db, transport=httpx.MockTransport(all_responses))
    assert evidence['status'] == 'succeeded', evidence
    assert evidence['request_count'] == 6  # Player source plus five enrichments.
    scheduler.attest_manual(cfg, db, evidence)
    scheduler.acknowledge(cfg, db, evidence['run_id'])  # Synthetic database only.
    scheduler.enable(cfg, db)
    result = scheduler.run(cfg, db, transport=httpx.MockTransport(all_responses))
    assert result['status'] == 'succeeded', result
    assert db.rebuild()['projections_verified'] == 12
    cfg.enrichment.reviews.purchase_type = 'steam'
    assert not scheduler.status(cfg, db)['enabled']


@pytest.mark.integration
def test_slow_sources_materialize_only_at_their_own_cadence(scratch_database):
    from game_census.collector import collect_once
    from game_census import scheduler
    db, cfg = scratch_database
    configured_all(cfg)
    evidence = collect_once(cfg, db, transport=httpx.MockTransport(all_responses))
    scheduler.attest_manual(cfg, db, evidence)
    scheduler.acknowledge(cfg, db, evidence['run_id'])
    scheduler.enable(cfg, db)
    with db.connection() as conn:
        state = conn.execute("UPDATE schedule_state SET enabled_at=clock_timestamp()-interval '301 seconds', cursor_at=clock_timestamp()-interval '301 seconds' WHERE singleton RETURNING *").fetchone()
        conn.execute('UPDATE schedule_state SET cursor_at=enabled_at WHERE singleton')
    scheduler._materialize(cfg, db, state['plan_hash'], state['epoch'])
    with db.connection() as conn:
        counts = {r['source']: r['n'] for r in conn.execute('SELECT source,count(*) n FROM scheduled_job GROUP BY source').fetchall()}
    from game_census.sources import players
    assert counts[players.SOURCE] == 2
    assert all(counts[s] == 1 for s in source.ADAPTERS)
