"""What the operator CLIs need to run as their own process: a pool wired to the core, and a job bootstrap that flushes on the way
out (specs/011-billing-module-boundary D24, D11).

The point of the bootstrap is the failure case. A CLI lives for seconds and the exporter pushes on a 15 s timer, so a job that
finishes sooner exports NOTHING; the counters that matter most from an unattended job are its failures (spec 008 B22). So the flush
happens in a `finally`, and a flush that itself fails must never turn a finished job into a failed one.
"""
import pytest

from credit_billing import db
from credit_billing.cli import _runtime


class FakeProvider:
    def __init__(self, flush_error: Exception | None = None, shutdown_error: Exception | None = None) -> None:
        self.calls: list[str] = []
        self._flush_error, self._shutdown_error = flush_error, shutdown_error

    def force_flush(self):
        self.calls.append("flush")
        if self._flush_error:
            raise self._flush_error

    def shutdown(self):
        self.calls.append("shutdown")
        if self._shutdown_error:
            raise self._shutdown_error


@pytest.fixture
def installed(monkeypatch):
    """Makes `scheduled_job` install the given provider; returns the list of service names it was asked for."""
    asked: list[str] = []

    def install(provider):
        def _install(service_name):
            asked.append(service_name)
            return provider

        monkeypatch.setattr(_runtime, "_install_meter_provider", _install)
        return asked

    return install


def test_the_provider_is_flushed_and_shut_down_when_the_job_returns(installed):
    provider = FakeProvider()
    asked = installed(provider)

    with _runtime.scheduled_job("a-cli"):
        pass

    assert asked == ["a-cli"] and provider.calls == ["flush", "shutdown"]


def test_the_provider_is_flushed_when_the_job_raises_and_the_error_still_propagates(installed):
    provider = FakeProvider()
    installed(provider)

    with pytest.raises(RuntimeError, match="the job failed"), _runtime.scheduled_job("a-cli"):
        raise RuntimeError("the job failed")

    assert provider.calls == ["flush", "shutdown"], "a failed job's counters are exactly what must reach the collector"


def test_a_flush_that_fails_never_fails_the_job_and_shutdown_is_still_attempted(installed):
    provider = FakeProvider(flush_error=OSError("collector unreachable"), shutdown_error=OSError("still unreachable"))
    installed(provider)

    with _runtime.scheduled_job("a-cli"):
        pass  # returns normally

    assert provider.calls == ["flush", "shutdown"]


def test_with_no_provider_the_job_runs_and_there_is_nothing_to_flush(installed):
    installed(None)

    with _runtime.scheduled_job("a-cli"):
        pass


def test_a_blank_endpoint_turns_the_push_off(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "  ")

    assert _runtime._install_meter_provider("a-cli") is None


def test_without_the_sdk_installed_the_cli_still_works_and_pushes_nothing(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:9")
    monkeypatch.setitem(__import__("sys").modules, "opentelemetry.sdk.metrics", None)  # makes `from opentelemetry.sdk.metrics import ...` raise ImportError

    assert _runtime._install_meter_provider("a-cli") is None


def test_with_an_endpoint_a_provider_is_installed_with_the_service_name(monkeypatch):
    installed_globally = []
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:9")  # the discard port: nothing is ever delivered
    monkeypatch.setattr(_runtime.metrics_api, "set_meter_provider", installed_globally.append)  # the real one is install-once per process

    from opentelemetry.sdk.resources import Resource

    real_create, resources = Resource.create, []

    def create(attributes=None, *args, **kwargs):  # the SDK's provider does not expose its resource, so record what the code asked for
        resources.append(dict(attributes or {}))
        return real_create(attributes, *args, **kwargs)

    monkeypatch.setattr(Resource, "create", staticmethod(create))

    provider = _runtime._install_meter_provider("agent-core-credits-cli")
    try:
        assert installed_globally == [provider]
        assert resources == [{"service.name": "agent-core-credits-cli"}]
    finally:
        provider.shutdown()


class FakePool:
    instances: list["FakePool"] = []

    def __init__(self, url, **kwargs):
        self.url, self.kwargs, self.events = url, kwargs, []
        FakePool.instances.append(self)

    async def open(self, wait=False):
        self.events.append("open")

    async def close(self):
        self.events.append("close")

    def connection(self):
        raise AssertionError("not used")


async def test_the_pool_is_wired_to_the_package_for_the_block_and_unwired_and_closed_after(monkeypatch):
    FakePool.instances.clear()
    monkeypatch.setattr(_runtime, "AsyncConnectionPool", FakePool)
    monkeypatch.setattr(db, "_provider", None)

    async with _runtime.wired_pool("postgresql://u:p@h/appdata") as pool:
        assert pool.url == "postgresql://u:p@h/appdata"
        assert db._provider == pool.connection, "the package's connections now come from this pool"
        assert pool.events == ["open"]

    assert db._provider is None and pool.events == ["open", "close"]


async def test_the_pool_is_unwired_and_closed_even_when_the_block_raises(monkeypatch):
    FakePool.instances.clear()
    monkeypatch.setattr(_runtime, "AsyncConnectionPool", FakePool)
    monkeypatch.setattr(db, "_provider", None)

    with pytest.raises(RuntimeError, match="boom"):
        async with _runtime.wired_pool("postgresql://u:p@h/appdata"):
            raise RuntimeError("boom")

    assert db._provider is None and FakePool.instances[0].events == ["open", "close"]


def test_the_database_url_and_collector_defaults_are_the_agent_apps(monkeypatch):
    """Same variables and same defaults as the application's settings, so `make credits` behaves as it always did."""
    for name in ("APPDATA_DATABASE_URL", "OTEL_EXPORTER_OTLP_ENDPOINT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir("/")  # no `.env` to read

    settings = _runtime.RuntimeSettings()

    assert settings.appdata_database_url == "postgresql://langfuse:langfuse@localhost:5432/appdata"
    assert settings.otel_exporter_otlp_endpoint == "http://localhost:4318"
