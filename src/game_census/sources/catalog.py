"""Versioned Steam catalog contract; the discovery v1 parser stays replayable."""
import json

from .base import Capture, SourceError, parse_json, validate_app_id
from .discovery import CATALOG, URLS
from .http import request

SOURCE = "steam_catalog_v2"
VERSION = "2"
HOST_GROUP = "webapi"
URL = URLS[CATALOG]
DOCUMENTATION_URL = "https://partner.steamgames.com/doc/webapi/IStoreService"


def invalid():
    return SourceError("invalid_catalog", "Steam returned an invalid or nonadvancing catalog page.",
                       "Inspect the catalog source contract and retry the same bounded scan; its checkpoint was not advanced.")


def parse(payload, unused_app_id=None):
    try:
        response = parse_json(payload)["response"]
        rows = response["apps"]
        more = response.get("have_more_results", False)
        if not isinstance(rows, list) or len(rows) > 50000 or type(more) is not bool or (more and not rows):
            raise invalid()
        items = []
        previous = 0
        for row in rows:
            app_id, name = row["appid"], row["name"]
            validate_app_id(app_id)
            if app_id <= previous or not isinstance(name, str) or len(name) > 500 or any(ord(c) < 32 for c in name):
                raise invalid()
            item = {"app_id": app_id, "name": name if name.strip() else f"Steam app {app_id}"}
            for field, maximum in (("last_modified", 4294967295), ("price_change_number", 18446744073709551615)):
                value = row.get(field)
                if value is not None and (type(value) is not int or not 0 <= value <= maximum):
                    raise invalid()
                item[field] = value
            items.append(item)
            previous = app_id
        cursor = response.get("last_appid", previous)
        if type(cursor) is not int or not previous <= cursor <= 4294967295:
            raise invalid()
        return {"items": items, "have_more_results": more, "last_appid": cursor}
    except (KeyError, TypeError, ValueError):
        raise invalid() from None


def fetch(client, parameters, max_bytes, key):
    if key is None or not key.get_secret_value():
        raise SourceError("catalog_key_required", "Catalog sync requires sources.catalog_api_key.",
                          "Configure a Steam Web API key before running catalog sync.")
    payload, started, received, status = request(client, URL,
        {"input_json": json.dumps(parameters, separators=(",", ":"))}, max_bytes,
        headers={"x-webapi-key": key.get_secret_value()})
    value = parse(payload)
    if (len(value["items"]) > parameters["max_results"]
            or any(item["app_id"] <= parameters["last_appid"] for item in value["items"])
            or (value["have_more_results"] and value["last_appid"] <= parameters["last_appid"])):
        raise invalid()
    return Capture(SOURCE, VERSION, None, started, received, status, parameters, payload, "raw_response", value)
