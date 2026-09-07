"""Stored GET views with explicit, bounded same-origin Steam refresh actions."""

from __future__ import annotations

import logging
import math
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlencode, urlsplit

from fastapi import FastAPI, HTTPException, Path as ApiPath, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

from .contracts import AppList, AppSummary, Comparison, PlayerHistory, PublicStatus, Rankings
from . import queries
from .db import QueryLimitError
from .sources.players import DOCUMENTATION_URL as SOURCE_URL, SOURCE as SOURCE_ID
from .sources.details import STORE, REVIEWS, NEWS, CURRENT, MEDIA_HOSTS

logger = logging.getLogger(__name__)
PACKAGE = Path(__file__).parent
APP_ID = Annotated[int, ApiPath(ge=1, le=4294967295)]


def _utc(value: datetime | str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _number(value: int | float | None) -> str:
    if value is None:
        return "—"
    return f"{value:,.0f}"


def _timestamp(value: datetime | str | None) -> str:
    return _utc(value).strftime("%d %b %Y, %H:%M:%S UTC") if value else "Not yet observed"


def _news_excerpt(value: str) -> str:
    """Display Steam announcement markup as text without altering captures."""
    value = re.sub(r"\[img\].*?\[/img\]", "", value, flags=re.IGNORECASE | re.DOTALL)
    value = re.sub(r"\{STEAM_CLAN_IMAGE\}[^\s\[]*", "", value)
    value = re.sub(r"\[/?(?:url|b|i|u|h[1-6]|list|olist|quote|code|spoiler|img|previewyoutube|table|tr|td|th)(?:=[^\]]*)?\]|\[\*\]", "", value, flags=re.IGNORECASE)
    return value.strip()


def _chart(history: dict, interval: int, gap_multiplier: float, maximum: int | None = None) -> dict:
    """Only draw observed samples; never bridge a cadence-capped collection gap."""
    points = history["points"]
    start, end = _utc(history["from"]), _utc(history["to"])
    seconds = max((end - start).total_seconds(), 1)
    width, height, left, top = 880, 260, 64, 24
    plot_width, plot_height = width - left - 24, height - top - 40
    maximum = max(max((point["player_count"] for point in points), default=0), maximum or 0)
    ceiling = max(4, math.ceil(maximum * 1.12))
    plotted, paths, segment = [], [], []
    previous_time = None
    for point in points:
        at = _utc(point["observed_at"])
        x = left + max(0, min(1, (at - start).total_seconds() / seconds)) * plot_width
        y = top + (1 - point["player_count"] / ceiling) * plot_height
        crosses_recorded_gap = previous_time is not None and any(
            _utc(gap["from"]) < at and _utc(gap["to"]) > previous_time for gap in history.get("gaps", [])
        )
        if previous_time is not None and (crosses_recorded_gap or (at - previous_time).total_seconds() > interval * gap_multiplier):
            paths.append(" ".join(segment))
            segment = []
        segment.append(f"{x:.2f},{y:.2f}")
        plotted.append({"x": round(x, 2), "y": round(y, 2), "count": point["player_count"], "at": _timestamp(at)})
        previous_time = at
    if segment:
        paths.append(" ".join(segment))
    ticks = [{"y": top + plot_height * index / 4, "label": _number(ceiling * (1 - index / 4))} for index in range(5)]
    return {"paths": paths, "points": plotted, "ticks": ticks, "start_label": start.strftime("%d %b · %H:%M"),
            "end_label": end.strftime("%d %b · %H:%M UTC"), "width": width, "height": height}


def create_app(settings: Any, db: Any = None) -> FastAPI:
    """Create stored views and bounded refresh routes; storage setup stays in the CLI."""
    if db is None:
        from .db import Database

        db = Database(settings.storage.database_url.get_secret_value())
    app = FastAPI(title="Game Census read API", version="1.0.0", docs_url=None, redoc_url=None)
    app.state.db = db
    app.state.settings = settings
    app.mount("/static", StaticFiles(directory=str(PACKAGE / "static")), name="static")
    templates = Jinja2Templates(directory=str(PACKAGE / "templates"))
    templates.env.filters["number"] = _number
    templates.env.filters["timestamp"] = _timestamp
    templates.env.filters["news_excerpt"] = _news_excerpt
    templates.env.filters["unixdate"] = lambda value: datetime.fromtimestamp(value, timezone.utc).strftime("%d %b %Y")
    maximum_hours = settings.web.max_history_days * 24

    def render(request: Request, name: str, context: dict | None = None, status_code: int = 200):
        return templates.TemplateResponse(request=request, name=name, status_code=status_code, context={
            "nav": "activity", "source_url": SOURCE_URL, "generated_at": datetime.now(timezone.utc),
            "refresh_seconds": settings.web.refresh_seconds, **(context or {}),
        })

    def read(operation: str, *args, **kwargs):
        try:
            return (operation if callable(operation) else getattr(db, operation))(*args, **kwargs)
        except queries.UnknownAppError as exc:
            raise HTTPException(404, {"code": "app_unknown", "message": str(exc)}) from None
        except QueryLimitError as exc:
            raise HTTPException(422, {"code": "history_point_limit", "message": str(exc)}) from None
        except Exception as exc:
            incident = uuid.uuid4().hex[:12]
            # Exception text may contain a database URL or other credentials.
            logger.error("read_service_failed operation=%s error_type=%s incident=%s", operation, type(exc).__name__, incident)
            raise HTTPException(503, {"code": "storage_unavailable", "message": "Stored data could not be read. Check database availability and run the status command.", "incident": incident}) from None

    def detail(app_id: int) -> dict:
        return read(queries.app_summary, settings, db, app_id)

    def listing(q: str = "") -> list[dict]:
        apps = [AppSummary.model_validate(item).model_dump() for item in read("list_apps", settings)]
        query = q.strip().casefold()
        return sorted([item for item in apps if not query or query in item["name"].casefold() or query in str(item["app_id"])],
                      key=lambda item: (item["availability"] != "fresh", -(item["player_count"] or 0), item["app_id"]))

    def history_for(app_id: int, hours: int, resolution: str = "raw") -> dict:
        result = (read("history", app_id, settings, hours=hours) if resolution == "raw"
                  else read("history", app_id, settings, hours=hours, resolution=resolution))
        if result is None:
            raise HTTPException(404, {"code": "app_not_tracked", "message": f"App {app_id} is not tracked by this instance."})
        if len(result["points"]) > settings.web.max_points:
            raise HTTPException(422, {"code": "history_point_limit", "message": f"This window exceeds web.max_points ({settings.web.max_points}). Choose a shorter history window."})
        return PlayerHistory.model_validate(result).model_dump(by_alias=True)

    def status_data() -> dict:
        from .cohort import effective, public_status
        membership = public_status(settings, db)
        state = read("status")
        schedule_state = state["scheduler"]
        if schedule_state == "enabled":
            from .scheduler import plan
            if not membership["policy_matches"] or state["scheduler_state"]["plan_hash"] != plan(effective(settings, db))["plan_hash"]:
                schedule_state = "plan_changed"
        apps = listing()
        ids = {row["app_id"] for row in membership["members"]}
        active = [row for row in apps if row["app_id"] in ids]
        run = read("last_run")
        # Restrict operational output to safe counts and outcomes, never arbitrary config.
        safe_run = None if run is None else {key: run[key] for key in ("run_id", "status", "started_at", "finished_at", "completed_at", "app_ids", "attempted", "succeeded", "failed") if key in run}
        return PublicStatus(generated_at=datetime.now(timezone.utc), tracked_apps=len(ids), retained_apps=len(apps),
                            fresh_apps=sum(item["availability"] == "fresh" for item in active),
                            stale_apps=sum(item["availability"] == "stale" for item in active),
                            apps_without_observations=sum(item["player_count"] is None for item in active) + len(ids-{item["app_id"] for item in active}),
                            total_observations=sum(item["sample_count"] for item in apps), source=SOURCE_ID,
                            collection_mode="scheduled" if schedule_state == "enabled" else "manual",
                            schedule_state=schedule_state, last_run=safe_run, cohort=membership).model_dump()

    def page_data(item: dict, hours: int) -> dict:
        history = history_for(item["app_id"], hours, resolution="auto")
        return {"game": item, "history": history, "hours": hours,
                "windows": [(value, label) for value, label in [(1, "1H"), (24, "24H"), (168, "7D"), (720, "30D")] if value <= maximum_hours],
                "chart": _chart(history, item["expected_interval_seconds"], settings.metrics.gap_cap_multiplier)}

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        try:
            response = await call_next(request)
        except Exception as exc:
            incident = uuid.uuid4().hex[:12]
            logger.error("read_contract_failure error_type=%s incident=%s", type(exc).__name__, incident)
            problem = {"code": "read_contract_failure", "message": "Stored data could not be presented. Run the status command and inspect the read service using this report reference.", "incident": incident}
            response = (JSONResponse(status_code=503, content={"error": problem})
                        if request.url.path.startswith(("/api/", "/health/", "/openapi.json"))
                        else render(request, "error.html", {"status_code": 503, "problem": problem}, 503))
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: " + " ".join("https://" + host for host in sorted(MEDIA_HOSTS)) + "; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store" if not request.url.path.startswith("/static/") else "public, max-age=3600"
        return response

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        problem = exc.detail if isinstance(exc.detail, dict) else {"code": "request_failed", "message": str(exc.detail)}
        if request.url.path.startswith(("/api/", "/health/")):
            return JSONResponse(status_code=exc.status_code, content={"error": problem})
        return render(request, "error.html", {"status_code": exc.status_code, "problem": problem}, exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def invalid_input(request: Request, exc: RequestValidationError):
        problem = {"code": "invalid_request", "message": "A request value is outside the allowed bounds.",
                   "fields": [".".join(str(part) for part in item["loc"]) for item in exc.errors()]}
        if request.url.path.startswith(("/api/", "/health/")):
            return JSONResponse(status_code=422, content={"error": problem})
        return render(request, "error.html", {"status_code": 422, "problem": problem}, 422)

    @app.get("/health/live", tags=["Health"])
    def live():
        return {"status": "alive"}

    @app.get("/health/ready", tags=["Health"])
    def ready():
        read("status")
        return {"status": "ready"}

    @app.get("/api/v1/apps", response_model=AppList, tags=["Players"])
    def api_apps(q: Annotated[str, Query(max_length=100)] = "", scope: Literal["enrolled", "catalog"] = "enrolled",
                 page: int | None = Query(default=None, ge=1, le=4294967295),
                 page_size: int | None = Query(default=None, ge=1, le=settings.web.max_page_size)):
        if scope == "catalog":
            return read(queries.catalog_search, settings, db, q, page or 1, page_size or min(25, settings.web.max_page_size))
        if page is not None or page_size is not None:
            raise HTTPException(422, "Use scope=catalog for paginated catalog/search results.")
        items = listing(q)
        return {"items": items, "total": len(items), "tracking_scope": "enrolled", "generated_at": datetime.now(timezone.utc)}

    @app.get("/api/v1/apps/{app_id}", response_model=AppSummary, tags=["Players"])
    @app.get("/api/v1/apps/{app_id}/players", response_model=AppSummary, tags=["Players"])
    def api_app(app_id: APP_ID):
        return detail(app_id)

    @app.get("/api/v1/apps/{app_id}/history", response_model=PlayerHistory, tags=["Players"])
    def api_history(app_id: APP_ID, hours: int | None = Query(default=None, ge=1, le=maximum_hours),
                    resolution: Literal["raw", "auto"] = "raw", from_time: datetime | None = Query(default=None, alias="from"),
                    to: datetime | None = None):
        if from_time is None and to is None:
            return history_for(app_id, 24 if hours is None else hours, resolution=resolution)
        start, end = read(queries.window, settings, hours=hours, from_time=from_time, to=to)
        result = read("history_range", app_id, settings, start, end, resolution)
        if result is None:
            raise HTTPException(404, {"code": "app_not_tracked", "message": "This app has no tracked history in this instance."})
        if len(result["points"]) > settings.web.max_points:
            raise HTTPException(422, {"code": "history_point_limit", "message": "This window exceeds web.max_points. Use auto resolution or a smaller window."})
        return PlayerHistory.model_validate(result).model_dump(by_alias=True)

    def selection(values):
        if not values or len(values) > 10 or sum(len(value) for value in values) > 110:
            raise HTTPException(422, {"code": "invalid_comparison", "message": "Select a bounded list of Steam app IDs."})
        return ",".join(values)

    @app.get("/api/v1/rankings", response_model=Rankings, tags=["Players"])
    def api_rankings():
        return read(queries.rankings, settings, db)

    @app.get("/api/v1/compare", response_model=Comparison, tags=["Players"])
    def api_compare(app_ids: Annotated[list[str] | None, Query()] = None,
                    hours: int | None = Query(default=None, ge=1, le=maximum_hours),
                    from_time: datetime | None = Query(default=None, alias="from"), to: datetime | None = None,
                    resolution: Literal["raw", "auto"] = "auto"):
        return read(queries.compare, settings, db, selection(app_ids), hours=hours, from_time=from_time, to=to, resolution=resolution)

    @app.get("/rankings", response_class=HTMLResponse, include_in_schema=False)
    def rankings_page(request: Request):
        return render(request, "rankings.html", {"nav": "rankings", "rankings": api_rankings()})

    @app.get("/compare", response_class=HTMLResponse, include_in_schema=False)
    def comparison_page(request: Request, app_ids: Annotated[list[str] | None, Query()] = None,
                        hours: int | None = Query(default=None, ge=1, le=maximum_hours),
                        from_time: datetime | None = Query(default=None, alias="from"), to: datetime | None = None):
        result = api_compare(app_ids, hours, from_time, to) if app_ids is not None else None
        selected = [row["app_id"] for row in result["series"]] if result else []
        candidates = {row["app_id"]: {"app_id": row["app_id"], "name": row["name"]} for row in listing()}
        for row in result["series"] if result else []:
            candidates.setdefault(row["app_id"], {"app_id": row["app_id"], "name": row["name"]})
        compared = []
        elapsed = (result["to"]-result["from"]).total_seconds()/3600 if result else (hours or 24)
        elapsed = int(elapsed) if elapsed == int(elapsed) else elapsed
        maximum = max((point["player_count"] for row in result["series"] if row["history"] for point in row["history"]["points"]), default=0) if result else 0
        for row in result["series"] if result else []:
            chart = _chart(row["history"], row["expected_interval_seconds"], settings.metrics.gap_cap_multiplier, maximum) if row["history"] else None
            history_url = "/api/v1/apps/" + str(row["app_id"]) + "/history?" + urlencode({"from": result["from"].isoformat(), "to": result["to"].isoformat(), "resolution": "auto"})
            compared.append({"game": row, "history": row["history"], "chart": chart, "history_url": history_url})
        api_url = "/api/v1/compare?" + urlencode({"app_ids": ",".join(map(str, selected)), "from": result["from"].isoformat(), "to": result["to"].isoformat()}) if result else None
        return render(request, "compare.html", {"nav": "compare", "comparison": result, "compared": compared,
            "candidates": list(candidates.values()), "selected": selected, "max_compare_apps": settings.web.max_compare_apps,
            "hours": elapsed, "max_hours": maximum_hours, "api_url": api_url, "windows": [],
            "comparison_app_ids": ",".join(map(str, selected))})

    @app.get("/api/v1/status", response_model=PublicStatus, tags=["Health"])
    def api_status():
        return status_data()

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def home(request: Request, q: Annotated[str, Query(max_length=100)] = "", page: int = Query(default=1, ge=1, le=4294967295)):
        if q or page != 1:
            return RedirectResponse("/search?" + urlencode({"q": q, "page": page}), status_code=303)
        apps = listing()
        return render(request, "index.html", {"apps": apps, "cohort_count": len(apps), "q": q,
                      "fresh_count": sum(item["availability"] == "fresh" for item in apps),
                      "rankings": api_rankings(),
                      "dashboard": read("dashboard")})

    @app.get("/search", response_class=HTMLResponse, include_in_schema=False)
    def search_page(request: Request, q: Annotated[str, Query(max_length=100)] = "", page: int = Query(default=1, ge=1, le=4294967295)):
        q = q.strip()
        return render(request, "search.html", {"nav": "search", "q": q,
                      "catalog": read("catalog", q, page, 24) if q else None,
                      "previous_url": "/search?" + urlencode({"q": q, "page": max(1, page-1)}),
                      "next_url": "/search?" + urlencode({"q": q, "page": page+1}),
                      "search_action": "/discovery/search?" + urlencode({"q": q, "return_page": page})})

    @app.get("/api/v1/catalog", tags=["Discovery"])
    def api_catalog(q: Annotated[str, Query(max_length=100)] = "", page: int = Query(default=1, ge=1, le=4294967295),
                    page_size: int = Query(default=min(25, settings.web.max_page_size), ge=1, le=settings.web.max_page_size)):
        return read("catalog", q, page, page_size)

    @app.get("/api/v1/dashboard", tags=["Discovery"])
    def api_dashboard():
        return read("dashboard")

    def same_origin(request):
        origin = request.headers.get("origin")
        referer = request.headers.get("referer")
        expected = urlsplit(str(request.base_url))
        supplied = urlsplit(origin or referer or "")
        if (supplied.scheme, supplied.netloc) != (expected.scheme, expected.netloc) or request.headers.get("sec-fetch-site") == "cross-site":
            raise HTTPException(403, "Steam collection requires a same-origin form submission.")

    def discovery_mutation(request, operation, redirect_url="/", **kwargs):
        from .collector import collect_discovery
        from .db import DatabaseError
        from .sources import SourceError
        same_origin(request)
        try:
            report = collect_discovery(settings, db, operation, **kwargs)
        except (DatabaseError, SourceError) as error:
            raise HTTPException(503, str(error)) from None
        if report["status"] != "succeeded":
            errors = [item["error"]["message"] for item in report["sources"] if item["status"] == "failed"]
            raise HTTPException(503, " ".join(errors) + " Last successful snapshots remain available on the dashboard.")
        return RedirectResponse(redirect_url, status_code=303)

    @app.post("/discovery/search", include_in_schema=False)
    def search_steam(request: Request, q: Annotated[str, Query(min_length=1, max_length=100)],
                     page: int = Query(default=1, ge=1, le=100),
                     return_page: int | None = Query(default=None, ge=1, le=4294967295)):
        return discovery_mutation(request, "search", redirect_url="/search?" + urlencode({"q": q, "page": return_page or page}), query=q, page=page)

    @app.post("/discovery/charts", include_in_schema=False)
    def refresh_charts(request: Request):
        return discovery_mutation(request, "charts")

    @app.get("/apps/{app_id}", response_class=HTMLResponse, include_in_schema=False)
    def game_page(request: Request, app_id: APP_ID, hours: int = Query(default=24, ge=1, le=maximum_hours), refreshed: str = ""):
        tracked = read("app_detail", app_id, settings)
        discovered = read("discovered_app", app_id)
        if tracked is None and discovered is None:
            raise HTTPException(404, "This Steam app is not yet known here. Search Steam from the dashboard.")
        details = read("game_details", app_id)
        snapshots = details["snapshots"]
        metadata = snapshots.get(STORE)
        context = {"tracked": bool(tracked), "details": details, "metadata": metadata,
                   "reviews": snapshots.get(REVIEWS), "news": snapshots.get(NEWS),
                   "current": snapshots.get(CURRENT), "charts": discovered.get("charts", []) if discovered else [],
                   "refresh_result": refreshed if refreshed in ("succeeded", "partial", "failed") else ""}
        if tracked:
            context.update(page_data(AppSummary.model_validate(tracked).model_dump(), hours))
            if context["current"] and tracked.get("observed_at") and _utc(tracked["observed_at"]) > _utc(context["current"]["observed_at"]):
                context["current"] = None
        else:
            context["game"] = discovered
        if metadata:
            context["game"]["name"] = metadata["name"]
        for field, key in (("profile_peak", "observed_24h_peak"), ("profile_highest", "highest_recorded")):
            values = [value for value in (details[key], tracked.get(key) if tracked else None) if value is not None]
            context[field] = max(values) if values else None
        return render(request, "game.html", context)

    @app.post("/apps/{app_id}/refresh", include_in_schema=False)
    def refresh_details(request: Request, app_id: APP_ID, automatic: bool = False):
        from .collector import collect_discovery
        from .db import DatabaseError
        from .sources import SourceError
        same_origin(request)
        if read("app_detail", app_id, settings) is None and read("discovered_app", app_id) is None:
            raise HTTPException(404, "This Steam app is not yet known here.")
        if automatic:
            raise HTTPException(422, "Automatic Steam collection is disabled. Use the explicit Refresh details action.")
        try:
            report = collect_discovery(settings, db, "details", app_id=app_id)
        except (DatabaseError, SourceError) as error:
            raise HTTPException(503, str(error)) from None
        return RedirectResponse(f"/apps/{app_id}?refreshed={report['status']}", status_code=303)

    @app.get("/api/v1/apps/{app_id}/details", tags=["Discovery"])
    def api_game_details(app_id: APP_ID):
        if read("app_detail", app_id, settings) is None and read("discovered_app", app_id) is None:
            raise HTTPException(404, "This Steam app is not yet known here.")
        return read("game_details", app_id)

    @app.get("/methodology", response_class=HTMLResponse, include_in_schema=False)
    def methodology(request: Request):
        return render(request, "methodology.html", {"nav": "methodology", "freshness_multiplier": settings.metrics.freshness_interval_multiplier,
                                                   "gap_multiplier": settings.metrics.gap_cap_multiplier, "max_points": settings.web.max_points,
                                                   "max_history_days": settings.web.max_history_days,
                                                   "minimum_coverage_ratio": settings.metrics.min_coverage_ratio})

    @app.get("/status", response_class=HTMLResponse, include_in_schema=False)
    def status_page(request: Request):
        return render(request, "status.html", {"nav": "status", "status": status_data(), "apps": listing(), "dashboard": read("dashboard")})

    return app
