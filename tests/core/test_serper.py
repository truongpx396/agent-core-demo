"""`app/core/serper.py`: the serper.dev client behind `web_search` (GRAPH_PATTERNS.md pattern 87).

All of it runs on `httpx.MockTransport`; the one real call is `make web-search-check`. Several tests pin behavior that was MEASURED against the live
API on 2026-10-28 (the header, the body, the `organic` shape, a wrong key being a 403, a blank query being billed). What a mock cannot show is that the
provider still behaves that way, which is what the manual check is for. Two things were not measured and have no test claiming them: what an account out
of credits returns, and what a 429 looks like. They are mapped by status class (`test_each_failure_class...`), which pins the mapping, not the provider.
"""
import logging

import httpx
import pytest
from pydantic import ValidationError

from app.core import metrics, serper
from app.core.config import Settings
from app.core.resilience import CircuitBreaker
from tests.conftest import metric_value

KEY = "test-serper-key-0123456789abcdef"


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    """A key, a fresh breaker per test (its state is process-wide), and no real sleeping between retries."""
    monkeypatch.setattr(serper, "SERPER_API_KEY", KEY)
    monkeypatch.setattr(serper, "WEB_SEARCH_MAX_RESULTS", 5)
    monkeypatch.setattr(serper, "_BREAKER", CircuitBreaker(name="serper-test", failure_threshold=3, cooldown_seconds=30.0))
    monkeypatch.setattr(serper, "_RETRY_BASE_DELAY_SECONDS", 0.0)


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _ok(organic: list) -> httpx.Response:
    return httpx.Response(200, json={"searchParameters": {"q": "x"}, "organic": organic, "credits": 1})


def _item(n: int = 1, **overrides) -> dict:
    return {"title": f"Title {n}", "link": f"https://example.com/{n}", "snippet": f"Snippet {n}", "position": n, **overrides}


def _outcome(name: str) -> int:
    return metric_value(metrics.agent_web_search_total, outcome=name)


async def test_a_search_posts_the_query_with_the_key_in_a_header_and_asks_for_the_configured_number():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _ok([_item()])

    await serper.search("anthropic status", client=_client(handler))

    (request,) = seen
    assert request.method == "POST"
    assert str(request.url) == "https://google.serper.dev/search"
    assert request.headers["x-api-key"] == KEY
    assert request.read() == b'{"q":"anthropic status","num":5}'
    assert KEY not in str(request.url) and KEY.encode() not in request.read(), "the key belongs in the header only"


async def test_results_come_back_as_hits_with_title_address_snippet_and_date():
    handler = lambda request: _ok([_item(1, date="3 days ago"), _item(2)])  # noqa: E731 - a one-line stub

    hits = await serper.search("q", client=_client(handler))

    assert hits == [
        serper.SearchHit("Title 1", "https://example.com/1", "Snippet 1", "3 days ago"),
        serper.SearchHit("Title 2", "https://example.com/2", "Snippet 2", None),
    ]


async def test_the_number_of_hits_is_capped_even_when_the_provider_sends_more(monkeypatch):
    monkeypatch.setattr(serper, "WEB_SEARCH_MAX_RESULTS", 2)
    handler = lambda request: _ok([_item(n) for n in range(1, 6)])  # noqa: E731

    hits = await serper.search("q", client=_client(handler))

    assert [h.title for h in hits] == ["Title 1", "Title 2"]


async def test_a_result_that_is_not_a_web_address_or_has_no_title_is_dropped_and_the_rest_survive():
    organic = [
        _item(1, link="javascript:alert(1)"),
        _item(2, link="data:text/html,<script>"),
        {"link": "https://example.com/no-title"},
        "not even an object",
        _item(5),
    ]

    hits = await serper.search("q", client=_client(lambda request: _ok(organic)))

    assert [h.url for h in hits] == ["https://example.com/5"]


