"""What an operator CLI needs to run as its own process, with nothing from any application (spec 011 D24, D11).

The two CLIs (`credit-billing`, `credit-billing-inbox-sweep`) used to borrow the agent app's connection pool
and job bootstrap. A package another project installs can't, so each CLI brings its own, built from the same
environment variables the app uses, with the same names and defaults: `APPDATA_DATABASE_URL`,
`OTEL_EXPORTER_OTLP_ENDPOINT` and the billing variables (read by `credit_billing.settings`). A `.env` in the
working directory is read as the app reads it.

**The pool.** `wired_pool()` opens one `psycopg_pool` pool, gives the core its `connection` (so
`credit_billing.db` works), and closes it afterwards. `pool.connection()` commits on a clean exit and rolls
back on an exception, the contract `credit_billing.db` documents for a provider.

**An unreachable database fails fast and says so.** The pool retries a refused connection until its open
timeout, and psycopg's default is 30 s of multi-line warnings ending in a raw `PoolTimeout` traceback. That's
the wrong failure for a command an operator types or an unwatched cron job. So the wait is
`CONNECT_TIMEOUT_SECONDS`, and the error is `DatabaseUnavailable`, naming host, port and database (never user
or password) and the variable to check. Both CLIs turn it into one `error:` line and exit status
`EXIT_DATABASE_UNAVAILABLE`.

**The job bootstrap.** `scheduled_job()` is the CLI's `app/core/job_runtime.py::scheduled_job`. A CLI lives
for seconds but the OpenTelemetry exporter pushes on a 15 s timer, so a faster job would export NOTHING, and
an unattended job's most important counters are its failures (spec 008 B22). So the provider is flushed on the
way out whether the body returned or raised, and a failing flush never turns a finished job into a failed one.
- The SDK and OTLP exporter are an optional extra (`credit-billing[otel]`); without them, or with a blank
  endpoint, metrics are simply not pushed and the CLI works the same.
- Logging is the standard library's, configured only if the process hasn't already, so the CLI takes whatever
  a host application set up. When the CLI does configure it, a record is one JSON line on stderr with the
  agent app's keys (`timestamp`, `level`, `logger`, `event`) plus every `extra=` field. The operator audit
  line (`credit_operator_action`: action, tenant, actor, amount, key) is only useful with its fields, which
  the default format drops.

Imports nothing from `app` (tests/test_boundary.py).
"""
import json
import logging
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from datetime import UTC, datetime
from typing import Any

import psycopg
from opentelemetry import metrics as metrics_api
from psycopg.conninfo import conninfo_to_dict
from psycopg_pool import AsyncConnectionPool, PoolTimeout
from pydantic_settings import BaseSettings, SettingsConfigDict

from credit_billing import db

logger = logging.getLogger(__name__)

# How long a CLI waits for its first connection. Generous for a connect (a healthy database answers in milliseconds, a distant one in
# well under a second) and short enough that an operator who mistyped the URL, or a cron job whose database is down, is told within
# seconds rather than half a minute.
CONNECT_TIMEOUT_SECONDS = 10.0
# The exit status both CLIs use when the database cannot be reached. `credit-billing` already uses 0 (done, or a duplicate), 1
# (nothing changed / no wallet) and 2 (invalid input); a fourth, distinct value lets a caller tell "retry later" from "fix the command".
EXIT_DATABASE_UNAVAILABLE = 3


class RuntimeSettings(BaseSettings):
    # `hide_input_in_errors`: the database URL carries a password, and a settings error is printed to a terminal or a log.
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)

    # Same variable and default as the agent app's `appdata_database_url`: the wallet lives in the app's `appdata` database.
    appdata_database_url: str = "postgresql://langfuse:langfuse@localhost:5432/appdata"
    # Same variable and default as the agent app's. Blank turns the push off.
    otel_exporter_otlp_endpoint: str = "http://localhost:4318"


class DatabaseUnavailable(RuntimeError):
    """No connection could be opened within `CONNECT_TIMEOUT_SECONDS`. The message names where, never who: it carries no user or password."""


