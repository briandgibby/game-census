"""PostgreSQL persistence and bounded read models; raw database errors stay private."""
from contextlib import contextmanager, nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
import hashlib
import json
import time
import uuid
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from . import metrics, projections
from .sources import SourceError, players


class DatabaseError(Exception):
    """A safe, actionable storage diagnostic, without a DSN or server error text."""


class QueryLimitError(DatabaseError):
    """A history request exceeds the configured public read bounds."""


def iso(value):
    return value.isoformat() if value is not None else None


class Database:
    def __init__(self, dsn: str):
        self._dsn = dsn

    @contextmanager
    def connection(self):
        try:
            with psycopg.connect(self._dsn, connect_timeout=5, row_factory=dict_row,
                                 application_name="game_census", options="-c statement_timeout=15000") as conn:
                yield conn
        except psycopg.Error:
            raise DatabaseError("PostgreSQL operation failed. Check storage.database_url and database status; run initialize before collecting or serving.") from None

    def initialize(self, app_ids: list[int], interval_seconds: int) -> dict:
        from .sources.base import validate_app_id
        for app_id in app_ids:
            validate_app_id(app_id)
        if type(interval_seconds) is not int or not 300 <= interval_seconds <= 604800:
            raise DatabaseError("tracking.interval_seconds must be an integer from 300 to 604800.")
        migration_dir = Path(__file__).parent / "migrations"
        with self.connection() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(734801001)")
            for migration in sorted(migration_dir.glob("*.sql")):
                conn.execute(migration.read_text(encoding="utf-8"))
                if migration.name == "004_storage.sql":
                    from .storage import create_empty_layout
                    create_empty_layout(conn)
            for app_id in app_ids:
                conn.execute("INSERT INTO app(app_id) VALUES (%s) ON CONFLICT DO NOTHING", (app_id,))
                latest = conn.execute("""SELECT t.interval_seconds,s.ended_at FROM tracking_interval t
                    LEFT JOIN tracking_stop s ON s.interval_id=t.id WHERE t.app_id=%s ORDER BY t.started_at DESC,t.id DESC LIMIT 1""", (app_id,)).fetchone()
                if latest is None or latest["ended_at"] is not None or latest["interval_seconds"] != interval_seconds:
                    conn.execute("INSERT INTO tracking_interval(app_id,interval_seconds) VALUES (%s,%s)", (app_id, interval_seconds))
            counts = conn.execute("SELECT (SELECT count(*) FROM app) AS tracked_apps, (SELECT count(*) FROM capture) AS captures").fetchone()
            version = conn.execute("SELECT max(version) AS version FROM schema_migration").fetchone()["version"]
            enabled = conn.execute("SELECT enabled FROM schedule_state WHERE singleton").fetchone()["enabled"]
        return {"schema_version": version, **counts, "enrolled_app_ids": app_ids, "scheduler": "enabled" if enabled else "disabled"}

    @contextmanager
    def collection_lock(self):
        with self.connection() as conn:
            acquired = conn.execute("SELECT pg_try_advisory_lock(734801002) AS acquired").fetchone()["acquired"]
            if not acquired:
                raise DatabaseError("Another collector is active. Wait for the manual collection or scheduler worker to finish before collecting again.")
            try:
                yield
            finally:
                conn.execute("SELECT pg_advisory_unlock(734801002)")

    def start_run(self, app_ids: list[int], sources: list[str]) -> str:
        run_id = str(uuid.uuid4())
        with self.connection() as conn:
            conn.execute("INSERT INTO collection_run(run_id,app_ids,sources) VALUES (%s,%s,%s)",
                         (run_id, Jsonb(app_ids), Jsonb(sources)))
        return run_id

    def reserve_attempt(self, run_id: str, app_id: int, source: str, host_group: str,
                        quota: int, interval_seconds: float, *, conn=None, deadline=None) -> str:
        """Charge before dispatch; a supplied transaction also owns scheduler fencing.

        The deadline is the final admissible reservation time, already reduced by
        the caller's required HTTP execution time. An uncertain dispatch remains
        charged; there is no refund or second admission ledger.
        """
        with (self.connection() if conn is None else nullcontext(conn)) as conn:
            conn.execute("SELECT pg_advisory_xact_lock(734801003)")
            def require_time(wait_seconds=0):
                if deadline is None:
                    return
                remaining = conn.execute("SELECT EXTRACT(EPOCH FROM (%s::timestamptz-clock_timestamp())) AS remaining", (deadline,)).fetchone()["remaining"]
                if remaining is None or float(remaining) <= wait_seconds:
                    raise SourceError("source_deadline", "The collection deadline cannot accommodate source pacing and dispatch.",
                                      "Inspect scheduler status and allow a new scheduled occurrence; do not backfill the expired one.")
            require_time()
            cooldown = conn.execute("SELECT max(expires_at) AS expires_at FROM source_cooldown WHERE host_group=%s AND expires_at>clock_timestamp()", (host_group,)).fetchone()["expires_at"]
            if cooldown is not None:
                raise SourceError("source_cooldown", f"Steam requested a {host_group} request cooldown.",
                                  f"Wait until {cooldown.isoformat()} before collecting from this source again.")
            count = conn.execute("SELECT count(*) AS n FROM request_attempt WHERE host_group=%s AND dispatched_at > clock_timestamp()-interval '24 hours'", (host_group,)).fetchone()["n"]
            if count >= quota:
                raise SourceError("quota_exhausted", f"The {host_group} rolling 24-hour request budget is exhausted.",
                                  "Wait for reserved attempts to leave the rolling window before collecting again.")
            previous = conn.execute("SELECT EXTRACT(EPOCH FROM (clock_timestamp()-max(dispatched_at))) AS elapsed FROM request_attempt WHERE host_group=%s", (host_group,)).fetchone()["elapsed"]
            if previous is not None:
                remaining = interval_seconds - float(previous)
                if remaining > 0:
                    require_time(remaining)
                    time.sleep(remaining)
            require_time()
            attempt_id = str(uuid.uuid4())
            conn.execute("INSERT INTO request_attempt(attempt_id,run_id,app_id,source,host_group) VALUES (%s,%s,%s,%s,%s)",
                         (attempt_id, run_id, app_id, source, host_group))
        return attempt_id

    def record_capture(self, run_id: str, attempt_id: str, capture, *, conn=None) -> str:
        capture_id = str(uuid.uuid4())
        row = {"capture_id": capture_id, "app_id": capture.app_id, "source": capture.source,
               "source_version": capture.source_version, "received_at": capture.received_at,
               "payload": capture.payload, "checksum": capture.checksum, "parameters": capture.parameters}
        with (self.connection() if conn is None else nullcontext(conn)) as conn:
            from .storage import register_capture
            register_capture(conn,capture_id,attempt_id,capture)
            conn.execute("""INSERT INTO capture(capture_id,attempt_id,run_id,app_id,source,source_version,
                request_started_at,received_at,http_status,parameters,payload,checksum,capture_form)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (capture_id, attempt_id, run_id, capture.app_id, capture.source, capture.source_version,
                 capture.request_started_at, capture.received_at, capture.http_status, Jsonb(capture.parameters),
                 capture.payload, capture.checksum, capture.capture_form))
            projections.project(conn, row)
            conn.execute("INSERT INTO request_result(attempt_id,status,http_status) VALUES (%s,'succeeded',%s)",
                         (attempt_id, capture.http_status))
        return capture_id

    def record_failure(self, attempt_id: str, error: SourceError, *, conn=None) -> None:
        with (self.connection() if conn is None else nullcontext(conn)) as conn:
            conn.execute("INSERT INTO request_result(attempt_id,status,http_status,error) VALUES (%s,'failed',%s,%s)",
                         (attempt_id, error.http_status, Jsonb(error.as_dict())))
            if error.retry_after_seconds is not None and error.retry_after_seconds > 0:
                conn.execute("""INSERT INTO source_cooldown(cooldown_id,attempt_id,host_group,expires_at)
                    SELECT %s,attempt_id,host_group,clock_timestamp()+(%s * interval '1 second')
                    FROM request_attempt WHERE attempt_id=%s""",
                    (str(uuid.uuid4()),error.retry_after_seconds,attempt_id))

    def finish_run(self, report: dict) -> None:
        with self.connection() as conn:
            conn.execute("INSERT INTO run_completion(run_id,status,report) VALUES (%s,%s,%s)",
                         (report["run_id"], report["status"], Jsonb(report)))

    def last_run(self) -> dict | None:
        with self.connection() as conn:
            row = conn.execute("""SELECT r.*, c.finished_at,c.status,c.report FROM collection_run r
                LEFT JOIN run_completion c USING(run_id) ORDER BY r.started_at DESC LIMIT 1""").fetchone()
        if row is None:
            return None
        if row["report"] is not None:
            return {**row["report"], "started_at": iso(row["started_at"]), "finished_at": iso(row["finished_at"])}
        return {"run_id": str(row["run_id"]), "status": "unfinished", "app_ids": row["app_ids"],
                "started_at": iso(row["started_at"]), "finished_at": None,
                "next_action": "Inspect the collector process. An interrupted request remains charged; a new bounded run cannot fill its historical gap."}

    def status(self) -> dict:
        with self.connection() as conn:
            counts = conn.execute("""SELECT (SELECT count(*) FROM app) AS tracked_apps,
                (SELECT count(*) FROM capture) AS captures,(SELECT count(*) FROM player_sample) AS player_samples,
                (SELECT count(*) FROM request_attempt a LEFT JOIN request_result r USING(attempt_id)
                  WHERE r.attempt_id IS NULL) AS uncertain_attempts""").fetchone()
            groups = conn.execute("SELECT host_group,count(*) AS n FROM request_attempt WHERE dispatched_at>clock_timestamp()-interval '24 hours' GROUP BY host_group").fetchall()
            cooldowns = conn.execute("SELECT host_group,max(expires_at) AS expires_at FROM source_cooldown WHERE expires_at>clock_timestamp() GROUP BY host_group").fetchall()
            schedule = conn.execute("SELECT enabled,plan_hash,epoch,enabled_at,cursor_at FROM schedule_state WHERE singleton").fetchone()
            jobs = conn.execute("SELECT state,count(*) AS n FROM scheduled_job GROUP BY state").fetchall()
        return {"database": "ok", **counts, "request_attempts_24h": {"webapi": 0, "store": 0, **{g["host_group"]: g["n"] for g in groups}},
                "source_cooldowns": [{"host_group": row["host_group"], "expires_at": iso(row["expires_at"])} for row in cooldowns],
                "last_run": self.last_run(), "scheduler": "enabled" if schedule["enabled"] else "disabled",
                "scheduler_state": {"enabled": schedule["enabled"], "plan_hash": schedule["plan_hash"],
                                    "epoch": schedule["epoch"], "enabled_at": iso(schedule["enabled_at"]),
                                    "cursor_at": iso(schedule["cursor_at"]),
                                    "jobs": {row["state"]: row["n"] for row in jobs}}}

    def list_apps(self, settings) -> list[dict]:
        with self.connection() as conn:
            rows = conn.execute("""SELECT a.app_id,
                (SELECT min(started_at) FROM tracking_interval WHERE app_id=a.app_id) AS tracking_started_at,
                n.name,p.player_count,p.observed_at,
                t.interval_seconds AS expected_interval_seconds,ts.ended_at AS tracking_ended_at,
                (SELECT count(*) FROM player_sample ps WHERE ps.app_id=a.app_id) AS sample_count,
                (SELECT max(player_count) FROM player_sample ps WHERE ps.app_id=a.app_id) AS highest_recorded,
                (SELECT max(player_count) FROM player_sample ps WHERE ps.app_id=a.app_id AND observed_at>=clock_timestamp()-interval '24 hours') AS observed_24h_peak,
                ra.dispatched_at,rr.status AS attempt_status,rr.error AS attempt_error
                FROM app a
                LEFT JOIN latest_app_name n ON n.app_id=a.app_id
                LEFT JOIN LATERAL (SELECT * FROM player_sample WHERE app_id=a.app_id ORDER BY observed_at DESC,capture_id DESC LIMIT 1) p ON true
                LEFT JOIN LATERAL (SELECT * FROM tracking_interval WHERE app_id=a.app_id ORDER BY started_at DESC,id DESC LIMIT 1) t ON true
                LEFT JOIN tracking_stop ts ON ts.interval_id=t.id
                LEFT JOIN LATERAL (SELECT * FROM request_attempt WHERE app_id=a.app_id AND source=%s ORDER BY dispatched_at DESC LIMIT 1) ra ON true
                LEFT JOIN request_result rr ON rr.attempt_id=ra.attempt_id ORDER BY a.app_id""", (players.SOURCE,)).fetchall()
        now = datetime.now(timezone.utc)
        result = []
        for row in rows:
            at = row["observed_at"]
            state = "no_observations" if at is None else ("fresh" if (now-at).total_seconds() <= row["expected_interval_seconds"]*settings.metrics.freshness_interval_multiplier else "stale")
            if row["tracking_ended_at"] is not None:
                state = "not_tracked"
            last_attempt = None if row["dispatched_at"] is None else {"status": row["attempt_status"] or "uncertain", "at": iso(row["dispatched_at"]), "error": row["attempt_error"]}
            result.append({"app_id": row["app_id"], "name": row["name"] or f"Steam app {row['app_id']}",
                           "player_count": row["player_count"], "availability": state, "observed_at": iso(at),
                           "tracking_started_at": iso(row["tracking_started_at"]), "sample_count": row["sample_count"],
                           "tracking_ended_at": iso(row["tracking_ended_at"]),
                           "observed_24h_peak": row["observed_24h_peak"], "highest_recorded": row["highest_recorded"],
                           "expected_interval_seconds": row["expected_interval_seconds"], "last_attempt": last_attempt,
                           "source": players.SOURCE, "source_version": players.VERSION, "source_url": players.DOCUMENTATION_URL})
        return result

    def app_detail(self, app_id: int, settings) -> dict | None:
        return next((row for row in self.list_apps(settings) if row["app_id"] == app_id), None)

    def catalog_sync_state(self):
        from . import catalog
        return catalog.state(self)

    def latest_cohort(self):
        with self.connection() as conn:
            return conn.execute("SELECT * FROM cohort_event ORDER BY id DESC LIMIT 1").fetchone()

    def catalog(self, query="", page=1, page_size=25):
        # Even one app per page can cover every supported uint32 Steam app ID.
        # The maximum offset with 500 rows per page remains within SQL bigint.
        if not 1 <= page <= 4294967295 or not 1 <= page_size <= 500 or len(query) > 100:
            raise QueryLimitError("Catalog search exceeds its page or query bounds.")
        # Filter after selecting each app's latest known name, including enrolled apps.
        cte = """WITH latest AS (SELECT * FROM latest_app_name),
            matching AS (SELECT *,EXISTS(SELECT 1 FROM tracking_interval t WHERE t.app_id=latest.app_id) AS has_tracking_history,
              COALESCE((SELECT s.interval_id IS NULL FROM tracking_interval t LEFT JOIN tracking_stop s ON s.interval_id=t.id
                WHERE t.app_id=latest.app_id ORDER BY t.started_at DESC,t.id DESC LIMIT 1),false) AS tracked
              FROM latest WHERE name ILIKE %s ESCAPE '\\' OR app_id::text=%s) """
        escaped = query.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        params = (f"%{escaped}%", query.strip())
        with self.connection() as conn:
            total = conn.execute(cte + "SELECT count(*) AS n FROM matching", params).fetchone()["n"]
            rows = conn.execute(cte + "SELECT * FROM matching ORDER BY lower(name),app_id LIMIT %s OFFSET %s",
                                (*params, page_size, (page-1)*page_size)).fetchall()
        for row in rows:
            row["observed_at"] = iso(row["observed_at"])
        return {"items": rows, "total": total, "page": page, "page_size": page_size,
                "query": query, "scope": "locally discovered Steam apps", "sync": self.catalog_sync_state()}

    def discovered_app(self, app_id):
        with self.connection() as conn:
            row = conn.execute("""SELECT e.app_id,e.name,s.observed_at,s.source FROM catalog_entry e
                JOIN discovery_snapshot s USING(capture_id) WHERE e.app_id=%s
                ORDER BY s.observed_at DESC,s.capture_id DESC LIMIT 1""", (app_id,)).fetchone()
        if row:
            row["observed_at"] = iso(row["observed_at"])
            row["charts"] = [{"source": source, "observed_at": chart["observed_at"], **item}
                             for source, chart in self.dashboard()["charts"].items()
                             for item in chart["items"] if item["app_id"] == app_id]
        return row

    def game_details(self, app_id):
        """Bounded per-source snapshots and local price history; no Steam reads."""
        from .sources.details import ADAPTERS, STORE, CURRENT
        snapshots, updates = {}, []
        with self.connection() as conn:
            for source in ADAPTERS:
                row = conn.execute("""SELECT capture_id,source,observed_at,value FROM discovery_snapshot
                    WHERE source=%s AND parameters->>'requested_app_id'=%s
                    ORDER BY observed_at DESC,capture_id DESC LIMIT 1""", (source, str(app_id))).fetchone()
                if row:
                    snapshots[source] = {**row["value"], "capture_id": str(row["capture_id"]), "observed_at": iso(row["observed_at"])}
            rows = conn.execute("""SELECT capture_id,observed_at,value->'price' AS price,value->'is_free' AS is_free
                FROM discovery_snapshot WHERE source=%s AND parameters->>'requested_app_id'=%s
                ORDER BY observed_at DESC,capture_id DESC LIMIT 100""", (STORE, str(app_id))).fetchall()
            prices = [{"capture_id": str(row["capture_id"]), "observed_at": iso(row["observed_at"]), "price": row["price"], "is_free": row["is_free"]} for row in rows]
            rows = conn.execute("""SELECT capture_id,source,observed_at FROM discovery_snapshot
                WHERE source=ANY(%s) AND parameters->>'requested_app_id'=%s
                ORDER BY observed_at DESC,capture_id DESC LIMIT 20""", (list(ADAPTERS), str(app_id))).fetchall()
            updates = [{"capture_id": str(row["capture_id"]), "source": row["source"], "observed_at": iso(row["observed_at"])} for row in rows]
            current = conn.execute("""SELECT max((value->>'player_count')::bigint) AS highest,
                max((value->>'player_count')::bigint) FILTER (WHERE observed_at >= now()-interval '24 hours') AS peak
                FROM discovery_snapshot WHERE source=%s AND parameters->>'requested_app_id'=%s""", (CURRENT, str(app_id))).fetchone()
            latest_run = conn.execute("""SELECT report,finished_at FROM run_completion WHERE report->>'requested_app_id'=%s
                ORDER BY finished_at DESC LIMIT 1""", (str(app_id),)).fetchone()
        return {"snapshots": snapshots, "prices": prices, "updates": updates,
                "last_refresh": {**latest_run["report"], "finished_at": iso(latest_run["finished_at"])} if latest_run else None,
                "highest_recorded": current["highest"], "observed_24h_peak": current["peak"]}

    def dashboard(self):
        from .sources.discovery import PLAYED, SALES, SEARCH, CATALOG, URLS
        charts, attempts, played_snapshots = {}, {}, []
        with self.connection() as conn:
            counts = conn.execute("""SELECT (SELECT count(DISTINCT app_id) FROM
                (SELECT app_id FROM catalog_entry UNION SELECT app_id FROM app) a) AS catalog_apps,
                (SELECT count(DISTINCT app_id) FROM tracking_interval) AS tracked_apps""").fetchone()
            for source in (PLAYED, SALES, SEARCH, CATALOG):
                attempt = conn.execute("""SELECT a.dispatched_at,r.status,r.error FROM request_attempt a
                    LEFT JOIN request_result r USING(attempt_id) WHERE a.source=%s ORDER BY a.dispatched_at DESC LIMIT 1""", (source,)).fetchone()
                attempts[source] = None if attempt is None else {"at": iso(attempt["dispatched_at"]), "status": attempt["status"] or "uncertain", "error": attempt["error"]}
                # Admission failures (quota/cooldown) have no dispatched attempt.
                # Their durable run outcome still supersedes an earlier success.
                completion = conn.execute("""SELECT c.finished_at,c.report FROM run_completion c
                    JOIN collection_run r USING(run_id) WHERE r.sources ? %s
                    ORDER BY c.finished_at DESC LIMIT 1""", (source,)).fetchone()
                if completion and (attempt is None or completion["finished_at"] >= attempt["dispatched_at"]):
                    outcomes = [row for row in completion["report"].get("sources", []) if row["source"] == source]
                    if outcomes and outcomes[-1]["status"] == "failed":
                        attempts[source] = {"at": iso(completion["finished_at"]), "status": "failed", "error": outcomes[-1].get("error")}
                if source not in (PLAYED, SALES):
                    continue
                rows = conn.execute("SELECT * FROM discovery_snapshot WHERE source=%s ORDER BY observed_at DESC,capture_id DESC LIMIT 2", (source,)).fetchall()
                latest = rows[0] if rows else None
                if latest:
                    from .sources.details import ChartImages
                    artwork = ChartImages()
                    raw = conn.execute("SELECT payload FROM capture WHERE capture_id=%s", (latest["capture_id"],)).fetchone()
                    artwork.feed(bytes(raw["payload"]).decode("utf-8"))
                    latest["value"]["items"] = [{**item, "image": artwork.images.get(item["app_id"])} for item in latest["value"]["items"]]
                charts[source] = {"items": latest["value"]["items"] if latest else [],
                                  "observed_at": iso(latest["observed_at"]) if latest else None,
                                  "source_url": URLS[source], "scope": "Steam global chart; app entries only",
                                  "age_seconds": max(0, int((datetime.now(timezone.utc)-latest["observed_at"]).total_seconds())) if latest else None}
                if source == PLAYED:
                    played_snapshots = rows
        trending = {"items": [], "from": None, "to": None, "scope": "Positive concurrent-player growth among apps present in both latest most-played snapshots"}
        if len(played_snapshots) == 2 and played_snapshots[0]["observed_at"] > played_snapshots[1]["observed_at"]:
            latest, previous = played_snapshots
            old = {item["app_id"]: item["players"] for item in previous["value"]["items"]}
            trending.update({"from": iso(previous["observed_at"]), "to": iso(latest["observed_at"])})
            for item in latest["value"]["items"]:
                before = old.get(item["app_id"])
                if before is not None and item["players"] > before:
                    trending["items"].append({**item, "change": item["players"]-before,
                                               "percent_change": round(100*(item["players"]-before)/before, 1) if before else None})
            trending["items"].sort(key=lambda row: (-row["change"], row["app_id"]))
        return {**counts, "charts": charts, "trending": trending, "latest_attempts": attempts,
                "catalog_sync": self.catalog_sync_state()}

    def history(self, app_id: int, settings, hours: int = 24, resolution: str = "raw") -> dict | None:
        from .queries import window
        start, end = window(settings, hours=hours, now=datetime.now(timezone.utc))
        return self.history_range(app_id, settings, start, end, resolution)

    def history_range(self, app_id: int, settings, start: datetime, end: datetime, resolution: str = "raw") -> dict | None:
        from .queries import window
        start, end = window(settings, from_time=start, to=end, now=datetime.now(timezone.utc))
        if resolution not in ("raw", "auto"):
            raise QueryLimitError("resolution must be raw or auto. Use auto for peak-preserving bounded history.")
        from .cache import history
        return history(self,settings,app_id,start,end,resolution)

    def rebuild(self) -> dict:
        """Replay retained captures, add missing rows, and prove all projections match."""
        digest = hashlib.sha256()
        with self.connection() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(734801002)")
            captures = conn.execute("SELECT * FROM capture ORDER BY received_at,capture_id").fetchall()
            for row in captures:
                projections.project(conn,row)
                digest.update(f"{row['capture_id']}:{row['checksum']}\n".encode())
            for capture in captures:
                projections.verify(conn, capture)
            projected = projections.count(conn)
            if projected != len(captures):
                raise DatabaseError("Projection row counts do not match retained captures. Inspect a scratch restore before repairing projections.")
        return {"status": "succeeded", "captures_replayed": len(captures), "projections_verified": projected,
                "capture_manifest_sha256": digest.hexdigest(), "canonical_history_changed": False}
