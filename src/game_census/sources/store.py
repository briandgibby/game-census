"""Optional observed Store endpoint: retain only the app identity and name."""
import json
import httpx
from .base import Capture, SourceError, parse_json, validate_app_id
from .http import request

SOURCE = "steam_store_name_v1"
VERSION = "1"
HOST_GROUP = "store"
URL = "https://store.steampowered.com/api/appdetails"


def parse(payload: bytes, app_id: int) -> str:
    data = parse_json(payload)
    if data.get("app_id") != app_id or type(data.get("app_id")) is not int:
        raise SourceError("invalid_store_identity", "Steam Store app identity did not match the requested app.",
                          "Inspect the Store source contract before enabling metadata.")
    name = data.get("name")
    if not isinstance(name, str) or not name.strip() or len(name) > 500 or any(ord(c) < 32 for c in name):
        raise SourceError("invalid_store_name", "Steam Store name was missing or invalid.",
                          "Inspect the Store source contract or disable sources.store_metadata_enabled.")
    return name


def parameters(app_id: int) -> dict:
    validate_app_id(app_id)
    return {"appids": app_id, "filters": "basic", "cc": "us", "l": "english"}


def fetch(client: httpx.Client, app_id: int, max_bytes: int) -> Capture:
    params = parameters(app_id)
    payload, started, received, status = request(client, URL, params, max_bytes)
    entry = parse_json(payload).get(str(app_id))
    if not isinstance(entry, dict) or entry.get("success") is not True or not isinstance(entry.get("data"), dict):
        raise SourceError("store_unavailable", "Steam Store did not return metadata for this app.",
                          "Check Store availability or disable sources.store_metadata_enabled.")
    data = entry["data"]
    retained = json.dumps({"app_id": data.get("steam_appid"), "name": data.get("name")},
                          ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    name = parse(retained, app_id)
    return Capture(SOURCE, VERSION, app_id, started, received, status, params,
                   retained, "allowlisted_name_projection", name)
