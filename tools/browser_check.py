"""Exercise the local website, Steam artwork and bounded per-profile loading."""
import argparse
import json
from pathlib import Path
from urllib.parse import urlsplit
from playwright.sync_api import sync_playwright
from game_census.sources.details import MEDIA_HOSTS


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--url", default="http://127.0.0.1:8000")
    p.add_argument("--output-dir", type=Path, default=Path("work/browser-check"))
    args = p.parse_args()
    url = args.url.rstrip("/")
    if urlsplit(url).hostname not in ("127.0.0.1", "localhost"):
        p.error("This bounded check accepts only a local instance URL.")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as engine:
        browser = engine.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1080}, reduced_motion="reduce")
        errors, external = [], []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("request", lambda req: external.append(req.url) if urlsplit(req.url).hostname not in ("127.0.0.1", "localhost") and
                not (req.resource_type == "image" and urlsplit(req.url).scheme == "https" and urlsplit(req.url).hostname in MEDIA_HOSTS) else None)
        before = page.request.get(url + "/api/v1/status").json()
        response = page.goto(url, wait_until="networkidle")
        assert response.status == 200, "Home must render successfully"
        apps = page.request.get(url + "/api/v1/apps").json()["items"]
        assert apps, "A real enrolled game is required for this journey"
        observed = next(item for item in apps if item["player_count"] is not None)
        app_id = observed["app_id"]
        assert page.get_by_role("heading", name="Steam charts", exact=True).is_visible()
        page.keyboard.press("Tab")
        assert page.get_by_role("link", name="Skip to content").evaluate("e => e === document.activeElement"), "Keyboard skip link must be first"
        page.keyboard.press("Enter")
        assert page.locator("#main").evaluate("e => e === document.activeElement")
        assert page.locator(".catalog-grid").count() == 0
        for title in ("Most played", "Top sellers"):
            assert page.get_by_role("heading", name=title, exact=True).bounding_box()["y"] < 350
        assert "revenue" not in page.locator('main').inner_text().lower()
        icons = page.locator('.game-icon')
        assert icons.count() > 0 and icons.first.evaluate('e => e.complete && e.naturalWidth > 0'), "Steam artwork must load"
        assert page.locator('.seller-table th').count() == 2, "Seller ranking must not duplicate the rank"
        page.locator("#main").evaluate("e => e.blur()")
        page.screenshot(path=str(args.output_dir / "desktop.png"), full_page=True)
        page.get_by_label("Search Steam game name or app ID").fill(observed["name"])
        page.get_by_role("button", name="Search", exact=True).click()
        assert "/search?q=" in page.url
        page.screenshot(path=str(args.output_dir / "search.png"), full_page=True)
        match = page.locator(f'.catalog-grid a[href="/apps/{app_id}"]')
        assert match.is_visible()
        match.click()
        page.wait_for_function("!document.querySelector('[data-auto-details]')", timeout=120000)
        assert page.url == f"{url}/apps/{app_id}"
        assert page.locator('.game-profile').is_visible()
        assert page.get_by_role('button', name='Refresh details').is_visible()
        chart = page.get_by_role("img", name=f"{observed['name']} player observations over 24 hours")
        chart.focus()
        page.keyboard.press("End")
        assert "observation" in page.locator(".chart-readout").inner_text()
        page.locator("details.data-table summary").click()
        assert page.locator("details.data-table[open] tbody tr").count() >= 1
        page.get_by_role("link", name="1H", exact=True).click()
        assert "hours=1" in page.url
        page.goto(url + f'/apps/{app_id}')
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), "Mobile profile must not overflow"
        page.screenshot(path=str(args.output_dir / 'profile-mobile.png'), full_page=True)
        page.set_viewport_size({"width": 1440, "height": 1080})
        for path in ("/methodology", "/status"):
            assert page.goto(url + path).status == 200
        assert page.goto(url + "/apps/4294967295").status == 404
        assert "not yet known" in page.locator("main").inner_text().lower()
        page.goto(url)
        page.set_viewport_size({"width": 390, "height": 844})
        response = page.goto(url, wait_until="networkidle")
        assert response.status == 200, "Mobile home must render successfully"
        assert page.get_by_role("heading", name="Steam charts", exact=True).is_visible()
        assert page.locator(".games-table tbody tr").count() >= 1
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), "Mobile page must not overflow horizontally"
        page.screenshot(path=str(args.output_dir / "mobile.png"), full_page=True)
        page.get_by_label("Search Steam game name or app ID").fill(observed["name"])
        page.get_by_role("button", name="Search", exact=True).click()
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), "Mobile search must not overflow"
        page.screenshot(path=str(args.output_dir / "search-mobile.png"), full_page=True)
        page.goto(url + "/search?q=zzzz-no-match-game-census")
        assert page.get_by_role("heading", name="No matching games").is_visible()
        page.goto(url + "/search")
        assert page.get_by_role("heading", name="Search Steam games").is_visible()
        after = page.request.get(url + "/api/v1/status").json()
        assert before["total_observations"] == after["total_observations"], "Profile loading must not enroll games or alter tracked player samples"
        assert not errors, errors
        assert not external, external
        print(json.dumps({"status": "succeeded", "url": url, "app_id": app_id, "observed_player_count": observed["player_count"],
                          "observed_at": observed["observed_at"], "desktop": "1440x1080", "mobile": "390x844",
                          "checks": ["stored count", "keyboard skip link", "separate catalog search", "empty search", "no search results", "charts above fold", "Steam artwork", "single seller rank", "game navigation", "compact profile", "keyboard chart", "observation table", "history window", "methodology", "status", "unknown game", "mobile overflow", "only allowlisted Steam images externally", "no browser errors", "profile load preserves tracked samples"],
                          "screenshots": [str((args.output_dir / name).resolve()) for name in ("desktop.png", "mobile.png", "search.png", "search-mobile.png", "profile-mobile.png")]}, indent=2))
        browser.close()


if __name__ == "__main__":
    main()
