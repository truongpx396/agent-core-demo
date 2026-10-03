"""Scheduled jobs must export the metrics they record.

`scripts/ops_digest.py`, `followup_sweep.py`, `tool_call_dedup_sweep.py` and
`ops_investigate.py` are run by cron (or by hand) as short-lived processes. Each
configured logging and nothing else, so every counter they incremented died with
the process. That is not hypothetical: `ops_digest` posts to the team channel, and
`TeamChannelNotifyFailing` alerts on `agent_team_channel_notify_total{outcome="error"}`
— which a failing digest post incremented in a process that never exported it, so
the alert could not fire for the one caller that runs unattended on a schedule
(spec 008, B22).

Configuring telemetry is not enough for a job: the exporter pushes on a 15 s
timer, so a job that finishes sooner exports nothing unless the provider is
flushed before the process exits. Three things are checked:
  * each script has a `main()` that sets up telemetry before the work and flushes
    after it — even when the work raises;
  * `shutdown_telemetry` flushes then stops the provider, once, and never raises;
  * against a real OTLP exporter and a local HTTP endpoint: a short-lived provider
    sends NOTHING until flushed, and its counter arrives once it is.
"""
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.core import job_runtime, telemetry
from scripts import followup_sweep, ops_digest, ops_investigate, tool_call_dedup_sweep


@pytest.fixture
def events(monkeypatch):
    """Record the order in which a job's setup, work and flush happen."""
    log: list[str] = []
    monkeypatch.setattr(job_runtime, "configure_logging", lambda: log.append("logging"))
    monkeypatch.setattr(job_runtime, "configure_telemetry", lambda name: log.append(f"telemetry:{name}"))
    monkeypatch.setattr(job_runtime, "shutdown_telemetry", lambda: log.append("flush"))
    return log


def _fake_work(log, result):
    async def work(*args, **kwargs):
        log.append("work")
        if isinstance(result, Exception):
            raise result
        return result

    return work


_JOBS = [
    pytest.param(ops_digest, "run_digest", "digest text", "agent-core-ops-digest", id="ops_digest"),
    pytest.param(followup_sweep, "run_followup_sweep", [], "agent-core-followup-sweep", id="followup_sweep"),
    pytest.param(tool_call_dedup_sweep, "run_sweep", 0, "agent-core-tool-call-dedup-sweep", id="tool_call_dedup_sweep"),
    pytest.param(ops_investigate, "investigate", "an answer", "agent-core-ops-investigate", id="ops_investigate"),
]


@pytest.mark.parametrize(("module", "work_name", "result", "service"), _JOBS)
def test_a_scheduled_job_configures_telemetry_before_its_work_and_flushes_after(
    module, work_name, result, service, events, monkeypatch, capsys
):
    monkeypatch.setattr(module, work_name, _fake_work(events, result))
    monkeypatch.setattr(sys, "argv", ["job"])

    module.main()

    assert events == ["logging", f"telemetry:{service}", "work", "flush"]


@pytest.mark.parametrize(("module", "work_name", "result", "service"), _JOBS)
def test_a_job_that_fails_still_flushes_what_it_recorded(module, work_name, result, service, events, monkeypatch):
    """The failure path is the one the alert exists for."""
    monkeypatch.setattr(module, work_name, _fake_work(events, RuntimeError("boom")))
    monkeypatch.setattr(sys, "argv", ["job"])

    with pytest.raises(RuntimeError, match="boom"):
        module.main()

    assert events[-2:] == ["work", "flush"]


def test_each_job_reports_under_its_own_service_name():
    names = [param.values[3] for param in _JOBS]

    assert len(set(names)) == len(names) and all(n.startswith("agent-core-") for n in names)


# --- shutdown_telemetry ---------------------------------------------------------


class _FakeProvider:
    def __init__(self, fail=False):
        self.calls: list[str] = []
        self._fail = fail

    def force_flush(self, *args, **kwargs):
        self.calls.append("force_flush")
        if self._fail:
            raise RuntimeError("collector unreachable")

    def shutdown(self, *args, **kwargs):
        self.calls.append("shutdown")


@pytest.fixture
def installed_provider(monkeypatch):
    def install(provider):
        monkeypatch.setattr(telemetry, "_provider", provider)
        return provider

    yield install


def test_shutdown_flushes_then_stops_the_provider_once(installed_provider):
    provider = installed_provider(_FakeProvider())

    telemetry.shutdown_telemetry()
    telemetry.shutdown_telemetry()

    assert provider.calls == ["force_flush", "shutdown"]


def test_shutdown_without_telemetry_configured_is_a_no_op(installed_provider):
    installed_provider(None)

    telemetry.shutdown_telemetry()  # must not raise


def test_a_collector_that_is_down_never_turns_a_finished_job_into_a_failure(installed_provider):
    provider = installed_provider(_FakeProvider(fail=True))

    telemetry.shutdown_telemetry()  # must not raise

    assert "shutdown" in provider.calls, "the provider is still stopped when the flush fails"


# --- against a real exporter -------------------------------------------------------


class _Collector(BaseHTTPRequestHandler):
    received: list[str] = []

    def do_POST(self):  # noqa: N802 - http.server's required name
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        type(self).received.append(self.path)
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


@pytest.fixture
def collector(monkeypatch):
    _Collector.received = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Collector)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(telemetry, "OTEL_EXPORTER_OTLP_ENDPOINT", f"http://127.0.0.1:{server.server_port}")
    yield _Collector
    server.shutdown()
    server.server_close()


def test_a_short_lived_provider_sends_nothing_until_it_is_flushed(collector):
    """The reason configuring telemetry is not enough for a job: the exporter's
    own timer is 15 s, longer than most of these jobs run."""
    provider = telemetry._build_provider("agent-core-test-job")
    provider.get_meter("test").create_counter("scheduled_job_total").add(1)

    assert collector.received == [], "nothing is pushed before the periodic timer or a flush"

    provider.force_flush()

    assert collector.received == ["/v1/metrics"]
    provider.shutdown()


def test_shutdown_telemetry_delivers_a_jobs_counter_before_exit(collector, monkeypatch):
    provider = telemetry._build_provider("agent-core-test-job")
    provider.get_meter("test").create_counter("scheduled_job_total").add(3)
    monkeypatch.setattr(telemetry, "_provider", provider)

    telemetry.shutdown_telemetry()

    assert collector.received.count("/v1/metrics") >= 1
