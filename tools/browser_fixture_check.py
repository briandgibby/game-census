"""Run the browser journey with synthetic stored data and zero external requests."""
import argparse
from datetime import datetime, timezone
from pathlib import Path
import socket
import sys
import threading
import time
from unittest.mock import patch
from urllib.parse import urlsplit
import uvicorn
from playwright.sync_api import Browser

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))
from test_api import FakeDatabase
from game_census.config import Settings, Storage
from game_census.sources.details import MEDIA_HOSTS
from game_census.web import create_app
import browser_check


class FixtureDatabase(FakeDatabase):
    def dashboard(self):
        value = super().dashboard()
        for chart in value['charts'].values():
            chart.update(observed_at=datetime.now(timezone.utc), age_seconds=0)
            chart['items'] = [{'app_id': 570, 'name': self.apps[0]['name'], 'rank': 1,
                'players': 0, 'peak_today': 21, 'image': 'https://shared.akamai.steamstatic.com/fixture.svg'}]
        return value

    def game_details(self, app_id):
        value = super().game_details(app_id)
        value['last_refresh'] = {'finished_at': datetime.now(timezone.utc), 'status': 'succeeded'}
        return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, default=ROOT / 'work/browser-fixture')
    args = parser.parse_args()
    app = create_app(Settings(storage=Storage(database_url='postgresql://fixture:fixture@localhost/fixture')), FixtureDatabase())
    @app.middleware('http')
    async def forbid_collection(request, call_next):
        if request.method != 'GET':
            raise RuntimeError('Offline browser fixture attempted collection')
        return await call_next(request)
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen(128)
    server = uvicorn.Server(uvicorn.Config(app, log_level='error'))
    thread = threading.Thread(target=server.run, kwargs={'sockets': [listener]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if not thread.is_alive() or time.monotonic() >= deadline:
            raise RuntimeError('Synthetic browser server did not become ready')
        time.sleep(.01)
    original = Browser.new_page
    intercepted = []
    def new_page(browser, *values, **options):
        page = original(browser, *values, **options)
        def offline_asset(route):
            if urlsplit(route.request.url).hostname not in MEDIA_HOSTS or route.request.resource_type != 'image':
                route.abort()
                raise RuntimeError('Unexpected external request in synthetic browser test')
            intercepted.append(route.request.url)
            route.fulfill(content_type='image/svg+xml', body='<svg xmlns="http://www.w3.org/2000/svg" width="52" height="26"><rect width="52" height="26" fill="#bada55"/></svg>')
        page.route('https://**/*', offline_asset)
        return page
    try:
        print('Synthetic fixture: one tracked game; in-memory stored reads; all external artwork requests intercepted.', flush=True)
        with patch.object(Browser, 'new_page', new_page), patch.object(sys, 'argv', [
            'browser_check.py', '--url', f'http://127.0.0.1:{listener.getsockname()[1]}', '--output-dir', str(args.output_dir)]):
            browser_check.main()
        assert intercepted, 'Artwork fixture was not exercised'
        print('Synthetic browser fixture passed; external network requests: 0.', flush=True)
    finally:
        server.should_exit = True
        thread.join(10)
        listener.close()
        if thread.is_alive():
            raise RuntimeError('Synthetic browser server did not stop')


if __name__ == '__main__':
    main()
