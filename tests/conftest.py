"""Shared test fixtures.

`mock_search_docs`/`mock_semantic_cache` are autouse: no test in this suite
should reach a live Qdrant/Redis/embeddings backend just because it happens
to invoke `retrieve_context`/`check_semantic_cache`/`write_semantic_cache`
on its way through the graph via build_graph()'s default GraphDeps. Tests
that care about a specific retrieved/cached value inject their own fake
instead — via `GraphDeps(search_docs=fake)`/`GraphDeps(cache_get=fake, ...)`
(build_graph) or `graph_retrieval.make_retrieve_context_node(fake)`/
`graph_cache.make_check_semantic_cache_node(fake)` (node-level) — which simply
bypasses these defaults.

`mock_model_resolver` is the same guarantee for `usage_ledger.record_usage`'s
model-alias lookup against LiteLLM (see that fixture).

`mock_ml_moderation` is the same guarantee for `app/agent/moderation.py`'s
ML injection-classifier layer: `moderate_input` runs on every full-graph
turn, unconditionally, and `moderation.screen`'s ML layer is a real HTTP
call to `ml-service` (app/core/config.py's `ML_SERVICE_URL`) when a
pattern doesn't already catch the input first. Real bug, found live: this
suite passed cleanly whenever `ml-service` happened to be unreachable
(fails open, same as no mock at all) but started failing real, unrelated
tests (test_manifest.py's leak-detection proof, among others) the moment a
real `ml-service` container was left running locally — ordinary test
phrases like "what are your instructions?" scored above the real model's
own malicious threshold, blocking turns those tests never expected
blocked. A test's outcome must not depend on which containers happen to be
up on the machine running it. Tests that care about the ML layer
specifically (tests/agent/test_moderation.py's own TestMlInjectionLayer)
override this fixture's patch locally, same override relationship
mock_search_docs/mock_semantic_cache already have with their own
exceptions.

`mock_appdata_postgres` is the equivalent guarantee for the third live
service (`appdata` Postgres, app/agent/sql_store.py) — a gap this suite
had until it was found the hard way: `app/agent/runtime_stream.py::astream_events_turn`
calls `_check_allowance`/`_upsert_session` UNCONDITIONALLY
on every turn (`spend.usage_summary`/`sessions.upsert_session` underneath),
and `_record_turn_metrics` calls `usage_ledger.record_usage` on every COMPLETED
one — all three already degrade gracefully on a connection FAILURE (each
has its own try/except, independently tested — see
tests/agent/test_tenant_budget.py/test_sessions.py), but none of them were
ever meant to degrade gracefully from a slow, real TCP connection attempt
with no Postgres listening (CI has never run one for this job — see
.github/workflows/ci.yml's own comment). Without this fixture, every test
that drives a real `astream_events_turn` call pays a real psycopg
connection-pool timeout on each of those calls — individually survivable,
but stacked across a turn (and across the whole suite) it was pushing
individual turns past their own REQUEST_TIMEOUT_SECONDS and the whole
suite from ~10s (locally, against a real docker-compose Postgres) to
~20+ minutes in CI (see GRAPH_PATTERNS.md pattern 46's note on the
recursion_limit fix found the same way).

Patched at `usage_ledger.get_connection`/`spend.get_connection`/`sessions.get_connection`/
`tool_idempotency.get_connection` — each module's OWN
`from app.agent.sql_store import get_connection` binding, not
`sql_store.get_connection` itself (a `from X import Y` binding is a
separate reference; patching the origin module wouldn't reach it) — and
specifically NOT the higher-level functions themselves
(`_check_allowance`, `usage_summary`, `upsert_session`,
`record_usage`, `idempotent`), because tests/agent/test_tenant_budget.py,
tests/agent/test_sessions.py, and tests/agent/test_tool_idempotency.py test
several of those AS the function under test, monkeypatching `get_connection`
locally to inject their own fake — this fixture's patch is simply
overridden by theirs within the same test, so both guarantees hold
together. Raises immediately rather than trying to fabricate a
query-shape-correct fake row for every possible query this could ever run —
every call site already independently degrades on a connection FAILURE by
design, so this exercises that same, already-tested real path instead of
inventing a new one.

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

`pytest_sessionfinish` (bottom of this file) is the cross-worker teardown
point for `tests/containers.py`'s Docker-backed real services — see that
module's own docstring.
"""
import pytest
from opentelemetry import metrics as metrics_api
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from app.agent import graph, moderation

TEST_CTX = {"tenant": "ecorp", "principal": "test-user", "claims": {}}

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


async def _no_op_search(query, ctx=None):
    return "", []