async def test_title_and_snippet_are_bounded_and_whitespace_collapsed():
    huge = _item(1, title="T " * 500, snippet="word\n\n  " * 500)

    (hit,) = await serper.search("q", client=_client(lambda request: _ok([huge])))

    assert len(hit.title) <= 200 and len(hit.snippet) <= 400
    assert "\n" not in hit.snippet and "  " not in hit.snippet


async def test_a_query_is_sent_with_its_whitespace_collapsed():
    seen: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.read())
        return _ok([_item()])

    await serper.search("  stripe \n  status   page ", client=_client(handler))

    assert seen == [b'{"q":"stripe status page","num":5}']


async def test_a_blank_query_is_refused_before_any_request_because_the_provider_bills_it():
    """Measured: serper.dev answered a blank query 200 with unrelated results and `credits: 1`. The tool's argument check is the first line;
    this is the second, for any other caller of the client."""
    calls: list[httpx.Request] = []
    before = _outcome("blank_query")

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return _ok([_item()])

    with pytest.raises(serper.SearchFailed):
        await serper.search("   \n\t ", client=_client(handler))

    assert calls == [], "a blank query must cost nothing"
    assert _outcome("blank_query") == before + 1


async def test_with_no_key_nothing_is_sent_and_the_error_is_the_not_configured_one(monkeypatch):
    monkeypatch.setattr(serper, "SERPER_API_KEY", "")
    calls: list[httpx.Request] = []
    before = _outcome("not_configured")

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return _ok([_item()])

    with pytest.raises(serper.SearchNotConfigured) as caught:
        await serper.search("q", client=_client(handler))

    assert calls == []
    assert isinstance(caught.value, serper.SearchFailed), "callers that catch SearchFailed must still see it"
    assert "not set up" in str(caught.value)
    assert _outcome("not_configured") == before + 1


@pytest.mark.parametrize(
    "status,outcome",
    [(401, "auth_failed"), (403, "auth_failed"), (429, "rate_limited"), (400, "rejected"), (402, "rejected"), (500, "unavailable"), (503, "unavailable")],
)
async def test_each_failure_class_is_counted_under_its_own_outcome_and_tells_the_model_not_to_retry(status, outcome):
    before = _outcome(outcome)
    body = {"message": "SECRET-PROVIDER-TEXT should not reach the model", "statusCode": status}

    with pytest.raises(serper.SearchFailed) as caught:
        await serper.search("q", client=_client(lambda request: httpx.Response(status, json=body)))

    message = str(caught.value)
    assert _outcome(outcome) == before + 1
    assert "Do not retry" in message
    assert "SECRET-PROVIDER-TEXT" not in message and KEY not in message and str(status) not in message, "the model is told what to do, not what the provider said"


async def test_the_providers_own_error_text_goes_to_the_log_for_the_operator_and_never_to_the_model(caplog):
    """Out-of-credits is expected to be a 4xx, but that was never measured. The log line is how an operator learns the real text."""
    body = {"message": "Not enough credits", "statusCode": 400}

    with caplog.at_level(logging.WARNING, logger="app.core.serper"), pytest.raises(serper.SearchFailed) as caught:
        await serper.search("a private customer question", client=_client(lambda request: httpx.Response(400, json=body)))

    (record,) = [r for r in caplog.records if r.name == "app.core.serper"]
    assert (record.outcome, record.status, record.provider_message) == ("rejected", 400, "Not enough credits")
    assert "Not enough credits" not in str(caught.value)
    assert "private customer question" not in record.getMessage() + repr(record.__dict__), "logs carry metadata, never the query"


