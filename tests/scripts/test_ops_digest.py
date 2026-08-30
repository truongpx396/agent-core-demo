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
"""
from scripts import ops_digest


class _FakeResponse:
    def __init__(self, content, usage_metadata=None):
        self.content = content
        self.usage_metadata = usage_metadata or {}


class _FakeChat:
    def __init__(self, response):
        self._response = response
        self.invoked_with = None

    def invoke(self, messages):
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
    monkeypatch.setattr(ops_digest.metrics_client, "fetch_readings", lambda: {"turn_error_rate": 0.01})
    monkeypatch.setattr(ops_digest.metrics_client, "detect_anomalies", lambda readings: [])

    posted = {}
    monkeypatch.setattr(
        ops_digest.notify,
        "post_to_team_channel",
        lambda channel, message: posted.setdefault(channel, message),
    )
    monkeypatch.setattr(ops_digest, "record_usage", lambda *a, **kw: None)

    fake_chat = _FakeChat(_FakeResponse("Everything is healthy today."))
    summary = ops_digest.run_digest(llm=fake_chat)

    assert summary == "Everything is healthy today."
    assert posted["ops-digest"] == "Everything is healthy today."


def test_run_digest_records_usage_when_tokens_are_reported(monkeypatch):
    monkeypatch.setattr(ops_digest.metrics_client, "fetch_readings", lambda: {})
    monkeypatch.setattr(ops_digest.metrics_client, "detect_anomalies", lambda readings: [])
    monkeypatch.setattr(ops_digest.notify, "post_to_team_channel", lambda channel, message: None)

    recorded = {}

    def _fake_record_usage(ctx, thread_id, model_alias, total_tokens):
        recorded["total_tokens"] = total_tokens

    monkeypatch.setattr(ops_digest, "record_usage", _fake_record_usage)

    fake_chat = _FakeChat(_FakeResponse("summary", usage_metadata={"total_tokens": 42}))
    ops_digest.run_digest(llm=fake_chat)

    assert recorded["total_tokens"] == 42


def test_run_digest_skips_record_usage_when_no_tokens_reported(monkeypatch):
    monkeypatch.setattr(ops_digest.metrics_client, "fetch_readings", lambda: {})
    monkeypatch.setattr(ops_digest.metrics_client, "detect_anomalies", lambda readings: [])
    monkeypatch.setattr(ops_digest.notify, "post_to_team_channel", lambda channel, message: None)

    called = []
    monkeypatch.setattr(ops_digest, "record_usage", lambda *a, **kw: called.append(True))

    fake_chat = _FakeChat(_FakeResponse("summary"))
    ops_digest.run_digest(llm=fake_chat)

    assert called == []
