"""Read epoch 3 progress and exact canary-window coverage; never collect."""
import json
import subprocess

web = 'game-census-p2-integration-web-1'
worker = 'game-census-p2-integration-web-run-479929d74896'
code = '''
import json
from datetime import timedelta
from game_census.config import load_settings
from game_census.db import Database
from game_census.schedule_coverage import calculate
s=load_settings('/config/local.json')
d=Database(s.storage.database_url.get_secret_value())
with d.connection() as c:
    c.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
    now=c.execute('SELECT clock_timestamp() AS now').fetchone()['now']
    event=c.execute("SELECT event_id,recorded_at,action,plan_hash,details FROM schedule_event WHERE action='enabled' AND details->>'epoch'='3'").fetchone()
    assert event and event['details']['app_ids']==[10,440,570]
    start=event['recorded_at']
    deadline=start+timedelta(hours=24)
    end=min(now,deadline)
    events=c.execute("SELECT event_id,recorded_at,action,plan_hash,details FROM schedule_event WHERE recorded_at>=%s AND recorded_at<%s ORDER BY recorded_at,event_id LIMIT 100", (start,end)).fetchall()
    samples=c.execute("SELECT p.app_id,p.observed_at,j.scheduled_at,j.plan_hash FROM player_sample p LEFT JOIN scheduled_job j ON j.capture_id=p.capture_id WHERE p.app_id=ANY(%s::bigint[]) AND p.observed_at>=%s-interval '600 seconds' AND p.observed_at<%s ORDER BY p.observed_at,p.app_id LIMIT 10001", ([10,440,570],start,end)).fetchall()
    assert len(samples)<=10000
    runs=c.execute("SELECT c.run_id,c.started_at,s.epoch,s.requested_cycles,s.max_run_seconds,r.status,r.finished_at FROM collection_run c JOIN scheduled_run s USING(run_id) LEFT JOIN run_completion r USING(run_id) WHERE s.epoch=3 ORDER BY c.started_at LIMIT 10").fetchall()
    attempts=c.execute("SELECT a.source,count(*) AS attempts,count(*) FILTER (WHERE r.status='succeeded') AS succeeded,count(*) FILTER (WHERE r.status='failed') AS failed,count(*) FILTER (WHERE r.status IS NULL) AS uncertain,max(a.dispatched_at) AS last_attempt FROM request_attempt a JOIN scheduled_run s USING(run_id) LEFT JOIN request_result r USING(attempt_id) WHERE s.epoch=3 GROUP BY a.source ORDER BY a.source").fetchall()
    jobs=c.execute("SELECT source,state,count(*) AS jobs FROM scheduled_job WHERE scheduled_at>=%s AND scheduled_at<%s GROUP BY source,state ORDER BY source,state", (start,end)).fetchall()
    state=c.execute('SELECT enabled,plan_hash,epoch,enabled_at FROM schedule_state WHERE singleton').fetchone()
print(json.dumps({'checked_at':now,'window_start':start,'window_end':deadline,'full_window_elapsed':now>=deadline,'state':state,'runs':runs,'attempts':attempts,'jobs':jobs,'coverage':calculate(events,samples,start,end,s.metrics.freshness_interval_multiplier)},default=str,indent=2))
'''
result = subprocess.run(['docker','exec',web,'python','-c',code],capture_output=True,text=True,timeout=30)
if result.returncode:
    raise SystemExit('Canary database snapshot failed; inspect Docker and preserve worker logs. No state was changed.')
print(result.stdout,end='')
for container in (worker,web,'game-census-p2-integration-db-1'):
    result=subprocess.run(['docker','inspect','--format','{{json .}}',container],capture_output=True,text=True,timeout=15)
    if result.returncode:
        print(json.dumps({'container':container,'inspection':'unavailable; inspect retained worker stdout/stderr for completion or failure'}))
        continue
    value=json.loads(result.stdout)
    print(json.dumps({'container':container,'image':value['Image'],'status':value['State']['Status'],'oom_killed':value['State']['OOMKilled'],'exit_code':value['State']['ExitCode'],'health':value['State'].get('Health',{}).get('Status'),'restart_policy':value['HostConfig']['RestartPolicy']}))
result=subprocess.run(['docker','stats','--no-stream','--format','{{.Name}} CPU={{.CPUPerc}} memory={{.MemUsage}}',worker,web,'game-census-p2-integration-db-1'],capture_output=True,text=True,timeout=20)
if result.returncode:
    raise SystemExit('Resource snapshot failed; retain the successful database snapshot and inspect Docker state.')
print(result.stdout,end='')