async def test_an_echo_of_the_key_in_the_providers_error_is_scrubbed_before_it_is_logged(caplog, monkeypatch):
    from app.core import config

    monkeypatch.setattr(config, "SERPER_API_KEY", KEY)
    body = {"message": f"bad key {KEY}", "statusCode": 403}

    with caplog.at_level(logging.WARNING, logger="app.core.serper"), pytest.raises(serper.SearchFailed):
        await serper.search("q", client=_client(lambda request: httpx.Response(403, json=body)))

    (record,) = [r for r in caplog.records if r.name == "app.core.serper"]
    assert KEY not in record.provider_message


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, json=["not", "an", "object"]),
        httpx.Response(200, json={"credits": 1}),
        httpx.Response(200, json={"organic": "nope"}),
        httpx.Response(200, text="<html>a captive portal</html>"),
    ],
    ids=["a list", "no organic", "organic not a list", "not json"],
)
async def test_a_200_that_is_not_the_documented_shape_is_a_bad_response_not_a_crash(response):
    before = _outcome("bad_response")

    with pytest.raises(serper.SearchFailed):
        await serper.search("q", client=_client(lambda request: response))

    assert _outcome("bad_response") == before + 1


async def test_a_search_that_finds_nothing_is_an_empty_list_and_counted_as_no_results_not_a_failure():
    ok_before, none_before = _outcome("ok"), _outcome("no_results")

    assert await serper.search("q", client=_client(lambda request: _ok([]))) == []

    assert _outcome("no_results") == none_before + 1
    assert _outcome("ok") == ok_before


async def test_a_connection_that_never_opens_is_retried_then_reported_unavailable():
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        raise httpx.ConnectError("no route to host")

    before = _outcome("unavailable")
    with pytest.raises(serper.SearchFailed):
        await serper.search("q", client=_client(handler))

    assert len(attempts) == 3, "a connection that never opened provably did not land, so it is the one thing retried"
    assert _outcome("unavailable") == before + 1


async def test_a_connection_that_fails_once_is_retried_and_the_search_succeeds():
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) == 1:
            raise httpx.ConnectTimeout("slow")
        return _ok([_item()])

    hits = await serper.search("q", client=_client(handler))

    assert len(attempts) == 2 and len(hits) == 1


@pytest.mark.parametrize("failure", [httpx.ReadTimeout("slow"), httpx.RemoteProtocolError("dropped")], ids=["read timeout", "dropped mid-response"])
async def test_a_failure_after_the_request_was_sent_is_never_retried_because_it_may_have_been_billed(failure):
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        raise failure

    with pytest.raises(serper.SearchFailed):
        await serper.search("q", client=_client(handler))

    assert len(attempts) == 1, "Principle IV: retry only what provably never landed"


async def test_repeated_connection_failures_open_the_breaker_and_the_next_call_makes_no_request():
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        raise httpx.ConnectError("down")

    for _ in range(3):  # failure_threshold calls, each exhausting its own retries
        with pytest.raises(serper.SearchFailed):
            await serper.search("q", client=_client(handler))
    spent = len(attempts)
    before = _outcome("circuit_open")

    with pytest.raises(serper.SearchFailed) as caught:
        await serper.search("q", client=_client(handler))

    assert len(attempts) == spent, "an open breaker fails fast without touching the network"
    assert _outcome("circuit_open") == before + 1
    assert "Do not retry" in str(caught.value)


async def test_without_a_client_the_search_opens_and_closes_its_own(monkeypatch):
    """Production passes no client. Patched at the class so the real `async with` path runs, over a mock transport."""
    opened = []
    real = httpx.AsyncClient

    class Tracking(real):
        def __init__(self, *args, **kwargs):
            opened.append(kwargs.get("timeout"))
            super().__init__(*args, transport=httpx.MockTransport(lambda request: _ok([_item()])), **{k: v for k, v in kwargs.items() if k != "transport"})

    monkeypatch.setattr(serper.httpx, "AsyncClient", Tracking)

    hits = await serper.search("q")

    assert len(hits) == 1
    assert opened == [serper._TIMEOUT], "the owned client carries the module's timeout"


def test_the_result_count_setting_is_bounded_to_what_the_provider_page_allows():
    for bad in (0, 11):
        with pytest.raises(ValidationError):
            Settings(web_search_max_results=bad, _env_file=None)
    assert Settings(web_search_max_results=10, _env_file=None).web_search_max_results == 10
