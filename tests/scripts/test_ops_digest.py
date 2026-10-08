"""Tests for scripts/ops_digest.py. `build_digest_prompt` is pure — tested
directly. `run_digest` is tested with a fake LLM (`llm=` DI, same pattern
app/agent/tools.py::_run_subagent_impl already uses) and
metrics_client/notify monkeypatched, so this stays hermetic (no live
Prometheus, LLM, or Postgres) — matching the rest of this suite's discipline.
The call goes through the metering choke point, whose usage-event write is
captured by tests/conftest.py's autouse `usage_event_sink` (so "this call was
metered" is assertable without a database).

`run_digest` is `async def` (awaits `metrics_client.fetch_readings`,
`chat.ainvoke`, `notify.post_to_team_channel` — all real I/O), so every call
below runs through `asyncio.run(...)`.
"""
import asyncio

import pytest

from app.agent import gateway
from scripts import ops_digest


class _FakeResponse:
    def __init__(self, content, usage_metadata=None):
        self.content = content
        self.usage_metadata = usage_metadata or {}


class _FakeChat:
    def __init__(self, response):
        self._response = response
        self.invoked_with = None
        self.invoked_kwargs = None

    async def ainvoke(self, messages, **kwargs):
        self.invoked_with = messages
        self.invoked_kwargs = kwargs
        return self._response


def test_build_digest_prompt_includes_readings_and_flags_anomalies():
    readings = {"turn_error_rate": 0.5}
    prompt = ops_digest.build_digest_prompt(readings, ["turn error rate: 0.5 (threshold 0.05)"])
    assert "0.5" in prompt
    assert "Flagged anomalies" in prompt


def test_build_digest_prompt_says_no_anomalies_when_none_found():
    prompt = ops_digest.build_digest_prompt({}, [])
    assert "No anomalies" in prompt


def test_run_digest_posts_the_summary_to_the_team_channel(monkeypatch):
    async def fake_fetch_readings():
        return {"turn_error_rate": 0.01}

    monkeypatch.setattr(ops_digest.metrics_client, "fetch_readings", fake_fetch_readings)
    monkeypatch.setattr(ops_digest.metrics_client, "detect_anomalies", lambda readings: [])

    posted = {}

    async def fake_post_to_team_channel(channel, message):
        posted.setdefault(channel, message)

    monkeypatch.setattr(ops_digest.notify, "post_to_team_channel", fake_post_to_team_channel)



    fake_chat = _FakeChat(_FakeResponse("Everything is healthy today."))
    summary = asyncio.run(ops_digest.run_digest(llm=fake_chat))

    assert summary == "Everything is healthy today."
    assert posted["ops-digest"] == "Everything is healthy today."


def test_run_digest_tells_the_gateway_whose_call_it_is(monkeypatch):
    """Otherwise the gateway's spend log shows this cron's spend as an anonymous caller
    (app/agent/gateway.py)."""

    async def fake_fetch_readings():
        return {"turn_error_rate": 0.01}

    async def fake_post_to_team_channel(channel, message):
        return None


    monkeypatch.setattr(ops_digest.metrics_client, "fetch_readings", fake_fetch_readings)
    monkeypatch.setattr(ops_digest.metrics_client, "detect_anomalies", lambda readings: [])
    monkeypatch.setattr(ops_digest.notify, "post_to_team_channel", fake_post_to_team_channel)

    fake_chat = _FakeChat(_FakeResponse("Everything is healthy today."))
    asyncio.run(ops_digest.run_digest(llm=fake_chat))

    assert fake_chat.invoked_kwargs == gateway.call_identity(ops_digest._CRON_CTX)
    assert fake_chat.invoked_kwargs["user"] == gateway.end_user_id(ops_digest.DEFAULT_TENANT)


