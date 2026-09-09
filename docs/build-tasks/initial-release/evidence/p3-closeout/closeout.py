"""Read the complete epoch-3 window and summarize retained runtime evidence."""
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import re
import subprocess

root = Path(__file__).resolve().parent
web = 'game-census-p2-integration-web-1'
worker = 'game-census-p2-integration-web-run-479929d74896'
code = '''
from collections import defaultdict
from datetime import timedelta
import json
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
    start=event['recorded_at']; end=start+timedelta(hours=24)
    assert now>=end, 'The full canary window has not elapsed.'
    events=c.execute("SELECT event_id,recorded_at,action,plan_hash,details FROM schedule_event WHERE recorded_at>=%s AND recorded_at<%s ORDER BY recorded_at,event_id LIMIT 101",(start,end)).fetchall()
    assert len(events)<=100
    samples=c.execute("SELECT p.app_id,p.observed_at,j.scheduled_at,j.plan_hash FROM player_sample p LEFT JOIN scheduled_job j ON j.capture_id=p.capture_id WHERE p.app_id=ANY(%s::bigint[]) AND p.observed_at>=%s-interval '600 seconds' AND p.observed_at<%s ORDER BY p.observed_at,p.app_id LIMIT 10001",([10,440,570],start,end)).fetchall()
    assert len(samples)<=10000
    runs=c.execute("SELECT c.run_id,c.started_at,r.finished_at,r.status,s.requested_cycles,s.max_run_seconds,r.report->'cycles' AS actual_cycles,r.report->'attempts' AS attempt_totals,r.report->'catchup_pending' AS catchup_pending FROM collection_run c JOIN scheduled_run s USING(run_id) LEFT JOIN run_completion r USING(run_id) WHERE s.epoch=3 ORDER BY c.started_at LIMIT 11").fetchall()
    assert len(runs)==1 and runs[0]['status']=='succeeded' and runs[0]['actual_cycles']==288
    per_source=c.execute("SELECT a.source,count(*) AS attempts,count(*) FILTER(WHERE r.status='succeeded') AS succeeded,count(*) FILTER(WHERE r.status='failed') AS failed,count(*) FILTER(WHERE r.status IS NULL) AS uncertain,count(cap.capture_id) AS captures,min(a.dispatched_at) AS first_attempt,max(a.dispatched_at) AS last_attempt,count(*) FILTER(WHERE a.dispatched_at>=%s) AS attempts_after_deadline FROM request_attempt a JOIN scheduled_run s USING(run_id) LEFT JOIN request_result r USING(attempt_id) LEFT JOIN capture cap USING(attempt_id) WHERE s.epoch=3 GROUP BY a.source ORDER BY a.source",(end,)).fetchall()
    disable=c.execute("SELECT event_id,recorded_at,action,details FROM schedule_event WHERE action='disabled' AND recorded_at>=%s ORDER BY recorded_at,event_id LIMIT 1",(start,)).fetchone()
    state=c.execute('SELECT enabled,epoch,plan_hash FROM schedule_state WHERE singleton').fetchone()
    schema=c.execute('SELECT max(version) AS version FROM schema_migration').fetchone()['version']
    assert not state['enabled'] and disable is not None
coverage=calculate(events,samples,start,end,s.metrics.freshness_interval_multiplier)
by_app={}
for app in [10,440,570]:
    app_events=[{**e,'details':{**e['details'],'app_ids':[app]}} for e in events]
    app_samples=[p for p in samples if p['app_id']==app]
    observed=[p['observed_at'] for p in app_samples if start<=p['observed_at']<end]
    gaps=[(b-a).total_seconds() for a,b in zip(observed,observed[1:])]
    app_coverage=calculate(app_events,app_samples,start,end,s.metrics.freshness_interval_multiplier)
    by_app[app]={'samples':len(observed),'first_observed_at':observed[0],'last_observed_at':observed[-1],
        'startup_without_fresh_sample_seconds':(observed[0]-start).total_seconds(),
        'maximum_sample_gap_seconds':max(gaps),'sample_gaps_at_least_600_seconds':sum(g>=600 for g in gaps),
        'final_sample_age_at_deadline_seconds':(end-observed[-1]).total_seconds(),'coverage':app_coverage}
accepted=(coverage['tracked_app_seconds']==259200 and coverage['expected_player_occurrences']==864
    and coverage['observed_scheduled_occurrences']==864 and coverage['freshness_ratio']>=.95
    and sum(x['attempts'] for x in per_source)==1728
    and all(x['failed']==x['uncertain']==x['attempts_after_deadline']==0 and x['captures']==864 for x in per_source))
assert accepted, 'Bounded canary acceptance failed; inspect the recorded outcome.'
print(json.dumps({'checked_at':now,'schema_version':schema,'state':state,'admission_disabled':disable,
    'window_start':start,'window_end':end,'accepted_scope':'P3 three-app player/Store-name canary only',
    'coverage':coverage,'per_app':by_app,'runs':runs,'sources':per_source,'accepted':accepted},default=str,indent=2))
'''
result=subprocess.run(['docker','exec',web,'python','-c',code],capture_output=True,text=True,timeout=30)
if result.returncode:
    raise SystemExit('Fixed-window database acceptance command failed. No state was changed; inspect the retained source and database readiness.')
