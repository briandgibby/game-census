"""Per-game API contracts, bounded refresh, retained replay and safe rich pages."""
import json
import httpx
import pytest
from game_census.sources.details import ADAPTERS, STORE, REVIEWS, NEWS, CURRENT, ChartImages, safe_url, store_value
from game_census.sources import SourceError
from game_census.collector import collect_discovery
from game_census.web import create_app
from fastapi.testclient import TestClient
from test_api import fixture_app
from test_bootstrap import scratch_database


def metadata_body(app_id=730):
    return {str(app_id): {"success": True, "data": {"steam_appid": app_id, "name": "Fixture game", "type": "game",
        "developers": ["Fixture studio"], "publishers": ["Fixture publisher"],
        "short_description": '<p>A game</p><script>alert(1)</script>',
        "website": "javascript:alert(1)", "header_image": "https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/730/header.jpg",
        "release_date": {"coming_soon": False, "date": "Coming soon"}, "is_free": False,
        "platforms": {"windows": True, "mac": False}, "categories": [{"id": 23, "description": "Steam Cloud"}],
        "genres": [{"id": "1", "description": "Action"}], "pc_requirements": {"minimum": "<b>OS:</b> Windows"},
        "price_overview": {"currency": "USD", "initial": 2000, "final": 1000, "discount_percent": 50, "final_formatted": "$10.00"},
        "screenshots": [{"path_thumbnail": "https://evil.example/image.jpg", "path_full": "https://evil.example/image.jpg"}],
        "packages": [12], "dlc": [123], "achievements": {"total": 5, "highlighted": [{"name": "First steps", "path": "https://shared.akamai.steamstatic.com/image.jpg"}]}}}}


def response(request):
    if request.url.path.endswith('appdetails'):
        return httpx.Response(200, json=metadata_body(int(request.url.params['appids'])))
    if '/appreviews/' in request.url.path:
        assert request.url.params['num_per_page'] == '0'
        assert request.url.params['language'] == 'all'
        return httpx.Response(200, json={"success": 1, "query_summary": {"total_positive": 80, "total_negative": 20, "total_reviews": 100, "review_score_desc": "Positive"}, "reviews": []})
    if 'ISteamNews' in request.url.path:
        app_id = int(request.url.params['appid'])
        return httpx.Response(200, json={"appnews": {"appid": app_id, "newsitems": [{"gid": "12345", "date": 1788497305, "title": "New update", "contents": "A new patch", "feedname": "steam_community_announcements"}]}})
    return httpx.Response(200, json={"response": {"result": 1, "player_count": 42}})


def test_store_optional_fields_and_safe_content():
    data = store_value(metadata_body(), 730)
    assert data['short_description'] == 'A game'
    assert data['website'] is None and data['screenshots'] == []
    assert data['release_date'] == 'Coming soon'
    assert data['platforms'] == ['windows']
    assert data['price']['final'] == 1000
    assert data['categories'] == ['Steam Cloud']
    assert data['requirements']['pc']['minimum'] == 'OS: Windows'
    sparse = store_value({'1': {'success': True, 'data': {'steam_appid': 1, 'name': 'Unavailable fields'}}}, 1)
    assert sparse['price'] is None and sparse['achievements'] is None and sparse['platforms'] == []
    with pytest.raises(SourceError):
        store_value(metadata_body(), 570)


def test_all_detail_adapters_retain_exact_response_and_reparse():
    with httpx.Client(transport=httpx.MockTransport(response)) as client:
        captures = [adapter.fetch(client, 730, 2000000) for adapter in ADAPTERS.values()]
    for capture in captures:
        assert capture.app_id is None  # Does not enroll the app.
        assert ADAPTERS[capture.source].parse(capture.payload) == capture.value
        assert json.loads(capture.payload)['body']
        assert capture.parameters['requested_app_id'] == 730
    by_source = {capture.source: capture.value for capture in captures}
    assert by_source[REVIEWS]['positive_percent'] == 80
    assert by_source[NEWS]['articles'][0]['url'] == 'https://store.steampowered.com/news/app/730/view/12345'
    assert by_source[CURRENT]['player_count'] == 42


def test_invalid_review_totals_do_not_become_ratings():
    envelope = json.dumps({'app_id': 730, 'body': json.dumps({'success': 1, 'query_summary': {'total_positive': 9, 'total_negative': 3, 'total_reviews': 10}})}).encode()
    with pytest.raises(SourceError):
        ADAPTERS[REVIEWS].parse(envelope)


def test_chart_artwork_is_read_from_retained_html_and_allowlisted():
    parser = ChartImages()
    parser.feed('<tr><td><a href="https://store.steampowered.com/app/730/Test"><img src="https://shared.akamai.steamstatic.com/hashed/image.jpg"></a></td></tr><tr><a href="https://store.steampowered.com/app/570/Test"><img src="https://evil.example/test.jpg"></a></tr>')
    assert parser.images == {730: 'https://shared.akamai.steamstatic.com/hashed/image.jpg'}
    assert safe_url('https://shared.akamai.steamstatic.com.evil.example/image', media=True) is None
    assert safe_url('https://user:pass@shared.akamai.steamstatic.com/image', media=True) is None


