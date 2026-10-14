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

    async def open(self, wait=False, timeout=30.0):
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


# --- a database that cannot be reached ---------------------------------------------------------------------------------

SECRET = "hunter2-do-not-print"


@pytest.fixture
def closed_port_url():
    """A URL to a local port that nothing listens on: the connection is refused at once, as with a stopped database."""
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    return f"postgresql://billing_user:{SECRET}@127.0.0.1:{port}/appdata"


@pytest.fixture
def fast_failure(monkeypatch):
    """Shorten the wait so the test is quick, and keep the bootstrap away from any collector or global meter provider."""
    monkeypatch.setattr(_runtime, "CONNECT_TIMEOUT_SECONDS", 0.5)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")


async def test_an_unreachable_database_raises_one_clear_error_that_never_carries_the_credentials(closed_port_url, fast_failure, monkeypatch):
    monkeypatch.setattr(db, "_provider", None)

    with pytest.raises(_runtime.DatabaseUnavailable) as raised:
        async with _runtime.wired_pool(closed_port_url):
            raise AssertionError("the block must not run: there is no database")

    message = str(raised.value)
    assert "127.0.0.1" in message and "/appdata" in message and "APPDATA_DATABASE_URL" in message
    assert SECRET not in message and "billing_user" not in message, "the error is printed to a terminal or a log"
    assert db._provider is None, "the core was never wired to a pool that does not work"


async def test_the_error_is_raised_in_about_the_configured_wait_not_psycopgs_thirty_seconds(closed_port_url, fast_failure):
    import time

    started = time.monotonic()
    with pytest.raises(_runtime.DatabaseUnavailable):
        async with _runtime.wired_pool(closed_port_url):
            pass

    assert time.monotonic() - started < 5, "the wait is CONNECT_TIMEOUT_SECONDS, plus the time to stop the pool"


def test_the_default_wait_is_short_enough_for_an_operator_and_a_cron_job():
    assert _runtime.CONNECT_TIMEOUT_SECONDS <= 15, "psycopg's own default is 30 s"


def test_a_url_that_does_not_parse_is_not_echoed():
    assert _runtime._where("not a url :: with a secret") == "the configured database"
    assert _runtime._where("postgresql://u:p@db.internal:6543/ledger") == "db.internal:6543/ledger"
    assert _runtime._where("postgresql://u:p@/ledger") == "(default socket):5432/ledger"


def test_credit_billing_exits_3_with_one_error_line_and_no_traceback(closed_port_url, fast_failure, monkeypatch, capsys):
    from credit_billing.cli import credits as credits_cli

    monkeypatch.setenv("APPDATA_DATABASE_URL", closed_port_url)

    with pytest.raises(SystemExit) as exited:
        credits_cli.main(["show", "--tenant", "acme"])

    captured = capsys.readouterr()
    assert exited.value.code == _runtime.EXIT_DATABASE_UNAVAILABLE == 3
    assert captured.err.startswith("error: cannot open a connection to 127.0.0.1:")
    assert "Traceback" not in captured.err and SECRET not in captured.err + captured.out
    assert captured.out == "", "nothing is printed as a result when nothing could be read"


def test_the_inbox_sweep_exits_3_with_one_error_line_and_no_traceback(closed_port_url, fast_failure, monkeypatch, capsys):
    from credit_billing.cli import inbox_sweep

    monkeypatch.setenv("APPDATA_DATABASE_URL", closed_port_url)

    with pytest.raises(SystemExit) as exited:
        inbox_sweep.main()

    captured = capsys.readouterr()
    assert exited.value.code == 3
    assert captured.err.startswith("error: cannot open a connection to 127.0.0.1:")
    assert "Traceback" not in captured.err and SECRET not in captured.err + captured.out


async def test_a_pool_that_never_connected_is_still_closed_and_the_core_stays_unwired(monkeypatch):
    """Its connect-retry workers must be stopped, or they keep running behind the error."""
    from psycopg_pool import PoolTimeout

    class NeverConnects(FakePool):
        async def open(self, wait=False, timeout=30.0):
            self.events.append(("open", wait, timeout))
            raise PoolTimeout("pool initialization incomplete")

    FakePool.instances.clear()
    monkeypatch.setattr(_runtime, "AsyncConnectionPool", NeverConnects)
    monkeypatch.setattr(db, "_provider", None)

    with pytest.raises(_runtime.DatabaseUnavailable):
        async with _runtime.wired_pool("postgresql://u:p@h/appdata"):
            pass

    assert FakePool.instances[0].events == [("open", True, _runtime.CONNECT_TIMEOUT_SECONDS), "close"]
    assert db._provider is None


# --- the log line keeps its fields -------------------------------------------------------------------------------------

def _record(message="credit_operator_action", level=20, exc_info=None, **extra):
    import logging

    record = logging.LogRecord("credit_billing.cli.credits", level, __file__, 1, message, None, exc_info)
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_a_record_is_one_json_line_with_the_keys_the_agent_apps_logs_have_and_every_extra():
    import json
    from decimal import Decimal

    line = _runtime.JsonFormatter().format(
        _record(action="grant", tenant="acme", actor="operator:alice", amount=Decimal("500"), key="k-1")
    )

    entry = json.loads(line)
    assert "\n" not in line
    assert entry["event"] == "credit_operator_action" and entry["level"] == "info" and entry["logger"] == "credit_billing.cli.credits"
    assert entry["timestamp"].endswith("Z") and "T" in entry["timestamp"]
    assert (entry["action"], entry["tenant"], entry["actor"], entry["key"]) == ("grant", "acme", "operator:alice", "k-1")
    assert entry["amount"] == "500", "a Decimal is written as its str, never as a lossy float"


def test_an_extra_cannot_overwrite_a_standard_key():
    import json

    entry = json.loads(_runtime.JsonFormatter().format(_record(level=40, event="forged", level_name="x", logger="forged", timestamp="forged")))

    assert entry["event"] == "credit_operator_action" and entry["level"] == "error" and entry["logger"] == "credit_billing.cli.credits"
    assert entry["timestamp"] != "forged"


def test_an_exception_is_carried_in_the_line_not_dropped():
    import json
    import sys

    try:
        raise ValueError("the reason")
    except ValueError:
        record = _record("job_failed", level=40, exc_info=sys.exc_info())

    entry = json.loads(_runtime.JsonFormatter().format(record))

    assert "ValueError: the reason" in entry["exception"] and entry["level"] == "error"


def test_the_job_bootstrap_installs_the_json_formatter_unless_logging_is_already_configured(installed, monkeypatch):
    import logging

    root = logging.getLogger()
    keep_handlers, keep_level = list(root.handlers), root.level
    installed(None)
    try:
        root.handlers[:] = []
        with _runtime.scheduled_job("a-cli"):
            pass
        assert len(root.handlers) == 1 and isinstance(root.handlers[0].formatter, _runtime.JsonFormatter)
        assert root.level == logging.INFO

        already = logging.NullHandler()
        root.handlers[:] = [already]
        with _runtime.scheduled_job("a-cli"):
            pass
        assert root.handlers == [already], "a process that configured logging keeps exactly what it set up"
    finally:
        root.handlers[:] = keep_handlers
        root.setLevel(keep_level)