def test_run_digest_records_usage_when_tokens_are_reported(monkeypatch, usage_event_sink):
    async def fake_fetch_readings():
        return {}

    monkeypatch.setattr(ops_digest.metrics_client, "fetch_readings", fake_fetch_readings)
    monkeypatch.setattr(ops_digest.metrics_client, "detect_anomalies", lambda readings: [])

    async def fake_post_to_team_channel(channel, message):
        return None

    monkeypatch.setattr(ops_digest.notify, "post_to_team_channel", fake_post_to_team_channel)

    fake_chat = _FakeChat(_FakeResponse("summary", usage_metadata={"total_tokens": 42}))
    asyncio.run(ops_digest.run_digest(llm=fake_chat))

    assert [event["total_tokens"] for event in usage_event_sink] == [42]


def test_run_digest_records_a_cron_usage_event(monkeypatch, usage_event_sink):
    """The call goes through the metering choke point (app/agent/metering.py), so it is one usage
    event under its own kind, attributed to the cron's identity, not an unmetered side call."""

    async def fake_fetch_readings():
        return {}

    async def fake_post_to_team_channel(channel, message):
        return None


    monkeypatch.setattr(ops_digest.metrics_client, "fetch_readings", fake_fetch_readings)
    monkeypatch.setattr(ops_digest.metrics_client, "detect_anomalies", lambda readings: [])
    monkeypatch.setattr(ops_digest.notify, "post_to_team_channel", fake_post_to_team_channel)

    usage = {"input_tokens": 30, "output_tokens": 12, "total_tokens": 42}
    asyncio.run(ops_digest.run_digest(llm=_FakeChat(_FakeResponse("summary", usage_metadata=usage))))

    (event,) = usage_event_sink
    assert (event["kind"], event["tenant"], event["principal"]) == ("cron", ops_digest.DEFAULT_TENANT, "ops-cron")
    assert event["thread_id"].startswith("ops-digest:")
    assert (event["input_tokens"], event["output_tokens"], event["total_tokens"]) == (30, 12, 42)


def test_run_digest_writes_no_event_when_no_tokens_are_reported(monkeypatch, usage_event_sink):
    async def fake_fetch_readings():
        return {}

    monkeypatch.setattr(ops_digest.metrics_client, "fetch_readings", fake_fetch_readings)
    monkeypatch.setattr(ops_digest.metrics_client, "detect_anomalies", lambda readings: [])

    async def fake_post_to_team_channel(channel, message):
        return None

    monkeypatch.setattr(ops_digest.notify, "post_to_team_channel", fake_post_to_team_channel)

    fake_chat = _FakeChat(_FakeResponse("summary"))
    asyncio.run(ops_digest.run_digest(llm=fake_chat))

    assert usage_event_sink == []


def test_run_digest_records_the_priced_cost_of_its_own_call(monkeypatch, usage_event_sink):
    """Spec 008 A2: a scheduled job calls the model itself, so it must price that
    call itself — it used to hand the ledger a table-derived cost, and so any
    model off the table cost $0 here too. (The priced figure is now the usage event's.)"""
    from app.agent import pricing
    from app.core.config import CHAT_MODEL

    async def priced_fetch():
        return [
            {
                "model_name": CHAT_MODEL,
                "model_info": {"input_cost_per_token": 2.5e-06, "output_cost_per_token": 1e-05},
            }
        ]

    async def fake_fetch_readings():
        return {}

    async def fake_post_to_team_channel(channel, message):
        return None

    monkeypatch.setattr(pricing, "_fetch_model_info", priced_fetch)
    monkeypatch.setattr(ops_digest.metrics_client, "fetch_readings", fake_fetch_readings)
    monkeypatch.setattr(ops_digest.metrics_client, "detect_anomalies", lambda readings: [])
    monkeypatch.setattr(ops_digest.notify, "post_to_team_channel", fake_post_to_team_channel)
    usage = {"input_tokens": 1000, "output_tokens": 500, "total_tokens": 1500}

    asyncio.run(ops_digest.run_digest(llm=_FakeChat(_FakeResponse("summary", usage_metadata=usage))))

    (event,) = usage_event_sink
    assert event["cost_usd"] == pytest.approx(0.0075)  # 1000 * 2.5e-06 + 500 * 1e-05
