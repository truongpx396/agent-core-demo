"""Fixtures for the billing package's own tests. Nothing here imports the application that hosts the package.

`metric_value` is how a test reads a counter: an OpenTelemetry instrument is write-only, so "its current value" only exists as a
reader's collected snapshot. `_METRIC_READER` is installed as the process's meter provider at import, before any test runs.
OpenTelemetry lets that happen once per process (a second `set_meter_provider` is ignored with a warning), which is also why the
repo's own tests and these run in separate processes (spec 011 D21) and why the instruments the package creates at import, against the
API's proxy meter, are replayed onto this provider whichever module is imported first.

Assertions on a counter use before/after deltas, never absolute values: the counters are process-wide and shared by every test.
"""
import pytest
from opentelemetry import metrics as metrics_api
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

_METRIC_READER = InMemoryMetricReader()
metrics_api.set_meter_provider(MeterProvider(metric_readers=[_METRIC_READER]))


def _points(counter):
    data = _METRIC_READER.get_metrics_data()
    if data is None:
        return
    for resource_metrics in data.resource_metrics:
        for scope_metrics in resource_metrics.scope_metrics:
            for metric in scope_metrics.metrics:
                if metric.name == counter.name:
                    yield scope_metrics.scope.name, metric.data.data_points


def metric_value(counter, **labels):
    """Current cumulative value of a `credit_billing.metrics` Counter for a label set: 0 if nothing has been recorded for it."""
    for _scope, points in _points(counter):
        for point in points:
            if dict(point.attributes) == labels:
                return point.value
    return 0


def has_series(counter, **labels) -> bool:
    """Whether a data point EXISTS for the label set, even at 0. `metric_value` cannot tell a series at 0 from no series, and that
    difference is exactly what a bare `increase(...)` alert depends on."""
    return any(dict(point.attributes) == labels for _scope, points in _points(counter) for point in points)


def scopes_of(counter) -> set[str]:
    """The instrumentation-scope names the counter's data points were exported under."""
    return {scope for scope, points in _points(counter) if points}


# --- shared by the tests of every operator command: a database that cannot be reached ----------------------------------

SECRET = "hunter2-do-not-print"


@pytest.fixture
def closed_port_url():
    """A URL to a local port that nothing listens on: the connection is refused at once, as with a stopped database. Carries a user and a
    password (SECRET) so a test can prove neither reaches an error message."""
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    return f"postgresql://billing_user:{SECRET}@127.0.0.1:{port}/appdata"


@pytest.fixture
def fast_failure(monkeypatch):
    """Shorten the connect wait so the test is quick, and keep the bootstrap away from any collector or global meter provider."""
    from credit_billing.cli import _runtime

    monkeypatch.setattr(_runtime, "CONNECT_TIMEOUT_SECONDS", 0.5)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")
