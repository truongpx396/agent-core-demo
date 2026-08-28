"""OpenTelemetry metrics SDK wiring — installs the MeterProvider every
Counter/Histogram in app/core/metrics.py ultimately records against.

Push, not pull: unlike the old prometheus_client-based `GET /metrics`
endpoint this replaces, every instrumented process — the API AND each
independently-scaled app/turns/agent_worker.py / app/ingestion/ingest_worker.py
replica (GRAPH_PATTERNS.md pattern 43) — pushes its own metrics via OTLP to
a shared otel-collector (docker-compose.observability.yml), which exposes
ONE aggregated Prometheus scrape target. A pull-based /metrics endpoint on
the API alone (the old design) could never see a worker process's metrics
at all: nothing scrapes a worker, since it binds no HTTP port.

Call `configure_telemetry(service_name)` once, at real process startup —
app/api/main.py's `lifespan`, or an `if __name__ == "__main__":` block for a
worker/channel entrypoint — NEVER at bare module-import time. That
distinction matters here in a way it doesn't for app/core/logging_config.py's
`configure_logging()` (freely re-callable, last call wins): OTel's
`set_meter_provider` is call-once — a second call is silently ignored (just
a warning), verified empirically while building this. Since pytest imports
app/api/main.py (tests/api/test_api.py, `from app.api import main as api`)
but never actually enters `lifespan` (no test uses `TestClient` as a context
manager — see that test module's own docstring for why), keeping this call
inside `lifespan` rather than at import time is what keeps a real,
network-bound OTLPMetricExporter from ever winning that one-shot race
against a test's own MeterProvider during `pytest -q`.

Import order between this module and app/core/metrics.py doesn't matter:
`opentelemetry.metrics.get_meter(...)` (called at app/core/metrics.py's
import time, before this ever runs) returns a proxy that defers real
instrument creation until a MeterProvider is installed, then replays it —
also verified empirically, not just asserted from the spec.
"""
import logging

from opentelemetry import metrics as metrics_api
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.metrics.view import ExplicitBucketHistogramAggregation, View
from opentelemetry.sdk.resources import Resource

from app.core.config import OTEL_EXPORTER_OTLP_ENDPOINT

logger = logging.getLogger(__name__)

# The OTel SDK's own default histogram bucket boundaries (0..10000, tuned
# for millisecond-scale web request durations) put every real turn in this
# app's typical 1-90s range into a single le="10000" bucket — no usable
# resolution at all (verified empirically against a real collector before
# adding these Views). These match the buckets this app actually cares
# about resolving: sub-second tool calls up to REQUEST_TIMEOUT_SECONDS
# (app/agent/runtime.py, 60s default) with headroom.
_LATENCY_BUCKETS_SECONDS = (0.5, 1.0, 2.5, 5.0, 10.0, 15.0, 30.0, 60.0, 90.0, 120.0)
# Matches MAX_ITERATIONS_PER_TURN's realistic range (app/agent/graph.py).
_ITERATION_BUCKETS = (1, 2, 3, 4, 5, 7, 10, 15)

_configured = False


def configure_telemetry(service_name: str) -> None:
    """Idempotent — a second call in the same process is a no-op (this
    process's own guard; OTel's `set_meter_provider` would just warn and
    ignore it anyway, but checking first keeps that warning out of normal
    single-call operation)."""
    global _configured
    if _configured:
        return
    _configured = True

    endpoint = f"{OTEL_EXPORTER_OTLP_ENDPOINT.rstrip('/')}/v1/metrics"
    exporter = OTLPMetricExporter(endpoint=endpoint)
    reader = PeriodicExportingMetricReader(exporter, export_interval_millis=15000)

    views = [
        View(
            instrument_name="agent_latency_seconds",
            aggregation=ExplicitBucketHistogramAggregation(
                boundaries=_LATENCY_BUCKETS_SECONDS
            ),
        ),
        View(
            instrument_name="agent_iterations",
            aggregation=ExplicitBucketHistogramAggregation(
                boundaries=_ITERATION_BUCKETS
            ),
        ),
    ]

    provider = MeterProvider(
        resource=Resource.create({"service.name": service_name}),
        metric_readers=[reader],
        views=views,
    )
    metrics_api.set_meter_provider(provider)
    logger.info(
        "telemetry_configured",
        extra={"service_name": service_name, "otlp_endpoint": endpoint},
    )
