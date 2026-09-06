"""Offline source acceptance: validate zero, failures, limits, and retained fields."""
import json
import httpx
import pytest
from game_census.sources import SourceError, players, store
from game_census.sources.http import request, retry_after
from game_census.collector import collect_once
from game_census.config import Settings


def player_fetch(payload, status=200):
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(status,json=payload))) as client:
        return players.fetch(client,570,2000000)


def test_valid_zero_is_retained_with_exact_payload_and_provenance():
    capture = player_fetch({"response": {"result": 1, "player_count": 0}})
    assert capture.value == 0
    assert json.loads(capture.payload)["response"]["player_count"] == 0
    assert capture.source == players.SOURCE
    assert capture.parameters == {"appid": 570}
    assert capture.received_at >= capture.request_started_at
    assert len(capture.checksum) == 64


@pytest.mark.parametrize("count", [None,-1,True,"12",1.2,9223372036854775808])
def test_invalid_counts_are_errors_never_zero(count):
    with pytest.raises(SourceError, match="player_count"):
        player_fetch({"response": {"result":1,"player_count":count}})


@pytest.mark.parametrize("payload", [{}, {"response":{}}, {"response":{"result":False,"player_count":9}}, {"response":{"result":2,"player_count":0}}])
def test_missing_or_unsuccessful_provider_result_is_error(payload):
    with pytest.raises(SourceError) as caught:
        player_fetch(payload)
    assert caught.value.code == "provider_result"


def test_http_429_preserves_retry_after_without_sensitive_body():
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(429,headers={"Retry-After":"90"},text="secret_token"))) as client:
        with pytest.raises(SourceError) as caught:
            players.fetch(client,570,1024)
    assert caught.value.retryable
    assert caught.value.retry_after_seconds == 90
    assert "secret_token" not in json.dumps(caught.value.as_dict())


def test_timeout_is_safe_and_retryable():
    def timeout(req):
        raise httpx.ReadTimeout("secret query credential",request=req)
    with httpx.Client(transport=httpx.MockTransport(timeout)) as client:
        with pytest.raises(SourceError) as caught:
            players.fetch(client,570,1024)
    assert caught.value.code == "timeout"
    assert "credential" not in str(caught.value)


def test_malformed_and_oversized_responses_fail():
    for payload,limit,code in [(b"{",1024,"invalid_json"),(b"x"*1025,1024,"response_too_large")]:
        with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200,content=payload))) as client:
            with pytest.raises(SourceError) as caught:
                players.fetch(client,570,limit)
        assert caught.value.code == code


@pytest.mark.parametrize("destination", ["https://example.com/secret", "https://steamdb.info/app/570/"])
def test_redirects_are_not_followed(destination):
    urls = []
    def redirected(req):
        urls.append(str(req.url))
        return httpx.Response(302,headers={"Location":destination})
    with httpx.Client(transport=httpx.MockTransport(redirected)) as client:
        with pytest.raises(SourceError):
            players.fetch(client,570,1024)
    assert len(urls) == 1


@pytest.mark.parametrize("url", [
    "http://api.steampowered.com/x", "https://example.com/x",
    "https://user:secret@api.steampowered.com/x", "https://api.steampowered.com:8443/x",
    "https://steamdb.info/app/570/", "https://api.steamdb.info/x",
    "https://steamcharts.com/app/570", "https://api.steampowered.com.steamdb.info/x",
    "https://store.steampowered.com@steamdb.info/x",
])
def test_unapproved_sources_are_rejected_before_dispatch(url):
    with httpx.Client(transport=httpx.MockTransport(lambda r: pytest.fail("must not dispatch"))) as client:
        with pytest.raises(SourceError) as caught:
            request(client,url,{},1024)
    assert caught.value.code == "source_not_allowed"


def test_store_retains_only_name_identity_not_extra_fields():
    source = {"570":{"success":True,"data":{"steam_appid":570,"name":"Dota 2","description":"discard me","reviewer_id":"private"}}}
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200,json=source))) as client:
        capture = store.fetch(client,570,10000)
    assert json.loads(capture.payload) == {"app_id":570,"name":"Dota 2"}
    assert capture.capture_form == "allowlisted_name_projection"
    assert capture.value == "Dota 2"


@pytest.mark.parametrize("data", [{"steam_appid":1,"name":"Wrong app"},{"steam_appid":570,"name":""},{"steam_appid":570,"name":"line\nbreak"},{}])
def test_store_invalid_required_fields_fail(data):
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200,json={"570":{"success":True,"data":data}}))) as client:
        with pytest.raises(SourceError):
            store.fetch(client,570,10000)


class MemoryLedger:
    """Only an offline orchestration fixture; PostgreSQL persistence is tested separately."""
    def __init__(self):
        self.attempts, self.captures, self.failures, self.reports = [],[],[],[]
    def collection_lock(self):
        from contextlib import nullcontext
        return nullcontext()
    def initialize(self,*args):
        pass
    def start_run(self,*args):
        return "run-1"
    def reserve_attempt(self,*args,**kwargs):
        self.attempts.append(args)
        return str(len(self.attempts))
    def record_capture(self,run,attempt,capture):
        self.captures.append(capture)
        return "capture-"+attempt
    def record_failure(self,attempt,error):
        self.failures.append((attempt,error))
    def finish_run(self,report):
        self.reports.append(report)


def test_optional_source_failure_is_partial_not_a_successful_run():
    settings = Settings.model_validate({"storage": {"database_url": "postgresql://fixture:fixture@db:5432/fixture"},
        "sources": {"store_metadata_enabled": True},
        "http": {"timeout_seconds": 1, "max_response_bytes": 10000},
        "quota": {"webapi_rolling_24h": 20, "store_rolling_24h": 20}})
    def respond(req):
        return httpx.Response(200,json={"response":{"result":1,"player_count":0}}) if req.url.host == "api.steampowered.com" else httpx.Response(503)
    ledger = MemoryLedger()
    report = collect_once(settings,ledger,transport=httpx.MockTransport(respond))
    assert report["status"] == "partial"
    assert report["request_count"] == 2
    assert len(ledger.attempts) == 2
    assert len(ledger.captures) == 1 and ledger.captures[0].value == 0
    assert len(ledger.failures) == 1
    assert ledger.reports == [report]


def test_retry_after_parser_invalid_value_does_not_invent_a_delay():
    assert retry_after("not a date") is None
    assert retry_after("20") == 20


def test_retry_after_extreme_integer_is_bounded_without_overflow():
    # An invalid extreme integer must never crash parsing or force unbounded sleep.
    assert retry_after("9"*400) is None


def test_successful_retry_keeps_attempt_failure_but_no_final_error(monkeypatch):
    settings = Settings.model_validate({"storage": {"database_url": "postgresql://fixture:fixture@db:5432/fixture"},
        "sources": {"store_metadata_enabled": False},
        "http": {"timeout_seconds": 1, "max_attempts": 2, "max_response_bytes": 10000},
        "quota": {"webapi_rolling_24h": 20, "store_rolling_24h": 20}})
    responses = iter([httpx.Response(503),httpx.Response(200,json={"response":{"result":1,"player_count":9}})])
    monkeypatch.setattr("game_census.collector.time.sleep",lambda seconds: None)
    ledger = MemoryLedger()
    report = collect_once(settings,ledger,transport=httpx.MockTransport(lambda req: next(responses)))
    assert report["status"] == "succeeded"
    assert len(ledger.failures) == 1
    assert report["request_count"] == 2
    assert "error" not in report["apps"][0]["sources"][0]
