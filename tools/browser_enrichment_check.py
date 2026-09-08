"""Chromium desktop/mobile enrichment checks using inspectable synthetic captures."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import socket
import sys
import threading
import time
import uuid
import uvicorn
from playwright.sync_api import sync_playwright

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tests'))
from test_api import FakeDatabase
from test_enrichment import fetch, settings
from game_census.sources import enrichment as source
from game_census.web import create_app


class FixtureDatabase(FakeDatabase):
    def __init__(self):
        super().__init__()
        self.mode='available'
        self.captures={kind:fetch(kind) for kind in source.KINDS}

    def enrichment_history(self, app_id, config, kind, **kwargs):
        result=super().enrichment_history(app_id,config,kind,**kwargs)
        capture=self.captures[kind]
        result.update(availability='stale' if self.mode=='stale' else capture.value['state'],
            observations=[{'capture_id':str(uuid.UUID(int=1)),'observed_at':capture.received_at.isoformat(),
                'value':capture.value,'matches_current_query':True,'net_delta':None}])
        if self.mode=='empty': result.update(availability='no_observations',observations=[])
        if self.mode=='failed': result['last_attempt']={'at':datetime.now(timezone.utc).isoformat(),'status':'failed',
            'error':{'code':'http_error','message':'Steam returned HTTP 503.','next_action':'Retry a bounded collection later.'}}
        if self.mode=='unsupported' and kind in ('achievements','achievement_schema'):
            result['availability']='unsupported'
            result['observations'][0]['value']={**capture.value,'state':'unsupported','achievements':[]}
        return result


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output-dir',type=Path,required=True);args=p.parse_args()
    args.output_dir.mkdir(parents=True,exist_ok=True)
    db=FixtureDatabase();app=create_app(settings(),db)
    @app.middleware('http')
    async def read_only(request,call_next):
        assert request.method=='GET','Browser fixture must not collect'
        return await call_next(request)
    listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen(128)
    server=uvicorn.Server(uvicorn.Config(app,log_level='error'));thread=threading.Thread(target=server.run,kwargs={'sockets':[listener]},daemon=True);thread.start()
    deadline=time.monotonic()+10
    while not server.started:
        if time.monotonic()>deadline: raise RuntimeError('Browser fixture server failed to start')
        time.sleep(.01)
    errors=[]
    try:
        with sync_playwright() as pw:
            browser=pw.chromium.launch()
            page=browser.new_page(viewport={'width':1280,'height':900})
            page.on('pageerror',lambda error: errors.append(str(error)))
            page.route('https://**/*',lambda route:route.abort())
            for mode in ('available','empty','failed','stale','unsupported'):
                db.mode=mode
                response=page.goto(f'http://127.0.0.1:{listener.getsockname()[1]}/apps/570')
                assert response.status==200
                assert page.get_by_text('Enrichment history',exact=False).first.is_visible()
                if mode=='available':
                    assert page.get_by_text('0.125%',exact=True).is_visible()
                    target=page.locator("section[aria-labelledby='enrichment-achievements']")
                    target.screenshot(path=str(args.output_dir/'achievements-desktop.png'))
                    summary=page.get_by_text('Current query',exact=True).first
                    summary.focus();page.keyboard.press('Enter')
                    assert summary.evaluate('(el)=>el.parentElement.open')
                if mode=='failed': assert page.get_by_text('Last attempt: failed.',exact=False).first.is_visible()
                if mode=='unsupported': assert page.get_by_text('Achievements · unsupported',exact=True).is_visible()
            db.mode='available';page.set_viewport_size({'width':390,'height':844})
            page.reload()
            assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'), 'Mobile page overflows horizontally'
            page.locator("section[aria-labelledby='enrichment-reviews']").screenshot(path=str(args.output_dir/'reviews-mobile.png'))
            assert not errors,errors
            browser.close()
        print(json.dumps({'status':'passed','states':['available','empty','failed','stale','unsupported'],'viewports':[1280,390],
                          'keyboard_query_toggle':True,'browser_errors':errors,'external_source_requests':0,'screenshots':str(args.output_dir)}))
    finally:
        server.should_exit=True;thread.join(10);listener.close()
        if thread.is_alive(): raise RuntimeError('Browser fixture did not stop')


if __name__=='__main__':main()
