"""Tests for scripts/ops_digest.py. `build_digest_prompt` is pure — tested
directly. `run_digest` is tested with a fake LLM (`llm=` DI, same pattern
app/agent/tools.py::_run_subagent_impl already uses) and
metrics_client/notify/record_usage monkeypatched, so this stays hermetic
(no live Prometheus, LLM, or Postgres) — matching the rest of this suite's
discipline (tests/conftest.py's autouse mock_appdata_postgres degrades
app.agent.meter.get_connection itself, but record_usage also calls
app.agent.model_resolver.resolve_model, a real network call this test
avoids entirely by monkeypatching record_usage directly rather than
exercising its internals — those have their own test coverage elsewhere).

`run_digest` is `async def` now (awaits `metrics_client.fetch_readings`,
`chat.ainvoke`, `record_usage`, `notify.post_to_team_channel` — all real
I/O), so every call below runs through `asyncio.run(...)`.
"""
import asyncio

from scripts import ops_digest


class _FakeResponse:
    def __init__(self, content, usage_metadata=None):
        self.content = content
        self.usage_metadata = usage_metadata or {}


class _FakeChat:
    def __init__(self, response):
        self._response = response
        self.invoked_with = None

    async def ainvoke(self, messages):
        self.invoked_with = messages
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

    async def fake_record_usage(*a, **kw):
        return None

    monkeypatch.setattr(ops_digest, "record_usage", fake_record_usage)

    fake_chat = _FakeChat(_FakeResponse("Everything is healthy today."))
    summary = asyncio.run(ops_digest.run_digest(llm=fake_chat))

    assert summary == "Everything is healthy today."
    assert posted["ops-digest"] == "Everything is healthy today."


def test_run_digest_records_usage_when_tokens_are_reported(monkeypatch):
    async def fake_fetch_readings():
        return {}

    monkeypatch.setattr(ops_digest.metrics_client, "fetch_readings", fake_fetch_readings)
    monkeypatch.setattr(ops_digest.metrics_client, "detect_anomalies", lambda readings: [])

    async def fake_post_to_team_channel(channel, message):
        return None

    monkeypatch.setattr(ops_digest.notify, "post_to_team_channel", fake_post_to_team_channel)

    recorded = {}

    async def _fake_record_usage(ctx, thread_id, model_alias, total_tokens):
        recorded["total_tokens"] = total_tokens

    monkeypatch.setattr(ops_digest, "record_usage", _fake_record_usage)

    fake_chat = _FakeChat(_FakeResponse("summary", usage_metadata={"total_tokens": 42}))
    asyncio.run(ops_digest.run_digest(llm=fake_chat))

    assert recorded["total_tokens"] == 42


def test_run_digest_skips_record_usage_when_no_tokens_reported(monkeypatch):
    async def fake_fetch_readings():
        return {}

    monkeypatch.setattr(ops_digest.metrics_client, "fetch_readings", fake_fetch_readings)
    monkeypatch.setattr(ops_digest.metrics_client, "detect_anomalies", lambda readings: [])

    async def fake_post_to_team_channel(channel, message):
        return None

    monkeypatch.setattr(ops_digest.notify, "post_to_team_channel", fake_post_to_team_channel)

    called = []

    async def fake_record_usage(*a, **kw):
        called.append(True)

    monkeypatch.setattr(ops_digest, "record_usage", fake_record_usage)

    fake_chat = _FakeChat(_FakeResponse("summary"))
    asyncio.run(ops_digest.run_digest(llm=fake_chat))

    assert called == []
