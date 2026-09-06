"""Official current concurrent-player source and its versioned parser."""
import httpx
from .base import Capture, SourceError, parse_json, validate_app_id
from .http import request

SOURCE = "steam_current_players_v1"
VERSION = "1"
HOST_GROUP = "webapi"
URL = "https://api.steampowered.com/ISteamUserStats/GetNumberOfCurrentPlayers/v1/"
DOCUMENTATION_URL = "https://partner.steamgames.com/doc/webapi/ISteamUserStats#GetNumberOfCurrentPlayers"


def parse(payload: bytes, app_id: int) -> int:
    validate_app_id(app_id)
    response = parse_json(payload).get("response")
    if not isinstance(response, dict) or type(response.get("result")) is not int or response["result"] != 1:
        raise SourceError("provider_result", "Steam did not report a successful player-count result.",
                          "Check that the app supports player counts and retry a bounded collection.")
    count = response.get("player_count")
    if type(count) is not int or not 0 <= count <= 9223372036854775807:
        raise SourceError("invalid_player_count", "Steam player_count is missing or is not a valid nonnegative integer.",
                          "Inspect the player-count source contract before collecting again.")
    return count


def parameters(app_id: int) -> dict:
    validate_app_id(app_id)
    return {"appid": app_id}


def fetch(client: httpx.Client, app_id: int, max_bytes: int) -> Capture:
    params = parameters(app_id)
    payload, started, received, status = request(client, URL, params, max_bytes)
    return Capture(SOURCE, VERSION, app_id, started, received, status, params,
                   payload, "exact_response", parse(payload, app_id))
