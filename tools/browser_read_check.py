"""Inspect cold stored pages in a real browser; all data and assets are synthetic."""
import argparse
from contextlib import contextmanager
from pathlib import Path
import socket
import sys
import threading
import time

import uvicorn
from fastapi.responses import JSONResponse
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from test_api import FakeDatabase
from test_p3_reads import ReadFixture, read_settings
from game_census.db import DatabaseError
from game_census.web import create_app


@contextmanager
def serve(db, settings):
    app = create_app(settings, db)
    mutations = []

    @app.middleware("http")
    async def only_stored_reads(request, call_next):
        if request.method != "GET":
            mutations.append(str(request.url.path))
            return JSONResponse({"error": "Synthetic browser fixture blocks collection"}, status_code=503)
        return await call_next(request)

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    worker = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    worker.start()
    deadline = time.monotonic() + 10
    try:
        while not server.started:
            if not worker.is_alive() or time.monotonic() >= deadline:
                raise RuntimeError("Synthetic read server did not start")
            time.sleep(.01)
        yield f"http://127.0.0.1:{listener.getsockname()[1]}", mutations
    finally:
        server.should_exit = True
        worker.join(10)
        listener.close()
        if worker.is_alive():
            raise RuntimeError("Synthetic read server did not stop")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile-only", action="store_true")
    parser.add_argument("--poll-only", action="store_true")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "work/browser-reads")
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    settings = read_settings()
    db = FakeDatabase() if args.profile_only else ReadFixture()
    with serve(db, settings) as (url, mutations), sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1080})
        errors, external = [], []
        page.on("pageerror", lambda error: errors.append(str(error)))
        def reject_external(route):
            external.append(route.request.url)
            route.abort()
        page.route("https://**/*", reject_external)
        if args.poll_only:
            page.clock.install()
            page.add_init_script("""window.pollProbe = {active: 0, peak: 0};
                window.fetch = (url, options = {}) => {
                  if (url !== '/api/v1/apps') throw new Error('Unexpected fixture fetch');
                  window.pollProbe.active += 1;
                  window.pollProbe.peak = Math.max(window.pollProbe.peak, window.pollProbe.active);
                  return new Promise((resolve, reject) => {
                    options.signal?.addEventListener('abort', () => {
                      window.pollProbe.active -= 1;
                      reject(new DOMException('Synthetic delayed read canceled', 'AbortError'));
                    }, {once: true});
                  });
                };""")
            page.goto(url + "/compare", wait_until="domcontentloaded")
            page.clock.run_for(settings.web.refresh_seconds * 3000)
            probe = page.evaluate("window.pollProbe")
            assert probe["peak"] == 1, f"Stored-state polling overlapped: {probe['peak']} requests in flight"
            assert "refresh check failed" in page.locator("[data-refresh-notice]").inner_text()
            page.evaluate("window.dispatchEvent(new PageTransitionEvent('pagehide'))")
            page.clock.run_for(settings.web.refresh_seconds * 2000)
            assert page.evaluate("window.pollProbe.active") == 0, "A left page retained polling"
            page.evaluate("window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted: true}))")
            assert page.evaluate("window.pollProbe.active") == 1, "A restored page did not resume polling"
            page.evaluate("Object.defineProperty(document, 'hidden', {configurable: true, get: () => true}); document.dispatchEvent(new Event('visibilitychange'))")
            page.clock.run_for(settings.web.refresh_seconds * 2000)
            assert page.evaluate("window.pollProbe.active") == 0, "Hidden page retained polling"
            page.evaluate("Object.defineProperty(document, 'hidden', {configurable: true, get: () => false}); document.dispatchEvent(new Event('visibilitychange'))")
            assert page.evaluate("window.pollProbe.active") == 1, "Visible page did not resume polling"
            print("PASS: delayed stored reads stay bounded to one in-flight poll", flush=True)
            print("PASS: timeout is visible; hidden/left pages cancel; visible/restored pages resume", flush=True)
            browser.close()
            return
        response = page.goto(url + "/apps/570", wait_until="networkidle")
        assert response.status == 200
        assert page.get_by_role("button", name="Refresh details").is_enabled()
        assert not mutations, f"A cold page load attempted collection: {mutations}"
        print("PASS: cold profile loads stored data; collection attempts: 0", flush=True)
        if not args.profile_only:
            page.goto(url, wait_until="networkidle")
            page.keyboard.press("Tab")
            assert page.get_by_role("link", name="Skip to content").evaluate("e => e === document.activeElement")
            page.keyboard.press("Enter")
            assert page.locator("#main").evaluate("e => e === document.activeElement")
            page.get_by_role("navigation", name="Main navigation").get_by_role("link", name="Rankings").click()
            assert page.get_by_role("heading", name="2 ranked games").is_visible()
            assert page.locator(".games-table tbody tr").count() == 2
            assert "1 stale, 1 without observations, 1 unsupported" in page.locator("main").inner_text()
            page.screenshot(path=str(args.output_dir / "rankings-desktop.png"), full_page=True)
            page.get_by_role("navigation", name="Main navigation").get_by_role("link", name="Compare").click()
            page.get_by_label("Synthetic Zero", exact=False).check()
            page.get_by_label("Synthetic Peer", exact=False).check()
            page.get_by_role("button", name="Compare selected games").click()
            page.wait_for_load_state("networkidle")
            assert page.locator(".comparison-game").count() == 2
            heading = page.locator(".comparison-game-heading").first.bounding_box()
            note = page.locator(".comparison-game > .section-note").first.bounding_box()
            assert note["y"] >= heading["y"] + heading["height"], "Tracking note overlaps comparison heading"
            charts = page.locator("[data-chart]")
            assert charts.count() == 2
            scales = [charts.nth(index).locator(".chart-label").all_text_contents() for index in range(2)]
            assert scales[0] == scales[1], "Charts must share both UTC and count scales"
            for index, first_count in [(0, "21"), (1, "42")]:
                chart = charts.nth(index)
                svg = chart.get_by_role("img")
                svg.focus()
                svg.press("Home")
                assert chart.locator(".chart-readout").inner_text().startswith(first_count + " concurrent players")
                svg.press("End")
                assert "0 concurrent players" in chart.locator(".chart-readout").inner_text()
                panel = page.locator(".comparison-game").nth(index)
                panel.get_by_text("View observation data", exact=False).click()
                assert panel.locator("tbody tr").count() == 3
            ids = page.locator("[id]").evaluate_all("nodes => nodes.map(node => node.id)")
            assert len(ids) == len(set(ids)), "Chart labels and controls need unique IDs"
            api_url = page.get_by_role("link", name="View comparison as JSON").get_attribute("href")
            payload = page.request.get(url + api_url).json()
            assert payload["returned_points"] == 6
            assert all(row["history"]["from"] == payload["from"] for row in payload["series"])
            page.evaluate("document.activeElement.blur(); window.scrollTo(0, 0)")
            page.screenshot(path=str(args.output_dir / "compare-desktop.png"), full_page=True)
            page.set_viewport_size({"width": 390, "height": 844})
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), "Comparison overflows on mobile"
            page.screenshot(path=str(args.output_dir / "compare-mobile.png"), full_page=True)
            page.goto(url + "/compare?app_ids=570,730,740,750,400", wait_until="networkidle")
            states = page.locator(".comparison-game-heading .state-label").all_text_contents()
            assert states == ["fresh", "stale", "no observations", "unsupported", "not tracked"]
            assert page.get_by_text("The player source timed out.", exact=False).is_visible()
            assert "Unknown time is not zero" in page.locator("main").inner_text()
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            page.screenshot(path=str(args.output_dir / "comparison-states-mobile.png"), full_page=True)
            page.goto(url + "/search?q=Catalog", wait_until="networkidle")
            page.get_by_role("link", name="Synthetic Catalog Only", exact=False).click()
            assert "not enrolled for tracking" in page.locator("main").inner_text().lower()
            assert page.get_by_role("button", name="Refresh details").is_enabled()
            details = db.game_details(570)
            details["last_refresh"] = {"status": "partial", "sources": [{"status": "failed", "error": {
                "message": "Synthetic review source unavailable.", "next_action": "Retry the explicit details action later."}}]}
            db.game_details = lambda app_id: details
            page.goto(url + "/apps/570", wait_until="networkidle")
            assert page.get_by_text("Details refresh partial", exact=True).is_visible()
            assert page.get_by_text("Synthetic review source unavailable.", exact=False).is_visible()
            page.screenshot(path=str(args.output_dir / "profile-partial-mobile.png"), full_page=True)
            db.error = DatabaseError("Synthetic local storage failure")
            response = page.goto(url + "/compare?app_ids=570", wait_until="networkidle")
            assert response.status == 503 and "report reference" in page.locator("main").inner_text().lower()
            page.screenshot(path=str(args.output_dir / "read-failure-mobile.png"), full_page=True)
            assert not mutations and not errors and not external, (mutations, errors, external)
            print("PASS: rankings, search/profile, aligned comparison, keyboard/table, mobile and availability/failure states; collection attempts: 0; external requests: 0; browser errors: 0", flush=True)
        browser.close()


if __name__ == "__main__":
    main()
