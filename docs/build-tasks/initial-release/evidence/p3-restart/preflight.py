"""Bounded read-only canary runtime and retained-ledger inspection."""
import json
import subprocess
import urllib.request

DB = 'game-census-p2-integration-db-1'
WEB = 'game-census-p2-integration-web-1'

def run(command):
    result = subprocess.run(command, capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise RuntimeError(f'{command[0]} diagnostic exited {result.returncode}; inspect Docker health')
    return result.stdout

for container in (DB, WEB):
    value = json.loads(run(['docker', 'inspect', '--format', '{{json .}}', container]))
    print(json.dumps({'container': container, 'image': value['Image'],
        'state': {k: value['State'].get(k) for k in ('Status', 'Running', 'OOMKilled', 'ExitCode', 'StartedAt', 'FinishedAt')},
        'health': value['State'].get('Health', {}).get('Status'),
        'restart_count': value['RestartCount'], 'restart_policy': value['HostConfig']['RestartPolicy']}, sort_keys=True))
print(json.dumps({'local_image': run(['docker', 'image', 'inspect', '--format', '{{.Id}}', 'game-census-p2-integration:local']).strip()}))
for endpoint in ('live', 'ready'):
    with urllib.request.urlopen(f'http://127.0.0.1:8003/health/{endpoint}', timeout=10) as response:
        print(json.dumps({'endpoint': endpoint, 'http_status': response.status, 'body': json.loads(response.read())}))
print(run(['docker', 'stats', '--no-stream', '--format', '{{.Name}} CPU={{.CPUPerc}} memory={{.MemUsage}}', DB, WEB]), end='')
code = '''
import json
from game_census.config import load_settings
from game_census.db import Database
from game_census.schedule_coverage import calculate
s = load_settings('/config/local.json')
d = Database(s.storage.database_url.get_secret_value())
with d.connection() as c:
    c.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
    health = c.execute("SELECT clock_timestamp() AS checked_at, pg_postmaster_start_time() AS database_started_at, (SELECT max(version) FROM schema_migration) AS schema_version, (SELECT count(*) FROM pg_locks WHERE locktype='advisory' AND objid=734801002) AS collection_locks").fetchone()
    events = c.execute("SELECT event_id,recorded_at,action,plan_hash,details FROM schedule_event ORDER BY recorded_at,event_id LIMIT 100").fetchall()
    old = c.execute("SELECT c.run_id,c.started_at,s.epoch,r.finished_at,r.status FROM collection_run c JOIN scheduled_run s USING(run_id) LEFT JOIN run_completion r USING(run_id) WHERE c.run_id=%s", ('8caaec5a-3c72-4f6c-b122-5604fcac4f82',)).fetchone()
    attempts = c.execute("SELECT a.source,count(*) AS attempts,count(*) FILTER (WHERE r.status='succeeded') AS succeeded,count(*) FILTER (WHERE r.status='failed') AS failed,count(*) FILTER (WHERE r.status IS NULL) AS uncertain,min(a.dispatched_at) AS first_attempt,max(a.dispatched_at) AS last_attempt FROM request_attempt a LEFT JOIN request_result r USING(attempt_id) WHERE a.run_id=%s GROUP BY a.source ORDER BY a.source", ('8caaec5a-3c72-4f6c-b122-5604fcac4f82',)).fetchall()
    quota = c.execute("SELECT host_group,count(*) AS charged_attempts FROM request_attempt WHERE dispatched_at>clock_timestamp()-interval '24 hours' GROUP BY host_group ORDER BY host_group").fetchall()
    start = next(e['recorded_at'] for e in events if e['action']=='enabled' and e['details'].get('epoch')==1)
    end = next(e['recorded_at'] for e in events if e['action']=='disabled' and e['recorded_at']>start)
    samples = c.execute("SELECT p.app_id,p.observed_at,j.scheduled_at,j.plan_hash FROM player_sample p LEFT JOIN scheduled_job j ON j.capture_id=p.capture_id WHERE p.app_id=ANY(%s::bigint[]) AND p.observed_at >= %s-interval '600 seconds' AND p.observed_at<%s ORDER BY p.observed_at,p.app_id", ([10,440,570],start,end)).fetchall()
    assert len(samples)<10000
print(json.dumps({'database':health,'interrupted_run':old,'interrupted_run_attempts':attempts,'rolling_24h_attempts':quota,'control_events':events,'interrupted_window_coverage':calculate(events,samples,start,end,s.metrics.freshness_interval_multiplier)},default=str,indent=2))
'''
print(run(['docker', 'exec', WEB, 'python', '-c', code]), end='')
