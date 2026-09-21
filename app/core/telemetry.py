"""OpenTelemetry metrics SDK wiring — installs the MeterProvider every
Counter/Histogram in app/core/metrics.py records against.

Push, not pull: every instrumented process (the API and each scaled
agent_worker.py/ingest_worker.py replica, pattern 43) pushes metrics via
OTLP to a shared otel-collector, which exposes one aggregated Prometheus
target. A pull-based /metrics endpoint on the API alone (the old design)
could never see a worker's metrics — nothing scrapes a worker process.

Call `configure_telemetry(service_name)` once, at real process startup
(app/api/main.py's `lifespan`, or a worker/channel's `__main__` block) —
NEVER at import time. Unlike `logging_config.configure_logging()` (freely
re-callable), OTel's `set_meter_provider` is call-once; a second call just
warns and is ignored. Keeping this inside `lifespan` (never entered under
pytest, see tests/api/test_api.py) keeps a real network-bound
OTLPMetricExporter from winning that race against a test's own
MeterProvider during `pytest -q`.

Import order vs app/core/metrics.py doesn't matter: `get_meter(...)`
(called at that module's import time) returns a proxy that defers real
instrument creation until a MeterProvider is installed, then replays it.
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

# The OTel SDK's default histogram buckets (0..10000, tuned for millisecond
# web requests) put every real turn in this app's 1-90s range into a single
# bucket — no usable resolution. These instead resolve sub-second tool
# calls up to REQUEST_TIMEOUT_SECONDS (60s default) with headroom.
_LATENCY_BUCKETS_SECONDS = (0.5, 1.0, 2.5, 5.0, 10.0, 15.0, 30.0, 60.0, 90.0, 120.0)
# Matches MAX_ITERATIONS_PER_TURN's realistic range (app/agent/graph.py).
_ITERATION_BUCKETS = (1, 2, 3, 4, 5, 7, 10, 15)

_configured = False


def configure_telemetry(service_name: str) -> None:
    """Idempotent — a second call in the same process is a no-op (this
    process's own guard; OTel's `set_meter_provider` would just warn and
    ignore it anyway, but checking first keeps that warning out of normal
    single-call operation).

    A blank `OTEL_EXPORTER_OTLP_ENDPOINT` skips setup entirely, leaving
    the OTel API's own default no-op MeterProvider in place —
    app/core/metrics.py's `get_meter(...)` proxy already tolerates that
    (see this module's own docstring: it just defers real instrument
    creation forever, never raising). For a real subprocess with no
    otel-collector anywhere nearby to receive anything (see that
    setting's own comment, app/core/config.py) — not something a real
    deployment would ever set."""
    global _configured
    if _configured:
        return
    _configured = True

    if not OTEL_EXPORTER_OTLP_ENDPOINT:
        logger.info("telemetry_disabled", extra={"service_name": service_name})
        return

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
