"""Every cost-governance path that fails and carries on is counted (spec 008 A1).

Each of these already had a test that the failure does NOT reach the turn (see
test_record_usage.py, test_usage_ledger.py, test_tenant_budget.py,
test_model_resolver.py). What none of them asserted is that the failure leaves a
trace anywhere but a log line — and a lost ledger row is committed spend that a
human will not learn about from a log. Constitution Principle V: a
degrade-and-continue path MUST increment a metric.

Statement shape and the fail-open behaviour are covered by those files; this one
only pins "and it is counted, under the right `path`".
"""
import httpx
import pytest

from app.agent import model_resolver, usage_ledger
from app.agent import runtime as runtime_module
from app.core import metrics
from tests.conftest import TEST_CTX, metric_value


def _count(path: str) -> float:
    return metric_value(metrics.agent_cost_governance_degraded_total, path=path)


def _broken():
    raise ConnectionError("appdata postgres unreachable")


async def test_a_failed_ledger_write_is_counted_as_ledger_write(monkeypatch):
    monkeypatch.setattr(usage_ledger, "get_connection", _broken)
    before = _count("ledger_write")

    await usage_ledger.record_usage(TEST_CTX, "thread-1", "chat", 500, 0.1)

    assert _count("ledger_write") == before + 1


async def test_a_failed_ledger_read_in_the_allowance_check_is_counted_as_ledger_read(monkeypatch):
    async def broken_summary(*args, **kwargs):
        raise ConnectionError("appdata postgres unreachable")

    monkeypatch.setattr(usage_ledger, "usage_summary", broken_summary)
    before = _count("ledger_read")

    allowance = await runtime_module._check_allowance(TEST_CTX)
    assert allowance.refused is False  # still fails open
    assert allowance.degraded is True  # ...and says it was not actually verified

    assert _count("ledger_read") == before + 1


async def test_a_failed_reservation_write_release_and_read_are_each_counted(monkeypatch):
    monkeypatch.setattr(usage_ledger, "get_connection", _broken)
    before = _count("reservation")

    assert await usage_ledger.reserve_budget(TEST_CTX, 0.5) is None
    await usage_ledger.release_budget_reservation(TEST_CTX, "some-hold-id")
    assert await usage_ledger.in_flight_reservation("acme") == 0.0

    assert _count("reservation") == before + 3


@pytest.fixture
def _fresh_resolver():
    model_resolver._cache.clear()
    model_resolver._failed_at.clear()
    yield
    model_resolver._cache.clear()
    model_resolver._failed_at.clear()


async def test_a_failed_model_resolution_is_counted_as_model_resolve(monkeypatch, _fresh_resolver):
    real = httpx.AsyncClient
    monkeypatch.setattr(
        model_resolver.httpx,
        "AsyncClient",
        lambda **kwargs: real(transport=httpx.MockTransport(lambda request: httpx.Response(500)), **kwargs),
    )
    before = _count("model_resolve")

    assert await model_resolver.resolve_model("chat") is None

    assert _count("model_resolve") == before + 1


async def test_an_alias_the_proxy_does_not_list_is_not_counted_as_a_degradation(monkeypatch, _fresh_resolver):
    """An unknown alias is an answer, not a failure — counting it would make the
    counter fire on a healthy proxy."""
    real = httpx.AsyncClient
    monkeypatch.setattr(
        model_resolver.httpx,
        "AsyncClient",
        lambda **kwargs: real(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"data": []})), **kwargs
        ),
    )
    before = _count("model_resolve")

    assert await model_resolver.resolve_model("nonexistent") is None

    assert _count("model_resolve") == before