def test_known_untracked_game_uses_same_profile_and_explicit_refresh(fixture_app, monkeypatch):
    client, db, settings = fixture_app
    db.discovered_app = lambda app_id: {'app_id': 730, 'name': 'Known game', 'charts': []} if app_id == 730 else None
    monkeypatch.setattr('game_census.collector.collect_discovery', lambda *a, **kw: pytest.fail('GET must never collect'))
    page = client.get('/apps/730')
    assert page.status_code == 200 and 'class="game-profile"' in page.text
    assert 'Refresh details' in page.text and 'not enrolled for tracking' in page.text
    assert 'Depots' not in page.text and 'Twitch' not in page.text
    assert client.get('/api/v1/apps/730/details').status_code == 200
    assert client.get('/apps/999').status_code == 404
    calls = []
    monkeypatch.setattr('game_census.collector.collect_discovery', lambda *a, **kw: calls.append((a[2],kw)) or {'status': 'partial'})
    assert client.post('/apps/730/refresh').status_code == 403
    post = client.post('/apps/730/refresh', headers={'Origin':'http://testserver'}, follow_redirects=False)
    assert post.status_code == 303 and post.headers['location'] == '/apps/730?refreshed=partial'
    assert calls == [('details', {'app_id': 730})]


@pytest.mark.integration
def test_details_durable_ledger_replay_and_untracked_render(scratch_database):
    db, settings = scratch_database
    report = collect_discovery(settings, db, 'details', app_id=730, transport=httpx.MockTransport(response))
    assert report['status'] == 'succeeded' and report['request_count'] == 4
    assert db.app_detail(730, settings) is None
    details = db.game_details(730)
    assert len(details['snapshots']) == 4 and len(details['prices']) == 1
    assert details['highest_recorded'] == 42 and details['observed_24h_peak'] == 42
    assert db.discovered_app(730)['name'] == 'Fixture game'
    assert db.rebuild() == db.rebuild()
    assert db.rebuild()['projections_verified'] == 4
    client = TestClient(create_app(settings, db))
    page = client.get('/apps/730')
    assert page.status_code == 200
    for text in ('Fixture studio', '$10.00 USD', '80.0%', '80 positive', '20 negative', 'New update', 'Steam Cloud', 'Prices & packages'):
        assert text in page.text
    assert '<script>alert' not in page.text and 'javascript:alert' not in page.text
    assert 'Coming soon' in page.text
    assert 'Steam all-time record' not in page.text
    # A quota failure remains visible and cannot erase the last good captures.
    settings.quota.store_rolling_24h = 2
    settings.quota.webapi_rolling_24h = 2
    failed = collect_discovery(settings, db, 'details', app_id=730, transport=httpx.MockTransport(lambda r: pytest.fail('Quota must block dispatch')))
    assert failed['request_count'] == 0 and failed['status'] == 'failed'
    assert db.game_details(730)['last_refresh']['status'] == 'failed'
    assert len(db.game_details(730)['snapshots']) == 4
    assert 'Previously captured data remains available' in client.get('/apps/730').text

def test_profile_selects_newest_player_count_across_sources(fixture_app):
    from datetime import timedelta
    client, db, _ = fixture_app
    observed_at = db.apps[0]['observed_at']
    snapshot = {'player_count': 777, 'observed_at': (observed_at - timedelta(minutes=1)).isoformat()}
    db.game_details = lambda app_id: {'snapshots': {CURRENT: snapshot}, 'prices': [], 'updates': [], 'last_refresh': None, 'highest_recorded': 777, 'observed_24h_peak': 777}
    assert 'class="big-count">0</div>' in client.get('/apps/570').text
    snapshot['observed_at'] = (observed_at + timedelta(minutes=1)).isoformat()
    assert 'class="big-count">777</div>' in client.get('/apps/570').text


def test_news_presentation_removes_steam_markup_without_changing_snapshot(fixture_app):
    from game_census.web import _news_excerpt
    raw = '[h2]Demo available[/h2] [img]{STEAM_CLAN_IMAGE}/45343632/long-image.jpg[/img] [url=https://example.com]Read more[/url]'
    assert _news_excerpt(raw) == 'Demo available  Read more'
    assert _news_excerpt('Demo {STEAM_CLAN_IMAGE}/45343632/long-image.jpg') == 'Demo'
    client, db, _ = fixture_app
    snapshot = {'articles': [{'title': 'Demo', 'date': 1788497305, 'url': 'https://store.steampowered.com/news/app/570/view/1', 'body': raw}]}
    db.game_details = lambda app_id: {'snapshots': {NEWS: snapshot}, 'prices': [], 'updates': [], 'last_refresh': None, 'highest_recorded': None, 'observed_24h_peak': None}
    html = client.get('/apps/570').text
    assert 'STEAM_CLAN_IMAGE' not in html and '[h2]' not in html and 'Demo available' in html
    assert snapshot['articles'][0]['body'] == raw

def test_profiles_remain_stored_reads_and_reject_automatic_collection(fixture_app, monkeypatch):
    from datetime import datetime, timedelta, timezone
    client, db, _ = fixture_app
    monkeypatch.setattr('game_census.collector.collect_discovery', lambda *a, **kw: pytest.fail('Page loads must not collect'))
    assert 'No Steam details have been recorded' in client.get('/apps/570').text
    assert client.get('/apps/570?refreshed=failed').status_code == 200
    details = {'snapshots': {}, 'prices': [], 'updates': [], 'last_refresh': {'status': 'failed', 'sources': [], 'finished_at': datetime.now(timezone.utc).isoformat()}, 'highest_recorded': None, 'observed_24h_peak': None}
    db.game_details = lambda app_id: details
    assert client.get('/apps/570').status_code == 200
    cached = client.post('/apps/570/refresh?automatic=true', headers={'Origin':'http://testserver'}, follow_redirects=False)
    assert cached.status_code == 422
    details['last_refresh']['finished_at'] = (datetime.now(timezone.utc) - timedelta(minutes=16)).isoformat()
    assert client.get('/apps/570').status_code == 200
    assert client.post('/apps/570/refresh?automatic=true', headers={'Origin':'http://testserver'}).status_code == 422
