"""One bounded HTTPS request. Durable admission belongs to the collector/database."""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import time
import httpx
from .base import SourceError

ALLOWED_HOSTS = frozenset({"api.steampowered.com", "store.steampowered.com"})
_LIMITS = ContextVar("steam_request_limits", default=None)


@contextmanager
def request_limits(deadline: datetime, cancelled=None):
    """Bound an adapter call without making source adapters own scheduling.

    The scheduler also checks its durable fence when admitting and storing an
    attempt. This local check prevents dispatch after cancellation or deadline
    and rejects a response that completes outside the allowed window.
    """
    if deadline.tzinfo is None:
        raise ValueError("Request deadlines require a timezone-aware UTC timestamp.")
    remaining = (deadline - datetime.now(timezone.utc)).total_seconds()
    token = _LIMITS.set((time.monotonic() + remaining, cancelled))
    try:
        _check_limits()
        yield
    finally:
        _LIMITS.reset(token)


def _check_limits():
    limits = _LIMITS.get()
    if limits is None:
        return None
    until, cancelled = limits
    if cancelled is not None and cancelled():
        raise SourceError("collection_cancelled", "Collection was cancelled before the request could complete.",
                          "Inspect the scheduler report; the reserved request remains charged.")
    remaining = until - time.monotonic()
    if remaining <= 0:
        raise SourceError("source_deadline", "The collection deadline expired before the request could complete.",
                          "Inspect the scheduler report and wait for a new occurrence; the reserved request remains charged.")
    return remaining


def retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        seconds = int(value)
        # Bound untrusted delta-seconds to the representable UTC timestamp range.
        maximum = (datetime.max.replace(tzinfo=timezone.utc) - datetime.now(timezone.utc)).total_seconds()
        return max(0.0, float(seconds)) if seconds <= maximum else None
    except (ValueError, OverflowError):
        try:
            when = parsedate_to_datetime(value)
            if when.tzinfo is None:
                return None
            return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return None


def request(client: httpx.Client, url: str, params: dict, max_bytes: int, *, headers: dict | None = None) -> tuple[bytes, datetime, datetime, int]:
    parsed = httpx.URL(url)
    if parsed.scheme != "https" or parsed.host not in ALLOWED_HOSTS or parsed.port not in (None, 443) or parsed.userinfo:
        raise SourceError("source_not_allowed", "The source URL is outside the approved Steam hosts.",
                          "Use the shipped Steam source adapters.")
    started = datetime.now(timezone.utc)
    try:
        remaining = _check_limits()
        request_options = {}
        if remaining is not None:
            # Keep each network wait within both HTTP configuration and the
            # remaining occurrence window. Responses still face a final check.
            request_options["timeout"] = httpx.Timeout(**{
                name: min(value, remaining) if value is not None else remaining
                for name, value in client.timeout.as_dict().items()
            })
        with client.stream("GET", url, params=params, headers=headers, follow_redirects=False, **request_options) as response:
            _check_limits()
            if response.status_code != 200:
                raise SourceError("http_error", f"Steam returned HTTP {response.status_code}.",
                                  "Retry a bounded collection after Steam access recovers.",
                                  http_status=response.status_code,
                                  retryable=response.status_code in (429, 500, 502, 503, 504),
                                  retry_after_seconds=retry_after(response.headers.get("Retry-After")))
            payload = bytearray()
            for chunk in response.iter_bytes():
                _check_limits()
                if len(payload) + len(chunk) > max_bytes:
                    raise SourceError("response_too_large", "Steam response exceeded http.max_response_bytes.",
                                      "Inspect the source contract and configured response-size bound.")
                payload.extend(chunk)
            _check_limits()
            return bytes(payload), started, datetime.now(timezone.utc), response.status_code
    except httpx.TimeoutException:
        raise SourceError("timeout", "Steam request timed out.",
                          "Check network connectivity and retry a bounded collection.", retryable=True) from None
    except httpx.RequestError:
        raise SourceError("network_error", "Steam request could not complete.",
                          "Check network connectivity and retry a bounded collection.", retryable=True) from None
