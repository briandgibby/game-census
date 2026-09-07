"""Discovery source contracts, stored reads, and durable global collection."""
import json
import httpx
import pytest
from pydantic import SecretStr
from game_census.collector import collect_discovery
from game_census.config import describe_settings
from game_census.sources import SourceError
from game_census.sources.discovery import ADAPTERS, PLAYED, SALES, SEARCH, CATALOG
from test_api import fixture_app
from test_bootstrap import scratch_database


def chart_html(players=100, app_id=730):
    # Shape verified against Steam's server-rendered chart tbody (2026-09-05).
    return f'''<table><tbody><tr><td><a href="https://store.steampowered.com/sub/9/">Package</a></td><td>1</td><td>Package</td><td>$9</td><td>New</td><td>1</td></tr>
    <tr><td><a href="https://store.steampowered.com/app/{app_id}/Test">image</a></td><td>2</td><td><a href="https://store.steampowered.com/app/{app_id}/Test"><img alt=""><div>Test &amp; Game</div></a></td><td>Free To Play</td><td>{players:,}</td><td>1,000</td></tr></tbody></table>'''.encode()


def search_html():
    return b'''<div id="search_resultsRows"><a class="search_result_row ds_collapse_flag" data-ds-appid="400" data-ds-itemkey="App_400"><span class="title">Portal</span></a></div>'''


def test_chart_contract_preserves_official_rank_and_counts():
    row = ADAPTERS[PLAYED].parse(chart_html(123))["items"][0]
    assert row == {"app_id": 730, "name": "Test & Game", "rank": 2, "players": 123, "peak_today": 1000}
    sales = ADAPTERS[SALES].parse(chart_html())["items"][0]
    assert sales == {"app_id": 730, "name": "Test & Game", "rank": 2}


@pytest.mark.parametrize("source,payload", [(PLAYED,b"<html>blocked</html>"), (SALES,b"<tbody></tbody>"),
    (SEARCH,b"<html>blocked</html>"), (CATALOG,b'{"response":{}}'), (PLAYED,chart_html(-1))])
def test_changed_or_invalid_source_is_not_empty_success(source, payload):
    with pytest.raises(SourceError):
        ADAPTERS[source].parse(payload)


def test_search_valid_empty_and_named_app():
    assert ADAPTERS[SEARCH].parse(b'<div id="search_resultsRows"></div>') == {"items": []}
    assert ADAPTERS[SEARCH].parse(search_html())["items"] == [{"app_id": 400, "name": "Portal"}]


def test_catalog_key_only_in_header_and_never_capture(fixture_app):
    _, _, settings = fixture_app
    secret = "fixture-secret-key"
    settings.sources.catalog_api_key = SecretStr(secret)
    def respond(request):
        assert request.headers["x-webapi-key"] == secret
        assert secret not in str(request.url)
        assert json.loads(request.url.params["input_json"])["last_appid"] == 0
        return httpx.Response(200, json={"response": {"apps": [{"appid":400,"name":"Portal"}], "have_more_results":False}})
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        capture = ADAPTERS[CATALOG].fetch(client, {"last_appid":0}, 2000000, settings.sources.catalog_api_key)
    assert secret not in repr(capture) + json.dumps(describe_settings(settings))


def test_catalog_nonadvancing_cursor_rejected():
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200,json={"response":{
        "apps":[{"appid":400,"name":"Portal"}],"have_more_results":True,"last_appid":400}}))) as client:
        with pytest.raises(SourceError):
            ADAPTERS[CATALOG].fetch(client, {"last_appid":400}, 2000000, SecretStr("fixture"))


def test_live_catalog_blank_name_keeps_valid_app_id():
    result = ADAPTERS[CATALOG].parse(b'{"response":{"apps":[{"appid":396420,"name":""}],"have_more_results":true,"last_appid":412230}}')
    assert result["items"] == [{"app_id":396420,"name":"Steam app 396420"}]
    assert result["last_appid"] == 412230


def test_catalog_routes_and_labels_are_stored_only(fixture_app, monkeypatch):
    client, _, _ = fixture_app
    monkeypatch.setattr("game_census.collector.collect_discovery", lambda *a,**k: pytest.fail("GET must not collect"))
    for path in ("/", "/?q=570", "/api/v1/catalog?q=570&page=1", "/api/v1/dashboard"):
        assert client.get(path).status_code == 200
    text = client.get("/").text
    assert "Global Steam ranking" in text and "Trending needs two" in text
    assert "revenue" not in text.lower()
    assert client.get("/api/v1/catalog?page=0").status_code == 422
    assert client.get("/api/v1/catalog?page_size=101").status_code == 422