database=json.loads(result.stdout)

stdout=(root/'worker.stdout.txt').read_text(encoding='utf-8')
decoder=json.JSONDecoder(); messages=[]; offset=0
while offset<len(stdout):
    if stdout[offset:].strip()=='':
        break
    offset+=len(stdout[offset:])-len(stdout[offset:].lstrip())
    value,used=decoder.raw_decode(stdout[offset:]); messages.append(value); offset+=used
final=messages[-1]
assert final['run_id']==str(database['runs'][0]['run_id']) and final['status']=='succeeded'
assert final['cycles']==288 and final['request_count']==1728 and len(final['outcomes'])==1728
assert all(outcome['status']=='succeeded' for outcome in final['outcomes'])
stderr=(root/'worker.stderr.txt').read_text(encoding='utf-8')
assert not any(x in stderr for x in ('scheduler_failure','Traceback','"failed"'))
files=sorted(set([root/'first-cycle.txt', *root.glob('check-*-snapshot.txt')]))
points=defaultdict(list)
for path in files:
    text=path.read_text(encoding='utf-8')
    for name,cpu,memory,unit in re.findall(r'^(game-census-\S+) CPU=([\d.]+)% memory=([\d.]+)(KiB|MiB|GiB) /',text,re.M):
        mib=float(memory)*{'KiB':1/1024,'MiB':1,'GiB':1024}[unit]
        points[name].append({'file':path.name,'cpu_percent':float(cpu),'memory_mib':mib})
resources={name:{'sample_count':len(rows),'sampled_cpu_percent_min':min(r['cpu_percent'] for r in rows),
    'sampled_cpu_percent_max':max(r['cpu_percent'] for r in rows),
    'sampled_memory_mib_min':min(r['memory_mib'] for r in rows),'sampled_memory_mib_max':max(r['memory_mib'] for r in rows)} for name,rows in points.items()}
current=[]
for name in (worker,web,'game-census-p2-integration-db-1'):
    result=subprocess.run(['docker','inspect','--format','{{json .}}',name],capture_output=True,text=True,timeout=15)
    if result.returncode:
        assert name==worker and database['runs'][0]['status']=='succeeded'
        current.append({'container':name,'state':'removed after successful run; launched with compose run --rm'})
    else:
        value=json.loads(result.stdout)
        current.append({'container':name,'state':value['State']['Status'],'image':value['Image'],
            'health':value['State'].get('Health',{}).get('Status'),'oom_killed':value['State']['OOMKilled'],
            'restart_count':value['RestartCount']})
print(json.dumps({'database_acceptance':database,'worker_log':{'status':final['status'],'cycles':final['cycles'],
    'request_count':final['request_count'],'outcome_count':len(final['outcomes']),
    'stdout_sha256':hashlib.sha256((root/'worker.stdout.txt').read_bytes()).hexdigest(),
    'stderr_sha256':hashlib.sha256((root/'worker.stderr.txt').read_bytes()).hexdigest()},
    'resource_snapshot_files':[p.name for p in files],'resources':resources,'current_containers':current,
    'resource_limitations':'Point-in-time samples on a shared Docker host; these are sampled ranges, not continuous peaks or a full-capacity benchmark.'},indent=2))
