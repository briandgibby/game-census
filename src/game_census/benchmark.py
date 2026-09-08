"""Bounded open-loop read measurement; synthetic acceptance tooling only."""
from concurrent.futures import ThreadPoolExecutor
import math
import platform
import time


def measure(read, *, requests_per_second, seconds, workers):
    if (type(requests_per_second) is not int or not 1<=requests_per_second<=50
            or type(seconds) is not int or not 1<=seconds<=300
            or type(workers) is not int or not 1<=workers<=25):
        raise ValueError('Benchmark bounds: 1–50 requests/second, 1–300 seconds, 1–25 workers.')
    total=requests_per_second*seconds
    anchor=time.monotonic()
    def one(index):
        due=anchor+index/requests_per_second
        delay=due-time.monotonic()
        if delay>0: time.sleep(delay)
        began=time.monotonic()
        try:
            group=read(index)
            return {'ok':True,'group':group,'latency':time.monotonic()-due,'service':time.monotonic()-began,'queue':max(0,began-due)}
        except Exception as error:
            return {'ok':False,'latency':time.monotonic()-due,'error_type':type(error).__name__}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results=list(pool.map(one,range(total)))
    elapsed=time.monotonic()-anchor
    latencies=sorted(r['latency'] for r in results)
    groups={}
    for name in ('summary','history'):
        times=sorted(r['latency'] for r in results if r.get('group')==name)
        if times: groups[name]={'requests':len(times),'p95_seconds':times[math.ceil(len(times)*.95)-1]}
    return {'requested_rps':requests_per_second,'duration_seconds':seconds,'requests':total,'workers':workers,
            'elapsed_seconds':elapsed,'completed_rps':total/elapsed,
            'p95_seconds':latencies[math.ceil(total*.95)-1], 'failed_requests':sum(not r['ok'] for r in results),
            'maximum_queue_seconds':max((r.get('queue',0) for r in results),default=0),
            'error_types':sorted({r['error_type'] for r in results if not r['ok']}),
            'groups':groups,
            'machine':{'system':platform.system(),'release':platform.release(),'machine':platform.machine(),'python':platform.python_version()},
            'latency_basis':'includes delay after intended dispatch time; failures remain in the denominator'}