def test_mutations_reject_cross_origin_and_redirect_success(fixture_app, monkeypatch):
    client, _, _ = fixture_app
    calls = []
    def collect(*args, **kwargs):
        calls.append(kwargs)
        return {"status":"succeeded", "sources":[]}
    monkeypatch.setattr("game_census.collector.collect_discovery", collect)
    for headers in ({}, {"Origin":"https://evil.example"}, {"Origin":"http://testserver","Sec-Fetch-Site":"cross-site"}):
        assert client.post("/discovery/charts",headers=headers).status_code == 403
    response = client.post("/discovery/search?q=Portal",headers={"Origin":"http://testserver"},follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/search?q=Portal&page=1"
    assert calls == [{"query":"Portal","page":1}]
    response = client.post("/discovery/search?q=Half+Life&page=2&return_page=5", headers={"Origin":"http://testserver"}, follow_redirects=False)
    assert response.headers["location"] == "/search?q=Half+Life&page=5"
    assert calls[-1] == {"query": "Half Life", "page": 2}


def test_catalog_pagination_crosses_old_limit(fixture_app, monkeypatch):
    client, db, _ = fixture_app
    def catalog(query="", page=1, page_size=25):
        return {"items": [], "total": 300000, "page": page, "page_size": page_size}
    monkeypatch.setattr(db, "catalog", catalog)
    assert '/search?q=game&amp;page=10001' in client.get('/search?q=game&page=10000').text
    assert client.get('/?page=10001').status_code == 200
    response = client.get('/api/v1/catalog?page=10001&page_size=1')
    assert response.status_code == 200 and response.json()["page"] == 10001
    assert client.get('/api/v1/catalog?page=4294967295&page_size=100').status_code == 200
    for route in ('/', '/search', '/api/v1/catalog'):
        assert client.get(route + '?page=4294967296').status_code == 422


def test_catalog_cli_passes_restart(fixture_app, monkeypatch, capsys):
    from game_census import cli
    _, db, settings = fixture_app
    calls = []
    monkeypatch.setattr(cli, "load_settings", lambda path: settings)
    monkeypatch.setattr(cli, "Database", lambda dsn: db)
    monkeypatch.setattr("game_census.catalog.plan", lambda *a, **k: {"operation": "catalog_sync"})
    monkeypatch.setattr("game_census.collector.collect_discovery",
                        lambda *a, **k: calls.append(k) or {"status": "succeeded"})
    assert cli.main(['catalog', 'sync', '--once', '--restart', '--max-pages', '1']) == 0
    assert calls[0]["restart"] is True and calls[0]["max_pages"] == 1


@pytest.mark.integration
def test_discovery_does_not_track_and_rebuilds(scratch_database):
    db,settings = scratch_database
    result = collect_discovery(settings,db,"charts",transport=httpx.MockTransport(lambda r: httpx.Response(200,content=chart_html())))
    assert result["status"] == "succeeded" and result["request_count"] == 2
    assert [row["app_id"] for row in db.list_apps(settings)] == [570]
    assert db.history(730,settings) is None
    assert db.catalog("Test")["items"][0]["tracked"] is False
    assert db.discovered_app(730)["charts"][0]["players"] == 100
    from fastapi.testclient import TestClient
    from game_census.web import create_app
    client = TestClient(create_app(settings,db))
    assert "not enrolled for tracking" in client.get("/apps/730").text
    assert 'action="/apps/730/refresh"' in client.get("/apps/730").text
    summary = client.get("/api/v1/apps/730")
    assert summary.status_code == 200 and summary.json()["availability"] == "not_tracked"
    assert summary.json()["tracking_started_at"] is None and summary.json()["player_count"] is None
    assert db.catalog("Test",page=1,page_size=1)["total"] == 1
    assert db.catalog("Test",page=2,page_size=1)["items"] == []
    assert db.catalog("Test",page=10001,page_size=1)["items"] == []
    assert db.catalog("Test",page=4294967295,page_size=100)["items"] == []
    assert db.catalog("%")["total"] == 0
    assert db.status()["player_samples"] == 0
    first = db.rebuild()
    assert first == db.rebuild() and first["captures_replayed"] == 2
    assert db.dashboard()["trending"]["items"] == []
    collect_discovery(settings,db,"charts",transport=httpx.MockTransport(lambda r: httpx.Response(200,content=chart_html(150))))
    assert db.dashboard()["trending"]["items"][0]["change"] == 50
    collect_discovery(settings,db,"charts",transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    dashboard = db.dashboard()
    assert dashboard["charts"][PLAYED]["items"][0]["players"] == 150
    assert dashboard["latest_attempts"][PLAYED]["status"] == "failed"
    settings.quota.store_rolling_24h = 1
    collect_discovery(settings,db,"charts",transport=httpx.MockTransport(lambda r: pytest.fail("quota must prevent dispatch")))
    assert db.dashboard()["latest_attempts"][PLAYED]["error"]["code"] == "quota_exhausted"


@pytest.mark.integration
def test_catalog_cursor_resume_terminal_and_failed_page_atomicity(scratch_database,monkeypatch):
    db,settings = scratch_database
    settings.sources.catalog_api_key = SecretStr("fixture")
    pages = []
    def respond(request):
        cursor = json.loads(request.url.params["input_json"])["last_appid"]
        pages.append(cursor)
        return httpx.Response(200,json={"response":{"apps":[{"appid":400 if cursor==0 else 620,"name":"Portal"}],
                                "have_more_results":cursor==0,"last_appid":400 if cursor==0 else 620}})
    collect_discovery(settings,db,"catalog",max_pages=1,transport=httpx.MockTransport(respond))
    assert db.catalog_sync_state()["last_appid"] == 400
    import game_census.projections as projections
    real_project = projections.project
    def fail_after_projection(conn,capture):
        real_project(conn,capture)
        raise SourceError("injected", "Fixture projection failure", "Retry")
    monkeypatch.setattr(projections,"project",fail_after_projection)
    result = collect_discovery(settings,db,"catalog",max_pages=1,transport=httpx.MockTransport(respond))
    assert result["status"] == "failed"
    assert db.catalog_sync_state()["last_appid"] == 400
    assert db.catalog("620")["total"] == 0
    monkeypatch.setattr(projections,"project",real_project)
    collect_discovery(settings,db,"catalog",max_pages=5,transport=httpx.MockTransport(respond))
    assert db.catalog_sync_state()["complete"] is True
    assert pages == [0,400,400]
    assert db.rebuild()["captures_replayed"] == 2
    terminal = db.catalog_sync_state()
    assert collect_discovery(settings,db,"catalog",transport=httpx.MockTransport(respond))["request_count"] == 0
    assert db.catalog_sync_state() == terminal
    # A failed restart must leave the terminal cursor and timestamp intact.
    monkeypatch.setattr(projections,"project",fail_after_projection)
    result = collect_discovery(settings,db,"catalog",restart=True,max_pages=1,transport=httpx.MockTransport(respond))
    assert result["status"] == "failed" and db.catalog_sync_state() == terminal
    monkeypatch.setattr(projections,"project",real_project)
    def refresh(request):
        cursor = json.loads(request.url.params["input_json"])["last_appid"]
        pages.append(cursor)
        return httpx.Response(200,json={"response":{"apps":[{"appid":400 if cursor==0 else 730,"name":"Updated game"}],
                                "have_more_results":cursor==0,"last_appid":400 if cursor==0 else 730}})
    collect_discovery(settings,db,"catalog",restart=True,max_pages=1,transport=httpx.MockTransport(refresh))
    partial = db.catalog_sync_state()
    assert partial["last_appid"] == 400 and partial["complete"] is False
    assert partial["observed_at"] > terminal["observed_at"]
    collect_discovery(settings,db,"catalog",max_pages=5,transport=httpx.MockTransport(refresh))
    complete = db.catalog_sync_state()
    assert complete["complete"] is True and complete["observed_at"] > partial["observed_at"]
    assert pages == [0,400,400,0,0,400]
    assert db.catalog("400")["items"][0]["name"] == "Updated game"
    assert db.catalog("730")["total"] == 1
    assert db.catalog("620")["total"] == 1  # Absence from a rescan never deletes a discovery.
    assert db.rebuild()["captures_replayed"] == 4
    assert db.catalog_sync_state() == complete
