"""Shared test fixtures.

`mock_search_docs`/`mock_semantic_cache` are autouse: no test in this suite
should reach a live Qdrant/Redis/embeddings backend just because it happens
to invoke `retrieve_context`/`check_semantic_cache`/`write_semantic_cache`
on its way through the graph via build_graph()'s default GraphDeps. Tests
that care about a specific retrieved/cached value inject their own fake
instead — via `GraphDeps(search_docs=fake)`/`GraphDeps(cache_get=fake, ...)`
(build_graph) or `graph.make_retrieve_context_node(fake)`/
`graph.make_check_semantic_cache_node(fake)` (node-level) — which simply
bypasses these defaults.

`TEST_CTX`: a valid SecurityCtx (app/core/security.py) every test that drives a
turn through validate_input needs — route_after_validation fails closed
without one (see graph.py). Each test file imports it directly
(`from tests.conftest import TEST_CTX`) rather than via a pytest fixture,
since most call sites need it as a plain value inside a hand-built
config/state dict, not injected as a test function parameter.

`metric_value`: installs the ONE real `opentelemetry.metrics.MeterProvider`
this whole test session uses (an `InMemoryMetricReader`-backed one) at THIS
module's import time — conftest.py is guaranteed to load before any test
module is collected, so this always wins OTel's one-shot
`set_meter_provider` race (see app/core/telemetry.py's own docstring for
why that race matters — app/core/telemetry.py::configure_telemetry, which
would otherwise compete for that same slot with a real network-bound
exporter, only ever runs from a real process entrypoint, never at import
time, so it never actually enters this race under pytest). Every test file
that asserts on a app/core/metrics.py Counter's value imports this rather
than reaching into OTel/prometheus_client internals directly.
"""
import pytest
from opentelemetry import metrics as metrics_api
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from app.agent import graph

TEST_CTX = {"tenant": "acme", "principal": "test-user", "claims": {}}

_METRIC_READER = InMemoryMetricReader()
metrics_api.set_meter_provider(MeterProvider(metric_readers=[_METRIC_READER]))


def metric_value(counter, **labels):
    """Current cumulative value of a app/core/metrics.py Counter for a given
    label set — 0 if nothing's been recorded for that name/label combo yet.
    OTel instruments are write-only; "current value" only exists via a
    MetricReader's collected snapshot, unlike prometheus_client's Counter,
    which exposed `._value.get()` directly (what every caller of this
    function used before the metrics library swap). Assertions on this
    should always be before/after deltas, never absolute values — these are
    global, process-wide counters shared across the whole test session."""
    data = _METRIC_READER.get_metrics_data()
    if data is None:
        return 0
    for resource_metrics in data.resource_metrics:
        for scope_metrics in resource_metrics.scope_metrics:
            for metric in scope_metrics.metrics:
                if metric.name != counter.name:
                    continue
                for point in metric.data.data_points:
                    if dict(point.attributes) == labels:
                        return point.value
    return 0


@pytest.fixture(autouse=True)
def mock_search_docs(monkeypatch):
    monkeypatch.setattr(graph, "_default_search", lambda query, ctx=None: ("", []))


@pytest.fixture(autouse=True)
def mock_semantic_cache(monkeypatch):
    # Always a miss, and writes are a no-op — a live embedding/Redis call
    # per graph turn would defeat this suite's "no live services" guarantee
    # (see e.g. test_graph_integration.py's module docstring) just as
    # surely as an unmocked search_docs call would.
    monkeypatch.setattr(graph, "_default_cache_get", lambda ctx, query: None)
    monkeypatch.setattr(graph, "_default_cache_set", lambda ctx, query, answer, citations: None)
