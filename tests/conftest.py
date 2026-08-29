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

`mock_appdata_postgres` is the equivalent guarantee for the third live
service (`appdata` Postgres, app/agent/sql_store.py) — a gap this suite
had until it was found the hard way: `app/agent/runtime.py::stream_turn`/
`answer` call `_tenant_over_daily_budget`/`_upsert_session` UNCONDITIONALLY
on every turn (`meter.usage_summary`/`sessions.upsert_session` underneath),
and `_record_turn_metrics` calls `meter.record_usage` on every COMPLETED
one — all three already degrade gracefully on a connection FAILURE (each
has its own try/except, independently tested — see
tests/agent/test_tenant_budget.py/test_sessions.py), but none of them were
ever meant to degrade gracefully from a slow, real TCP connection attempt
with no Postgres listening (CI has never run one for this job — see
.github/workflows/ci.yml's own comment). Without this fixture, every test
that drives a real `stream_turn`/`answer` call pays a real psycopg
connection-pool timeout on each of those calls — individually survivable,
but stacked across a turn (and across the whole suite) it was pushing
individual turns past their own REQUEST_TIMEOUT_SECONDS and the whole
suite from ~10s (locally, against a real docker-compose Postgres) to
~20+ minutes in CI (see GRAPH_PATTERNS.md pattern 46's note on the
recursion_limit fix found the same way).

Patched at `meter.get_connection`/`sessions.get_connection` — each
module's OWN `from app.agent.sql_store import get_connection` binding, not
`sql_store.get_connection` itself (a `from X import Y` binding is a
separate reference; patching the origin module wouldn't reach it) — and
specifically NOT the higher-level functions themselves
(`_tenant_over_daily_budget`, `usage_summary`, `upsert_session`,
`record_usage`), because tests/agent/test_tenant_budget.py and
tests/agent/test_sessions.py test several of those AS the function under
test, monkeypatching `get_connection` locally to inject their own fake —
this fixture's patch is simply overridden by theirs within the same test,
so both guarantees hold together. Raises immediately rather than trying
to fabricate a query-shape-correct fake row for every possible query this
could ever run — every call site already independently degrades on a
connection FAILURE by design, so this exercises that same, already-tested
real path instead of inventing a new one.

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


class _NoPostgresInTests(Exception):
    """Raised by `mock_appdata_postgres` instead of letting `get_connection()`
    attempt a real (and, without a live Postgres, slow-to-time-out) TCP
    connection — see this module's docstring."""


def _no_postgres_in_tests():
    raise _NoPostgresInTests(
        "appdata Postgres is not available in this test session — every "
        "caller of get_connection() must already degrade gracefully on a "
        "connection failure by design; see tests/conftest.py's docstring."
    )


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


@pytest.fixture(autouse=True)
def mock_appdata_postgres(monkeypatch):
    from app.agent import meter, sessions

    monkeypatch.setattr(meter, "get_connection", _no_postgres_in_tests)
    monkeypatch.setattr(sessions, "get_connection", _no_postgres_in_tests)
