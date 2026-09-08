"""Versioned, query-qualified enrichment captures. Legacy parsers remain registered.

Reviews retain aggregate fields only; news retains linked metadata only. The
allowlist runs before persistence, including before a capture enters run reports.
"""
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re

from .base import Capture, SourceError, parse_json, validate_app_id
from .details import plain, safe_url, store_value
from .http import request

STORE = "steam_store_details_v2"
REVIEWS = "steam_review_summary_v2"
ACHIEVEMENTS = "steam_global_achievements_v1"
SCHEMA = "steam_achievement_schema_v1"
NEWS = "steam_app_news_v2"


def invalid():
    return SourceError("invalid_enrichment", "Steam returned an invalid enrichment response.",
                       "Inspect source status and the versioned contract; retain the last valid observation and retry a bounded run.")


def integer(value, maximum=4294967295):
    if type(value) is not int or not 0 <= value <= maximum:
        raise invalid()
    return value


def label(value, maximum=1000):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise invalid()
    return plain(value)


def identity(parameters):
    return hashlib.sha256(json.dumps(parameters, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def review_summary(body):
    summary = body.get("query_summary")
    if type(body.get("success")) is not int or body["success"] != 1 or not isinstance(summary, dict):
        raise invalid()
    result = {key: integer(summary.get(key)) for key in ("total_positive", "total_negative", "total_reviews")}
    if result["total_positive"] + result["total_negative"] != result["total_reviews"]:
        raise invalid()
    result["review_score"] = integer(summary.get("review_score"), 10)
    result["review_score_desc"] = label(summary.get("review_score_desc"))
    return {"success": 1, "query_summary": result}


def news_metadata(body, app_id):
    news = body.get("appnews")
    if not isinstance(news, dict) or type(news.get("appid")) is not int or news["appid"] != app_id or not isinstance(news.get("newsitems"), list):
        raise invalid()
    articles, seen = [], set()
    for row in news["newsitems"]:
        if not isinstance(row, dict):
            raise invalid()
        gid = str(row.get("gid", ""))
        if not re.fullmatch(r"[0-9]{1,20}", gid) or gid in seen or row.get("feedname") != "steam_community_announcements":
            raise invalid()
        seen.add(gid)
        articles.append({"gid": gid, "title": label(row.get("title")),
                         "date": integer(row.get("date"), 253402300799),
                         "feedname": "steam_community_announcements"})
    return {"appnews": {"appid": app_id, "newsitems": articles}}


class Adapter:
    def __init__(self, kind, source, version, group, url, policy=None, key=None):
        self.kind, self.SOURCE, self.VERSION = kind, source, version
        self.HOST_GROUP, self.URL = group, url
        self.policy, self.key = policy, key

    @property
    def interval_seconds(self):
        return self.policy.interval_seconds

    def parameters(self, app_id):
        validate_app_id(app_id)
        from ..config import Enrichment
        policy = self.policy or getattr(Enrichment(), self.kind)
        if self.kind == "store":
            return {"appids": app_id, "cc": policy.country, "l": policy.language}
        if self.kind == "reviews":
            return {"requested_app_id": app_id, "json": 1, "language": policy.language,
                    "filter": policy.filter, "day_range": policy.day_range,
                    "review_type": policy.review_type, "purchase_type": policy.purchase_type,
                    "filter_offtopic_activity": 0 if policy.include_offtopic else 1,
                    "num_per_page": 1, "cursor": "*"}
        if self.kind == "achievements":
            return {"gameid": app_id}
        if self.kind == "achievement_schema":
            return {"appid": app_id, "l": policy.language}
        return {"appid": app_id, "count": policy.count, "maxlength": 1, "feeds": "steam_community_announcements"}

    def parse(self, payload, app_id=None):
        envelope = parse_json(payload)
        requested = envelope.get("app_id")
        validate_app_id(requested)
        if app_id is not None and requested != app_id:
            raise invalid()
        params, body = envelope.get("parameters"), envelope.get("body")
        if not isinstance(params, dict) or not isinstance(body, dict):
            raise invalid()
        # Validate retained query policy independently of the current configuration.
        from ..config import StorePolicy, ReviewPolicy, SchemaPolicy, NewsPolicy
        from pydantic import ValidationError
        try:
            if self.kind == "store":
                policy = StorePolicy(country=params["cc"], language=params["l"])
            elif self.kind == "reviews":
                if type(params["filter_offtopic_activity"]) is not int or params["filter_offtopic_activity"] not in (0, 1):
                    raise invalid()
                policy = ReviewPolicy(**{key: params[key] for key in ("language", "filter", "day_range", "review_type", "purchase_type")},
                                      include_offtopic=params["filter_offtopic_activity"] == 0)
            elif self.kind == "achievement_schema":
                policy = SchemaPolicy(language=params["l"])
            elif self.kind == "news":
                policy = NewsPolicy(count=params["count"])
            else:
                policy = None
            bound = Adapter(self.kind, self.SOURCE, self.VERSION, self.HOST_GROUP, self.URL, policy)
            if params != bound.parameters(requested):
                raise invalid()
        except (KeyError, ValidationError):
            raise invalid() from None
        result = {"items": [], "app_id": requested, "kind": self.kind, "state": "available", "query": params,
                  "query_hash": identity(params)}
        if self.kind == "store":
            entry = body.get(str(requested))
            if not isinstance(entry, dict) or type(entry.get("success")) is not bool:
                raise invalid()
            result.update(country=params["cc"], product=f"app:{requested}", currency=None, price=None)
            if not entry["success"]:
                result["state"] = "unavailable"
                return result
            metadata = store_value(body, requested)
            data = entry["data"]
            price = data.get("price_overview")
            if price is not None:
                if not isinstance(price, dict) or not re.fullmatch(r"[A-Z]{3}", str(price.get("currency", ""))):
                    raise invalid()
                for key in ("initial", "final"):
                    integer(price.get(key))
                integer(price.get("discount_percent"), 100)
                if price["final"] > price["initial"]:
                    raise invalid()
                result.update(state="priced", price=metadata["price"], currency=price["currency"])
            elif metadata["is_free"] is True:
                result["state"] = "free"
            else:
                result["state"] = "unknown"
            result["metadata"] = metadata
            result["items"] = metadata["items"]
        elif self.kind == "reviews":
            summary = review_summary(body)["query_summary"]
            result.update(summary)
            result["positive_percent"] = round(summary["total_positive"] * 100 / summary["total_reviews"], 2) if summary["total_reviews"] else None
        elif self.kind == "news":
            news = news_metadata(body, requested)["appnews"]["newsitems"]
            if len(news) > params["count"]:
                raise invalid()
            result["articles"] = [{**row, "url": f"https://store.steampowered.com/news/app/{requested}/view/{row['gid']}"} for row in news]
        elif self.kind == "achievements":
            outer = body.get("achievementpercentages")
            if not isinstance(outer, dict) or not isinstance(outer.get("achievements"), list):
                raise invalid()
            values, seen = [], set()
            for row in outer["achievements"]:
                if not isinstance(row, dict):
                    raise invalid()
                name = label(row.get("name"), 256)
                try:
                    percent = Decimal(str(row.get("percent")))
                except InvalidOperation:
                    raise invalid() from None
                if not percent.is_finite() or not 0 <= percent <= 100 or name in seen:
                    raise invalid()
                seen.add(name)
                values.append({"name": name, "percent": str(percent)})
            result["achievements"] = values
            result["state"] = "available" if values else "unsupported"
        else:
            game = body.get("game")
            if not isinstance(game, dict) or not isinstance(game.get("availableGameStats"), dict):
                raise invalid()
            rows = game["availableGameStats"].get("achievements")
            if rows is None:
                result.update(state="unsupported", achievements=[])
                return result
            if not isinstance(rows, list):
                raise invalid()
            values, seen = [], set()
            for row in rows:
                if not isinstance(row, dict):
                    raise invalid()
                name = label(row.get("name"), 256)
                if name in seen:
                    raise invalid()
                seen.add(name)
                values.append({"name": name, "display_name": label(row.get("displayName")),
                               "description": plain(row.get("description")), "hidden": integer(row.get("hidden"), 1),
                               "icon": safe_url(row.get("icon"), media=True), "icon_gray": safe_url(row.get("icongray"), media=True)})
            result.update(achievements=values, state="available" if values else "unsupported")
        return result

    def fetch(self, client, app_id, max_bytes):
        params = self.parameters(app_id)
        headers = None
        if self.kind == "achievement_schema":
            if self.key is None or not self.key.get_secret_value().strip():
                raise SourceError("missing_schema_key", "sources.catalog_api_key is required for achievement schema.",
                                  "Provide the Steam Web API key through local configuration and retry this bounded source.")
            headers = {"x-webapi-key": self.key.get_secret_value()}
        payload, started, received, status = request(client, self.URL.format(app_id=app_id),
            {k: v for k, v in params.items() if k != "requested_app_id"}, max_bytes, headers=headers)
        body = parse_json(payload)
        if self.kind == "reviews":
            body = review_summary(body)
        elif self.kind == "news":
            body = news_metadata(body, app_id)
        retained = json.dumps({"app_id": app_id, "parameters": params, "body": body}, ensure_ascii=False, separators=(",", ":")).encode()
        return Capture(self.SOURCE, self.VERSION, app_id, started, received, status, params, retained,
                       "query_qualified_allowlist" if self.kind in ("reviews", "news") else "query_qualified_response", self.parse(retained, app_id))


ADAPTERS = {a.SOURCE: a for a in (
    Adapter("store", STORE, "2", "store", "https://store.steampowered.com/api/appdetails"),
    Adapter("reviews", REVIEWS, "2", "store", "https://store.steampowered.com/appreviews/{app_id}"),
    Adapter("achievements", ACHIEVEMENTS, "1", "webapi", "https://api.steampowered.com/ISteamUserStats/GetGlobalAchievementPercentagesForApp/v2/"),
    Adapter("achievement_schema", SCHEMA, "1", "webapi", "https://api.steampowered.com/ISteamUserStats/GetSchemaForGame/v2/"),
    Adapter("news", NEWS, "2", "webapi", "https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/"),
)}
KINDS = {a.kind: a.SOURCE for a in ADAPTERS.values()}


def configured(settings, kinds=None):
    selected = kinds if kinds is not None else [kind for kind in KINDS if getattr(settings.enrichment, kind).enabled]
    return [Adapter(a.kind, a.SOURCE, a.VERSION, a.HOST_GROUP, a.URL, getattr(settings.enrichment, a.kind), settings.sources.catalog_api_key)
            for a in (ADAPTERS[KINDS[kind]] for kind in selected)]
