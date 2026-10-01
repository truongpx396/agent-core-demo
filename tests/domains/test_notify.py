"""Tests for app/domains/notify.py::post_to_team_channel — same "patch the
low-level client constructor" convention as tests/domains/ops/test_metrics_client.py
for the Slack leg; the local sink is a real `var/team_channel.log` write,
redirected to a tmp_path so this never touches the repo's own `var/`.

`agent_team_channel_notify_total{sink,outcome}` is the metric this module
gained specifically because this is a saga PIVOT send (see its own module
docstring) — a failed push must still be OBSERVABLE even though it's never
load-bearing, which is exactly what every "_records_..." test below proves.
"""
from app.core import metrics
from app.domains import notify
from tests.conftest import metric_value as _count


class _FakeResponse:
    def __init__(self, status_code=200):
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeAsyncClient:
    def __init__(self, response=None, raise_on_post=None):
        self._response = response
        self._raise_on_post = raise_on_post
        self.posted = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None):
        self.posted.append((url, json))
        if self._raise_on_post:
            raise self._raise_on_post
        return self._response


class TestLocalSink:
    async def test_appends_a_line_and_records_an_ok_outcome(self, monkeypatch, tmp_path):
        monkeypatch.setattr(notify, "_LOG_PATH", tmp_path / "team_channel.log")
        monkeypatch.setattr(notify, "SLACK_WEBHOOK_URL", None)
        before = _count(metrics.agent_team_channel_notify_total, sink="local", outcome="ok")

        result = await notify.post_to_team_channel("ops-alerts", "something happened")

        assert "ops-alerts" in result
        assert "something happened" in (tmp_path / "team_channel.log").read_text()
        assert _count(metrics.agent_team_channel_notify_total, sink="local", outcome="ok") == before + 1

    async def test_a_write_failure_never_raises_but_records_an_error_outcome(self, monkeypatch, tmp_path):
        # A path whose parent can never be created (a file, not a dir, in
        # its place) forces mkdir to fail — the cheapest real OSError.
        blocking_file = tmp_path / "not_a_directory"
        blocking_file.write_text("x")
        monkeypatch.setattr(notify, "_LOG_PATH", blocking_file / "team_channel.log")
        monkeypatch.setattr(notify, "SLACK_WEBHOOK_URL", None)
        before = _count(metrics.agent_team_channel_notify_total, sink="local", outcome="error")

        result = await notify.post_to_team_channel("ops-alerts", "something happened")

        assert "ops-alerts" in result  # never raises — still returns normally
        assert _count(metrics.agent_team_channel_notify_total, sink="local", outcome="error") == before + 1


class TestSlackSink:
    async def test_not_posted_at_all_when_no_webhook_is_configured(self, monkeypatch, tmp_path):
        monkeypatch.setattr(notify, "_LOG_PATH", tmp_path / "team_channel.log")
        monkeypatch.setattr(notify, "SLACK_WEBHOOK_URL", None)
        before_ok = _count(metrics.agent_team_channel_notify_total, sink="slack", outcome="ok")
        before_err = _count(metrics.agent_team_channel_notify_total, sink="slack", outcome="error")

        await notify.post_to_team_channel("ops-alerts", "something happened")

        assert _count(metrics.agent_team_channel_notify_total, sink="slack", outcome="ok") == before_ok
        assert _count(metrics.agent_team_channel_notify_total, sink="slack", outcome="error") == before_err

    async def test_a_successful_post_records_an_ok_outcome(self, monkeypatch, tmp_path):
        monkeypatch.setattr(notify, "_LOG_PATH", tmp_path / "team_channel.log")
        monkeypatch.setattr(notify, "SLACK_WEBHOOK_URL", "https://hooks.example.com/x")
        fake_client = _FakeAsyncClient(response=_FakeResponse(200))
        monkeypatch.setattr(notify.httpx, "AsyncClient", lambda **kw: fake_client)
        before = _count(metrics.agent_team_channel_notify_total, sink="slack", outcome="ok")

        await notify.post_to_team_channel("ops-alerts", "something happened")

        assert len(fake_client.posted) == 1
        assert _count(metrics.agent_team_channel_notify_total, sink="slack", outcome="ok") == before + 1

    async def test_a_failed_post_never_raises_but_records_an_error_outcome(self, monkeypatch, tmp_path):
        """The gap this metric closes: before it existed, this failure was
        a log line only — nothing could ever alert on a human going
        unnotified about a real, already-committed escalation/handoff."""
        monkeypatch.setattr(notify, "_LOG_PATH", tmp_path / "team_channel.log")
        monkeypatch.setattr(notify, "SLACK_WEBHOOK_URL", "https://hooks.example.com/x")
        fake_client = _FakeAsyncClient(raise_on_post=ConnectionError("unreachable"))
        monkeypatch.setattr(notify.httpx, "AsyncClient", lambda **kw: fake_client)
        before = _count(metrics.agent_team_channel_notify_total, sink="slack", outcome="error")

        result = await notify.post_to_team_channel("ops-alerts", "something happened")

        assert "ops-alerts" in result  # never raises — still returns normally
        assert _count(metrics.agent_team_channel_notify_total, sink="slack", outcome="error") == before + 1

    async def test_a_4xx_response_counts_as_an_error_not_an_ok(self, monkeypatch, tmp_path):
        monkeypatch.setattr(notify, "_LOG_PATH", tmp_path / "team_channel.log")
        monkeypatch.setattr(notify, "SLACK_WEBHOOK_URL", "https://hooks.example.com/x")
        fake_client = _FakeAsyncClient(response=_FakeResponse(404))
        monkeypatch.setattr(notify.httpx, "AsyncClient", lambda **kw: fake_client)
        before = _count(metrics.agent_team_channel_notify_total, sink="slack", outcome="error")

        await notify.post_to_team_channel("ops-alerts", "something happened")

        assert _count(metrics.agent_team_channel_notify_total, sink="slack", outcome="error") == before + 1
