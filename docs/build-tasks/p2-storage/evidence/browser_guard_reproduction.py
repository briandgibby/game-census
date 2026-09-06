"""Run the actual browser journey with a failed second home-page response.

Execute with the project's local Playwright environment and a running instance.
All requests remain local; the injected response changes no persisted data.
"""
from contextlib import contextmanager
import importlib.util
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch

repo = Path(__file__).resolve().parents[4]
spec = importlib.util.spec_from_file_location("browser_check", repo / "tools/browser_check.py")
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)
original = check.sync_playwright


@contextmanager
def injected_playwright():
    with original() as engine:
        launch = engine.chromium.launch

        def injected_launch(*args, **kwargs):
            browser = launch(*args, **kwargs)
            new_page = browser.new_page

            def injected_page(*args, **kwargs):
                page = new_page(*args, **kwargs)
                home_requests = 0

                def route_home(route):
                    nonlocal home_requests
                    home_requests += 1
                    if home_requests == 2:
                        route.fulfill(status=503, content_type="text/html", body="<main>Stored data is unavailable.</main>")
                    else:
                        route.continue_()

                page.route("http://127.0.0.1:8000/", route_home)
                return page

            browser.new_page = injected_page
            return browser

        engine.chromium.launch = injected_launch
        yield engine


with TemporaryDirectory(prefix="game-census-browser-guard-") as output:
    with patch.object(check, "sync_playwright", injected_playwright), patch.object(sys, "argv", ["browser_check", "--output-dir", output]):
        try:
            check.main()
        except AssertionError as error:
            assert str(error) == "Mobile home must render successfully", str(error)
            print("PASS: browser journey rejects the injected mobile HTTP 503 response")
        else:
            raise AssertionError("Browser journey incorrectly accepted mobile HTTP 503")
