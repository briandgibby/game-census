"""HTML data semantics and chart geometry; browser journey is a separate live check."""

from datetime import datetime, timedelta, timezone

from game_census.web import _chart
from test_api import fixture_app


def test_chart_splits_large_gaps_without_inventing_points():
    at = datetime(2026, 9, 4, tzinfo=timezone.utc)
    chart = _chart({"from": at, "to": at + timedelta(hours=24), "points": [
        {"observed_at": at + timedelta(hours=1), "player_count": 10},
        {"observed_at": at + timedelta(hours=1, minutes=5), "player_count": 20},
        {"observed_at": at + timedelta(hours=4), "player_count": 0},
    ]}, 300, 2)
    assert len(chart["paths"]) == 2
    assert len(chart["points"]) == 3
    assert chart["points"][-1]["count"] == 0


def test_one_sample_is_one_dot(fixture_app):
    client, db, _ = fixture_app
    db.history_value["points"] = db.history_value["points"][:1]
    db.history_value["coverage"]["sample_count"] = 1
    response = client.get("/apps/570")
    assert response.status_code == 200
    assert response.text.count('class="chart-point"') == 1
    assert "One recorded observation" in response.text
    assert "View observation data" in response.text


def test_no_observations_have_explicit_empty_state(fixture_app):
    client, db, _ = fixture_app
    db.apps[0].update(player_count=None, observed_at=None, availability="no_observations", sample_count=0,
                      highest_recorded=None, observed_24h_peak=None, last_attempt=None)
    db.history_value["points"] = []
    db.history_value["coverage"].update(sample_count=0, coverage_ratio=0, covered_seconds=0)
    db.history_value["metrics"].update(observed_peak=None, average_observed_ccu=None)
    response = client.get("/apps/570")
    assert response.status_code == 200
    assert "No observations in this window" in response.text
    assert "Awaiting observation" in response.text
    assert "Not yet observed" in response.text
    assert 'class="big-count">—' in response.text


def test_chart_and_navigation_have_keyboard_and_table_alternatives(fixture_app):
    client, _, _ = fixture_app
    html = client.get("/apps/570").text
    assert 'class="skip-link"' in html
    assert 'aria-label="Main navigation"' in html
    assert 'aria-labelledby="chart-title-chart-570 chart-description-chart-570" tabindex="0"' in html
    assert 'id="chart-title-chart-570"' in html and 'id="chart-description-chart-570"' in html
    assert '<details class="data-table">' in html
    assert 'aria-live="polite"' in html
    assert 'scope="col"' in html


def test_chart_uses_recorded_gap_when_cadence_changed():
    at = datetime(2026, 9, 4, tzinfo=timezone.utc)
    chart = _chart({"from": at, "to": at + timedelta(hours=24), "points": [
        {"observed_at": at + timedelta(hours=1), "player_count": 10},
        {"observed_at": at + timedelta(hours=1, minutes=15), "player_count": 20},
    ], "gaps": [{"from": at + timedelta(hours=1, minutes=10), "to": at + timedelta(hours=1, minutes=15), "seconds": 300}]}, 3600, 2)
    assert len(chart["paths"]) == 2


def test_fresh_snapshot_labels_do_not_claim_continuing_freshness(fixture_app):
    client, _, _ = fixture_app
    home = client.get("/").text
    detail = client.get("/apps/570").text
    status = client.get("/status").text
    assert "Fresh at page read" in home
    assert "1 fresh at page read" in home
    assert "Fresh at page read" in detail
    assert "Fresh at page read" in status
    assert "Fresh observation" not in home + detail + status


def test_home_prioritizes_charts_and_search_is_separate(fixture_app):
    client, db, _ = fixture_app
    html = client.get("/").text
    assert "catalog" not in db.calls
    assert "Search the catalog" not in html
    assert 'class="catalog-grid"' not in html
    assert html.index("Most played") < html.index("Trending by player growth") < html.index("Tracked games")
    for path in ("/", "/search", "/apps/570", "/status", "/methodology"):
        html = client.get(path).text
        assert 'action="/search" method="get" role="search"' in html
        assert 'for="catalog-search">Search Steam game name or app ID</label>' in html


def test_search_empty_matches_and_no_results(fixture_app):
    client, db, _ = fixture_app
    assert "Enter a game name or Steam app ID" in client.get("/search").text
    assert "catalog" not in db.calls
    html = client.get("/search?q=570").text
    assert 'href="/apps/570"' in html
    assert "1 matching game" in html
    assert "No matching games" in client.get("/search?q=not-present").text
    assert 'action="/discovery/search?q=570&amp;return_page=1" method="post"' in html


def test_search_links_preserve_query_and_page(fixture_app):
    client, db, _ = fixture_app
    db.catalog = lambda *args: {"items": [], "total": 80, "page": 2, "page_size": 24}
    html = client.get("/search?q=Half+Life&page=2").text
    assert '/search?q=Half+Life&amp;page=1' in html
    assert '/search?q=Half+Life&amp;page=3' in html
    assert '/discovery/search?q=Half+Life&amp;return_page=2' in html
    legacy = client.get("/?q=Half+Life&page=2", follow_redirects=False)
    assert legacy.headers["location"] == "/search?q=Half+Life&page=2"