@pytest.fixture(autouse=True)
def mock_search_docs(monkeypatch):
    # `_default_search` is `async def` now (awaits `tools.gather_context`,
    # which awaits the reranker's HTTP call — see app/agent/graph.py) — the
    # fake needs to be awaitable too, not a plain lambda.
    monkeypatch.setattr(graph, "_default_search", _no_op_search)


async def _benign_ml_score(text: str) -> float:
    return 0.0


@pytest.fixture(autouse=True)
def mock_ml_moderation(monkeypatch):
    monkeypatch.setattr(moderation, "_ml_malicious_score", _benign_ml_score)


@pytest.fixture(autouse=True)
def mock_semantic_cache(monkeypatch):
    # Always a miss, and writes are a no-op — a live embedding/Redis call
    # per graph turn would defeat this suite's "no live services" guarantee
    # (see e.g. test_graph_integration.py's module docstring) just as
    # surely as an unmocked search_docs call would. Both are `async def`
    # now — check_semantic_cache/write_semantic_cache await `cache_get`/
    # `cache_set` directly (app/retrieval/semantic_cache.py's real
    # implementations await a real `redis.asyncio.Redis` client), so a
    # plain sync lambda here would raise "NoneType can't be used in
    # 'await' expression" the instant any graph test actually reached
    # either node.
    async def fake_cache_get(ctx, query):
        return None

    async def fake_cache_set(ctx, query, answer, citations):
        return None

    monkeypatch.setattr(graph, "_default_cache_get", fake_cache_get)
    monkeypatch.setattr(graph, "_default_cache_set", fake_cache_set)


@pytest.fixture(autouse=True)
def mock_appdata_postgres(monkeypatch):
    from app.agent import sessions, spend, tool_idempotency, usage_ledger
    from app.billing import credits

    monkeypatch.setattr(usage_ledger, "get_connection", _no_postgres_in_tests)
    # The spend read behind every dollar cap and GET /usage (specs/010 T030): it moved out of usage_ledger, so it
    # needs its own guard or an ordinary turn test would pay a real connection attempt for it again.
    monkeypatch.setattr(spend, "get_connection", _no_postgres_in_tests)
    # The credit wallet is read before a turn only when CREDITS_ENFORCEMENT is on and on the usage
    # endpoint only when CREDITS_PER_USD is set; both are off in the default world, but a test that
    # turns one on must not reach a real database by forgetting to stub the wallet.
    monkeypatch.setattr(credits, "get_connection", _no_postgres_in_tests)
    monkeypatch.setattr(sessions, "get_connection", _no_postgres_in_tests)
    # tool_idempotency.idempotent() degrades the SAME way (see its own
    # module docstring) — every mutating/outward tool test in this suite
    # exercises that real fail-open path by default, same guarantee as the
    # two modules above, rather than needing its own per-test mock.
    monkeypatch.setattr(tool_idempotency, "get_connection", _no_postgres_in_tests)


@pytest.fixture(autouse=True)
def mock_model_resolver(monkeypatch):
    """`usage_ledger.record_usage` resolves the chat alias to its concrete model
    over HTTP (LiteLLM's `GET /model/info`) on every completed turn. Without
    this, the "hermetic" suite made a real request to whatever answers on the
    configured proxy address — so a result depended on whether a LiteLLM
    happened to be running on the machine, the same class of leak
    `mock_ml_moderation` closes for the ML service. Patches the CALLER's own
    binding (`usage_ledger.resolve_model`), so tests/agent/test_model_resolver.py
    still exercises the real resolver."""

    async def _no_model_resolution(alias):
        return None

    from app.agent import usage_ledger

    monkeypatch.setattr(usage_ledger, "resolve_model", _no_model_resolution)


@pytest.fixture(autouse=True)
def usage_event_sink(monkeypatch):
    """Every model call now writes one usage event (`usage_events.record_call`, via
    `metering.metered_invoke`), which means a real INSERT into `appdata` Postgres and a real
    `resolve_model` HTTP call on the turn path. Left alone, an ordinary agent-node test would pay a
    connection timeout and an HTTP request per call and its result would depend on what is running
    on the machine (the leak `mock_appdata_postgres` and `mock_model_resolver` close for their
    modules). The default world records nothing real and CAPTURES each event row in a list the test
    can read, so "this call was metered" is assertable without a database. The real `_insert`
    statement is proven by tests/agent/test_usage_events.py (fake connection) and
    tests/integration/test_usage_events_real_postgres.py (a real one); both restore it from the
    reference saved at import time, which is before this fixture patches anything."""
    from app.agent import usage_events

    async def _no_model_resolution(alias):
        return None

    captured: list[dict] = []

    async def _capture(row):
        if row["event_id"] in {r["event_id"] for r in captured}:
            return False  # the same duplicate story the real table tells
        captured.append(row)
        return True

    monkeypatch.setattr(usage_events, "resolve_model", _no_model_resolution)
    monkeypatch.setattr(usage_events, "_insert", _capture)
    usage_events.reset_state()
    yield captured
    usage_events.reset_state()


