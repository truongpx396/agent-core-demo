"""The profiler is off unless asked for, never stops a process from starting, and never carries a tenant (specs/022).

The native sampler itself was run against a real Pyroscope server in a hardened container when this was written (specs/022 research.md);
what is tested here is the code around it: when it starts, what it is told, and what happens when it cannot.
"""
import sys
from types import SimpleNamespace

import pytest

from app.core.metrics import agent_profiling_start_failures_total as failures
from app.platform import profiling
from tests.conftest import metric_value


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.setattr(profiling, "_started", False)
    monkeypatch.setattr(profiling, "PYROSCOPE_SAMPLE_RATE", 100)
    monkeypatch.setattr(profiling, "AGENT_DOMAIN", "ecorp")


def _sdk(monkeypatch, configure):
    monkeypatch.setitem(sys.modules, "pyroscope", SimpleNamespace(configure=configure))


def test_a_blank_address_is_off_and_the_sdk_is_never_imported(monkeypatch):
    monkeypatch.setattr(profiling, "PYROSCOPE_SERVER_ADDRESS", "")
    monkeypatch.setitem(sys.modules, "pyroscope", None)  # an import now raises ImportError, which would be counted
    before = metric_value(failures, reason="sdk_missing")

    assert profiling.start_profiling("agent-core-api") is False
    assert metric_value(failures, reason="sdk_missing") == before, "a blank address must not even try to import the SDK"


def test_with_an_address_it_configures_the_sdk_once_with_the_service_the_server_and_two_tags(monkeypatch):
    monkeypatch.setattr(profiling, "PYROSCOPE_SERVER_ADDRESS", "http://pyroscope:4040")
    monkeypatch.setattr(profiling, "PYROSCOPE_SAMPLE_RATE", 50)
    monkeypatch.setattr(profiling.socket, "gethostname", lambda: "box-1")
    calls = []
    _sdk(monkeypatch, lambda **kw: calls.append(kw))

    assert profiling.start_profiling("agent-core-agent-worker") is True
    assert profiling.start_profiling("agent-core-agent-worker") is True  # idempotent: a second call does not start a second sampler

    assert calls == [
        {
            "application_name": "agent-core-agent-worker",
            "server_address": "http://pyroscope:4040",
            "sample_rate": 50,
            "tags": {"domain": "ecorp", "replica": "box-1"},
        }
    ]


def test_a_process_serving_several_domains_tags_them_all_instead_of_the_client_default(monkeypatch):
    monkeypatch.setattr(profiling, "PYROSCOPE_SERVER_ADDRESS", "http://pyroscope:4040")
    calls = []
    _sdk(monkeypatch, lambda **kw: calls.append(kw))

    profiling.start_profiling("agent-core-agent-worker", domain="ecorp+support")

    assert calls[0]["tags"]["domain"] == "ecorp+support"


def test_profiles_carry_no_tenant_principal_or_thread(monkeypatch):
    """Every distinct tag value is a new series in the profile store, and a tenant id would put customer names in a UI engineers browse."""
    monkeypatch.setattr(profiling, "PYROSCOPE_SERVER_ADDRESS", "http://pyroscope:4040")
    calls = []
    _sdk(monkeypatch, lambda **kw: calls.append(kw))

    profiling.start_profiling("agent-core-api")

    assert set(calls[0]["tags"]) == {"domain", "replica"}


def test_a_missing_sdk_leaves_the_process_running_and_is_counted(monkeypatch):
    monkeypatch.setattr(profiling, "PYROSCOPE_SERVER_ADDRESS", "http://pyroscope:4040")
    monkeypatch.setitem(sys.modules, "pyroscope", None)
    before = metric_value(failures, reason="sdk_missing")

    assert profiling.start_profiling("agent-core-api") is False
    assert metric_value(failures, reason="sdk_missing") == before + 1


def test_an_sdk_that_raises_leaves_the_process_running_and_is_counted(monkeypatch):
    monkeypatch.setattr(profiling, "PYROSCOPE_SERVER_ADDRESS", "not a url")

    def boom(**_):
        raise RuntimeError("bad address")

    _sdk(monkeypatch, boom)
    before = metric_value(failures, reason="configure_failed")

    assert profiling.start_profiling("agent-core-api") is False
    assert metric_value(failures, reason="configure_failed") == before + 1
    assert profiling._started is False, "a failed start must not be remembered as started"


def test_the_entrypoints_start_it_right_after_telemetry_and_a_short_lived_job_does_not():
    """The failure counter needs the meter provider `configure_telemetry` installs, so the order matters; and a job that lives seconds uploads nothing
    (the SDK sends every 10 s), so `scheduled_job` leaves it out on purpose."""
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    for relative, service in (
        ("app/adapters/inbound/api/main.py", "agent-core-api"),
        ("app/adapters/inbound/workers/agent_worker.py", "agent-core-agent-worker"),
        ("app/adapters/inbound/workers/ingest_worker.py", "agent-core-ingest-worker"),
    ):
        source = (repo / relative).read_text()
        telemetry, start = f'configure_telemetry("{service}")', f'start_profiling("{service}"'
        assert telemetry in source and start in source, relative
        assert source.index(telemetry) < source.index(start), f"{relative}: start_profiling must follow configure_telemetry"
    assert "start_profiling" not in (repo / "app/platform/job_runtime.py").read_text()
