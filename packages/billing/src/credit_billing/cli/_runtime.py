"""What an operator CLI needs to run as its own process, with nothing from any application (spec 011 D24, D11).

The two CLIs (`credit-billing`, `credit-billing-inbox-sweep`) used to borrow the agent app's connection pool and its job
bootstrap. A package another project installs cannot, so each CLI now brings its own, built from the same three environment
variables the app uses, with the same names and defaults: `APPDATA_DATABASE_URL`, `OTEL_EXPORTER_OTLP_ENDPOINT` and
(read by `credit_billing.settings`) the billing variables. A `.env` in the working directory is read as the app reads it.

**The pool.** `wired_pool()` opens one `psycopg_pool` pool, gives the core its `connection` (so `credit_billing.db`
works), and closes it afterwards. `pool.connection()` commits on a clean exit and rolls back on an exception, which is the
contract `credit_billing.db` documents for a provider.

**The job bootstrap.** `scheduled_job()` is what `app/core/job_runtime.py::scheduled_job` is for the app: a CLI lives for
seconds, the OpenTelemetry exporter pushes on a 15 s timer, so a job that finishes sooner would export NOTHING, and the
counters that matter most from an unattended job are its failures (spec 008 B22). So the provider is flushed on the way
out whether the body returned or raised, and a flush that fails never turns a finished job into a failed one. The SDK and
the OTLP exporter are an optional extra (`credit-billing[otel]`): without them, or with a blank endpoint, metrics are
simply not pushed and the CLI works the same. Logging is the standard library's, configured only if the process has not
already configured it, so the CLI takes whatever a host application set up.

Imports nothing from `app` (tests/test_boundary.py).
"""
import logging
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from typing import Any

from opentelemetry import metrics as metrics_api
from psycopg_pool import AsyncConnectionPool
from pydantic_settings import BaseSettings, SettingsConfigDict

from credit_billing import db

logger = logging.getLogger(__name__)


class RuntimeSettings(BaseSettings):
    # `hide_input_in_errors`: the database URL carries a password, and a settings error is printed to a terminal or a log.
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)

    # Same variable and default as the agent app's `appdata_database_url`: the wallet lives in the app's `appdata` database.
    appdata_database_url: str = "postgresql://langfuse:langfuse@localhost:5432/appdata"
    # Same variable and default as the agent app's. Blank turns the push off.
    otel_exporter_otlp_endpoint: str = "http://localhost:4318"


@asynccontextmanager
async def wired_pool(url: str | None = None) -> AsyncIterator[AsyncConnectionPool]:
    """A pool for the length of the block, with the core wired to it."""
    pool = AsyncConnectionPool(url or RuntimeSettings().appdata_database_url, min_size=1, max_size=10, open=False)
    await pool.open(wait=True)
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


@contextmanager
def scheduled_job(service_name: str) -> Iterator[None]:
    """Logging and telemetry up for the job's body, telemetry flushed after it whether it returned or raised.
    `service_name` is the OTel `service.name`, so each job's series is distinguishable on a dashboard."""
    logging.basicConfig(level=logging.INFO)  # does nothing when the process already configured logging
    provider = _install_meter_provider(service_name)
    try:
        yield
    finally:
        _flush(provider)