def _where(url: str) -> str:
    """`host:port/database` of a connection URL, without its credentials; a URL that does not parse is not echoed at all."""
    try:
        parts = conninfo_to_dict(url)
    except psycopg.ProgrammingError:
        return "the configured database"
    return f"{parts.get('host') or '(default socket)'}:{parts.get('port') or '5432'}/{parts.get('dbname') or '?'}"


@asynccontextmanager
async def wired_pool(url: str | None = None) -> AsyncIterator[AsyncConnectionPool]:
    """A pool for the length of the block, with the core wired to it. Raises `DatabaseUnavailable` if it cannot connect in time."""
    target = url or RuntimeSettings().appdata_database_url
    pool = AsyncConnectionPool(target, min_size=1, max_size=10, open=False)
    try:
        await pool.open(wait=True, timeout=CONNECT_TIMEOUT_SECONDS)
    except PoolTimeout as exc:
        await pool.close()  # stop its connect-retry workers; otherwise they keep running behind the error
        raise DatabaseUnavailable(
            f"cannot open a connection to {_where(target)} within {CONNECT_TIMEOUT_SECONDS:g}s; "
            "check APPDATA_DATABASE_URL (host, port, database, credentials) and that the server is up"
        ) from exc
    db.configure(connection=pool.connection)
    try:
        yield pool
    finally:
        db.configure(connection=None)
        await pool.close()


def _install_meter_provider(service_name: str) -> Any | None:
    """The OTLP-exporting provider, installed globally, or None when there is nothing to push to or no SDK to push with."""
    endpoint = RuntimeSettings().otel_exporter_otlp_endpoint.strip()
    if not endpoint:
        return None
    try:
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
            OTLPMetricExporter,
        )
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.resources import Resource
    except ImportError:
        logger.info("telemetry_sdk_not_installed", extra={"hint": "pip install 'credit-billing[otel]' to push metrics"})
        return None
    reader = PeriodicExportingMetricReader(
        OTLPMetricExporter(endpoint=f"{endpoint.rstrip('/')}/v1/metrics"), export_interval_millis=15000
    )
    provider = MeterProvider(resource=Resource.create({"service.name": service_name}), metric_readers=[reader])
    metrics_api.set_meter_provider(provider)
    return provider


def _flush(provider: Any | None) -> None:
    """Push what was recorded, then stop the exporter. Idempotent, a no-op without a provider, and never raises: an
    unreachable collector must not turn a finished job into a failed one."""
    if provider is None:
        return
    try:
        provider.force_flush()
    except Exception as exc:  # noqa: BLE001 - a flush failing must not fail the job
        logger.warning("telemetry_flush_failed", extra={"error_class": type(exc).__name__})
    try:
        provider.shutdown()
    except Exception as exc:  # noqa: BLE001 - same
        logger.warning("telemetry_shutdown_failed", extra={"error_class": type(exc).__name__})


# Every attribute a LogRecord has of its own; anything else on a record arrived through `extra=` and belongs in the log line.
_RECORD_FIELDS = frozenset(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    """One JSON object per record: `timestamp` (UTC, ISO 8601), `level`, `logger`, `event`, every `extra=` field, and `exception`
    when there is one. The four standard keys win over an extra of the same name; a value JSON cannot encode is written as its `str`."""

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {key: value for key, value in vars(record).items() if key not in _RECORD_FIELDS}
        entry.update(
            timestamp=datetime.fromtimestamp(record.created, UTC).isoformat(timespec="microseconds").replace("+00:00", "Z"),
            level=record.levelname.lower(),
            logger=record.name,
            event=record.getMessage(),
        )
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def _configure_logging() -> None:
    """JSON lines on stderr at INFO, unless the process already configured logging (then it is left exactly as it is)."""
    root = logging.getLogger()
    if root.handlers:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    root.setLevel(logging.INFO)


@contextmanager
def scheduled_job(service_name: str) -> Iterator[None]:
    """Logging and telemetry up for the job's body, telemetry flushed after it whether it returned or raised.
    `service_name` is the OTel `service.name`, so each job's series is distinguishable on a dashboard."""
    _configure_logging()
    provider = _install_meter_provider(service_name)
    try:
        yield
    finally:
        _flush(provider)
