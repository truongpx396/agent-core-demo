"""The API says which way it is learning who is calling, and will not start if it cannot (specs/018, task T060).

Header mode trusts two headers any client can set. It is for tests and a developer's own machine; the mistake it invites is a deployment that
forgot to leave it. So it is announced three ways: a warning when the API starts, the gauge `agent_api_auth_mode{mode}` (alert `ApiHeaderAuthMode`),
and, here, the proof that the gauge STAYS reported: the collector forgets a series five minutes after its last update, so a value set once at
startup would be gone before a ten-minute alert could see it.

The lifespan is entered with telemetry stubbed out: it must refuse a misconfiguration before it opens anything, and that is the part under test.
`TestClient(app)` elsewhere never runs it (tests/api/test_api.py).
"""
import asyncio
import logging

import pytest

from app.api import identity
from app.api import main as api
from app.core import metrics
from app.core.config import settings
from app.core.internal_identity import AuthConfigError
from tests.conftest import _METRIC_READER


def _modes() -> dict[str, float]:
    """Both series of the gauge from ONE collection. An OTel gauge reports a point once after it is set and then nothing until it is set again, and
    `metric_value` collects on every call, so reading the two series with two calls would find the second one gone (and read it as 0, which is
    exactly the value a wrong test would be hoping for). That is also why the API re-sets the gauge on a timer."""
    out: dict[str, float] = {}
    data = _METRIC_READER.get_metrics_data()
    for resource_metrics in data.resource_metrics if data else []:
        for scope_metrics in resource_metrics.scope_metrics:
            for metric in scope_metrics.metrics:
                if metric.name == metrics.agent_api_auth_mode.name:
                    out.update({point.attributes["mode"]: point.value for point in metric.data.data_points})
    return out


@pytest.mark.parametrize("mode,other", [("headers", "identity"), ("identity", "headers")])
def test_the_gauge_is_one_for_the_mode_in_force_and_zero_for_the_other(mode, other):
    identity.announce_auth_mode(mode)

    assert _modes() == {mode: 1.0, other: 0.0}, "both series exist, so an alert on either can tell 'off' from 'never reported'"


def test_changing_the_mode_flips_both_series():
    identity.announce_auth_mode("headers")
    identity.announce_auth_mode("identity")

    assert _modes() == {"headers": 0.0, "identity": 1.0}


async def test_the_gauge_is_set_again_and_again_for_as_long_as_the_api_runs(monkeypatch):
    calls = []
    monkeypatch.setattr(identity, "announce_auth_mode", lambda mode: calls.append(mode))

    task = asyncio.create_task(identity.keep_announcing_auth_mode("headers", interval_seconds=0.01))
    await asyncio.sleep(0.15)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(calls) >= 3 and set(calls) == {"headers"}


def test_the_default_interval_is_well_inside_the_collectors_five_minute_memory():
    assert identity.ANNOUNCE_INTERVAL_SECONDS <= 60


def test_header_mode_is_a_warning_in_the_log_and_names_no_header_value(caplog):
    caplog.set_level(logging.INFO)

    identity.warn_if_header_mode("headers")

    (record,) = [r for r in caplog.records if r.name == "app.api.identity"]
    assert record.levelno == logging.WARNING and record.getMessage() == "api_auth_mode_headers"
    assert record.auth_mode == "headers"  # type: ignore[attr-defined]  # an `extra` field the logging module sets as an attribute


def test_identity_mode_is_an_info_line_not_a_warning(caplog):
    caplog.set_level(logging.INFO)

    identity.warn_if_header_mode("identity")

    (record,) = [r for r in caplog.records if r.name == "app.api.identity"]
    assert record.levelno == logging.INFO


async def test_identity_mode_without_the_key_address_does_not_start_and_opens_nothing(identity_mode, monkeypatch):
    """The refusal comes before any pool is opened, naming the variable; nothing below it in the lifespan runs."""
    monkeypatch.setattr(settings, "authlimit_keys_url", "")
    monkeypatch.setattr(api, "configure_telemetry", lambda _name: None)
    opened = []

    async def not_expected():
        opened.append("graph")

    monkeypatch.setattr(api, "init_graph_async", not_expected)
    monkeypatch.setattr(api.billing_http, "validate_configuration", lambda: opened.append("billing"))

    with pytest.raises(AuthConfigError, match="AUTHLIMIT_KEYS_URL"):
        async with api.lifespan(api.app):
            pass  # pragma: no cover - the refusal comes first

    assert opened == []


async def test_header_mode_starts_announces_and_closes_cleanly(monkeypatch, caplog):
    """The rest of the lifespan, with the things that need a real service stubbed: header mode needs no key address, the gauge is set and kept,
    and shutdown cancels the announcing task and closes the verifier's client."""
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(api, "configure_telemetry", lambda _name: None)
    monkeypatch.setattr(api.billing_http, "validate_configuration", lambda: None)

    async def no_graph():
        return None

    async def no_op():
        return None

    monkeypatch.setattr(api, "init_graph_async", no_graph)
    monkeypatch.setattr(api, "close_all_connections", no_op)
    monkeypatch.setattr(api.sql_store, "close_pool", no_op)
    monkeypatch.setattr(api, "close_checkpointer_pool", no_op)
    closed = []

    async def record_close():
        closed.append(True)

    monkeypatch.setattr(identity, "close", record_close)

    async with api.lifespan(api.app):
        await asyncio.sleep(0)  # let the announcing task run once
        assert _modes() == {"headers": 1.0, "identity": 0.0}
        assert any(r.getMessage() == "api_auth_mode_headers" and r.levelno == logging.WARNING for r in caplog.records)

    assert closed == [True]