@pytest.fixture(autouse=True)
def mock_budget_policies(monkeypatch):
    """`budgets.check` reads the operator's per-tenant/per-person limit overrides
    (`budget_policies.overrides_for`) before every turn. Without this, an ordinary turn test
    would hit the `budget_policies` table, so its result would depend on whether one exists.
    The default world has no overrides, so every test sees exactly the Settings defaults;
    a test that needs one patches `overrides_for` itself, and
    tests/agent/test_budget_policies.py restores the real function to exercise it. The
    module's per-process cache is cleared on both sides."""
    from app.agent import budget_policies

    async def _no_overrides(tenant, principal):
        return []

    budget_policies.reset_cache()
    monkeypatch.setattr(budget_policies, "overrides_for", _no_overrides)
    yield
    budget_policies.reset_cache()


@pytest.fixture(autouse=True)
def fresh_budget_crossing_log():
    """`budgets` remembers which threshold crossings it has already logged, per process, so a
    tenant sitting at 90% logs once a day instead of on every turn. That memory is process-wide,
    so one test's crossing would silence the same tenant's log line in the next test (and in
    whichever test an xdist worker happens to run after it): cleared on both sides."""
    from app.agent import budgets

    budgets.reset_logged_crossings()
    yield
    budgets.reset_logged_crossings()


@pytest.fixture(autouse=True)
def mock_model_pricing(monkeypatch):
    """`pricing.get_price` reads every model's price from LiteLLM's
    `GET /model/info` on the agent node's hot path, so the same leak
    `mock_model_resolver` closes applies: a test's cost must not depend on
    whether a proxy answers on the configured address. The default world is the
    one this app ships with — a local, free model (LiteLLM reports Ollama as a
    KNOWN $0, not an unknown price) — so an ordinary turn costs nothing and
    never counts as unpriced. A test that needs a price patches
    `pricing._fetch_model_info` itself; tests/agent/test_pricing.py restores the
    real fetch to exercise it over a MockTransport. The module's price cache is
    process-wide, so it is cleared on both sides of every test."""
    from app.agent import pricing
    from app.core.config import CHAT_MODEL

    async def _free_local_model():
        return [
            {
                "model_name": CHAT_MODEL,
                "model_info": {"input_cost_per_token": 0.0, "output_cost_per_token": 0.0},
            }
        ]

    pricing.reset_pricing_state()
    monkeypatch.setattr(pricing, "_fetch_model_info", _free_local_model)
    yield
    pricing.reset_pricing_state()


def pytest_sessionfinish(session, exitstatus):
    """Tears down every Docker container `tests/containers.py::ensure_*()`
    started this run (Postgres/Redis/Qdrant/Ollama, for
    tests/agent/test_durable_checkpoint.py, tests/integration/, tests/live/) —
    see that module's own docstring for why teardown lives here rather than
    each container's default Ryuk-on-process-exit behavior (disabled) or a
    fixture's own finalizer.

    Under pytest-xdist, EVERY worker process runs its own
    `pytest_sessionfinish` as it individually finishes — too early, since
    other workers may still be mid-test against a container this would just
    removed out from under them. `session.config.workerinput` exists ONLY on
    a worker's own config (injected by xdist); its absence means this is
    either the xdist CONTROLLER process (which runs no tests itself and only
    reaches its own `pytest_sessionfinish` after every worker has finished
    and reported back) or a plain, non-distributed run — the one correct
    place, in both cases, to actually tear down.

    Also skips entirely when THIS process never called any `ensure_*()` —
    see `tests.containers.used_shared_cache`'s own docstring for the real
    incident this guards against: an ordinary `pytest -q` run (no live/e2e/
    integration marker selected) still reached this function and tore down
    a real Postgres/Qdrant/Ollama a SEPARATE, concurrently-running `tests/
    live/` invocation was actively using, the moment the unrelated run
    finished — the shared cache is a fixed path across ALL invocations
    against this repo (see tests/containers.py's module docstring), not
    scoped to just this one.
    """
    if hasattr(session.config, "workerinput"):
        return
    from tests.containers import teardown_all, used_shared_cache

    if not used_shared_cache():
        return
    teardown_all()
