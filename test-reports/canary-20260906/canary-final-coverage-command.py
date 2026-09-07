import subprocess
import sys

code = '''
import json
from datetime import datetime
from game_census.config import load_settings
from game_census.db import Database
from game_census.schedule_coverage import calculate
settings = load_settings('/config/local.json')
start = datetime.fromisoformat('2026-09-06T03:35:23.553903+00:00')
end = datetime.fromisoformat('2026-09-07T03:35:23.553903+00:00')
with Database(settings.storage.database_url.get_secret_value()).connection() as conn:
    conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
    events = conn.execute("SELECT event_id,recorded_at,action,plan_hash,details FROM schedule_event WHERE action IN ('enabled','disabled') ORDER BY recorded_at,event_id").fetchall()
    samples = conn.execute("SELECT p.app_id,p.observed_at,p.capture_id,j.scheduled_at,j.plan_hash FROM player_sample p LEFT JOIN scheduled_job j ON j.capture_id=p.capture_id AND j.source='steam_current_players_v1' WHERE p.app_id=ANY(%s::bigint[]) AND p.observed_at >= %s - interval '10 minutes' AND p.observed_at < %s ORDER BY p.app_id,p.observed_at", ([440,570,730],start,end)).fetchall()
result = calculate(events,samples,start,end,settings.metrics.freshness_interval_multiplier)
assert result['tracked_app_seconds'] == 259200
assert result['expected_player_occurrences'] == 864
result['stale_app_seconds'] = result['tracked_app_seconds'] - result['fresh_app_seconds']
result['target'] = 0.95
result['passes_freshness_target'] = result['freshness_ratio'] >= 0.95
result['control_events'] = events
print(json.dumps(result,indent=2,default=str))
'''
command = ['docker','exec','-i','game-census-p2-canary-web-1','python','-']
result = subprocess.run(command,input=code.encode(),stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
sys.stdout.buffer.write(result.stdout)
sys.exit(result.returncode)
