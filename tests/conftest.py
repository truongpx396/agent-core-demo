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

`mock_classification` is the same guarantee for the model router's one live call (`app/core/classification.py`, a POST to the ML service's
/v1/decide): every hermetic test sees "no answer", whatever the environment says.

The usage event's model-alias lookup against LiteLLM is guarded the same way, inside `usage_event_sink` (the only caller of
`model_resolver.resolve_model` since the per-turn ledger write was retired).

`mock_ml_moderation` is the same guarantee for `app/core/moderation.py`'s
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
specifically (tests/core/test_moderation.py's own TestMlInjectionLayer)
override this fixture's patch locally, same override relationship
mock_search_docs/mock_semantic_cache already have with their own
exceptions.

`mock_appdata_postgres` is the equivalent guarantee for the third live
service (`appdata` Postgres, app/core/sql_store.py) — a gap this suite
had until it was found the hard way: `app/agent/runtime/stream.py::astream_events_turn`
calls `_check_allowance`/`upsert_session` UNCONDITIONALLY
on every turn (`spend.usage_summary`/`sessions.upsert_session` underneath),
and every model call writes a usage event (`usage_events._insert`, guarded by
`usage_event_sink` below) — all of these already degrade gracefully on a connection FAILURE (each
has its own try/except, independently tested — see
tests/agent/cost/test_tenant_budget.py/test_sessions.py), but none of them were
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

Patched at `budget_holds.get_connection`/`spend.get_connection`/`sessions.get_connection`/
`tool_idempotency.get_connection` — each module's OWN
`from app.core.sql_store import get_connection` binding, not
`sql_store.get_connection` itself (a `from X import Y` binding is a
separate reference; patching the origin module wouldn't reach it) — and
specifically NOT the higher-level functions themselves
(`_check_allowance`, `usage_summary`, `upsert_session`,
`idempotent`), because tests/agent/cost/test_tenant_budget.py,
tests/agent/runtime/test_sessions.py, and tests/core/test_tool_idempotency.py test
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
without one (see app/agent/graph/core.py). Each test file imports it directly
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

from app.agent.graph import build as graph_build
from app.api import identity
from app.core import classification, moderation
from app.core.config import settings
from app.retrieval.embedding_identity import EmbeddingIdentity

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
    # `default_search` is `async def` now (awaits `tools.gather_context`,
    # which awaits the reranker's HTTP call — see app/agent/graph/build.py) — the
    # fake needs to be awaitable too, not a plain lambda.
    monkeypatch.setattr(graph_build, "default_search", _no_op_search)


# What every hermetic test sees as "the configured embedding identity" (see `fixed_embedding_identity`).
TEST_EMBEDDING_IDENTITY = EmbeddingIdentity(
    alias="embed", resolved_model="test/embed-model", dim=3, query_prefix="", document_prefix="", revision_label=""
)


async def _benign_ml_score(text: str) -> float:
    return 0.0


@pytest.fixture(autouse=True)
def no_real_tracing(monkeypatch):
    """The default suite never builds a real Langfuse client (it starts threads, tries the network, and registers an
    atexit flush that took ~90 s to give up when Langfuse was down). `get_langfuse()` answers None, so a trace is simply
    absent; a test of the tracing code itself resets this and brings its own fake SDK (tests/core/test_tracing_client.py)."""
    from app.core import tracing

    monkeypatch.setattr(tracing, "_client", None)
    monkeypatch.setattr(tracing, "_settled", True)


@pytest.fixture(autouse=True)
def fixed_embedding_identity(monkeypatch):
    """The embedding identity is read on every Qdrant read and write (app/retrieval/qdrant_store.py::check_identity), and computing the
    real one asks LiteLLM which model the `embed` alias resolves to and probes the embedding model for its dimension: two live-service
    calls. A hermetic test gets a fixed identity instead, and a clean per-process target cache so one test's alias answers never leak
    into the next. A test that exercises the identity itself (tests/retrieval/test_qdrant_identity.py) supplies its own."""
    from app.retrieval import embedding_identity, qdrant_store

    async def fixed():
        return TEST_EMBEDDING_IDENTITY

    monkeypatch.setattr(embedding_identity, "current_identity", fixed)
    monkeypatch.setattr(qdrant_store, "current_identity", fixed)
    qdrant_store.reset_identity_cache()
    yield
    qdrant_store.reset_identity_cache()


@pytest.fixture(autouse=True)
def no_real_qdrant(request, monkeypatch):
    """A hermetic test never opens a connection to a real Qdrant.

    `qdrant_store.hybrid_search` and `upsert` now consult Qdrant's aliases BEFORE they embed the query (the embedding-identity check,
    specs/014), so a test that reaches them without a fake used to fail fast at the embedding call and now reaches the network first:
    the client's constructor even probes the server, and a developer with Qdrant running locally would have a unit test talk to it
    (Constitution VII: a test's outcome must not depend on which containers happen to be up). A test that wants Qdrant injects a fake with
    `monkeypatch.setattr(qdrant_store, "get_client", ...)`; the tiers that really need it (integration, live) are exempt by marker.
    """
    if any(request.node.get_closest_marker(m) for m in ("integration", "llm", "e2e", "deepeval", "crawl", "sandbox")):
        return
    from app.retrieval import qdrant_store

    def refuse():
        raise ConnectionError(
            "the hermetic suite never talks to a real Qdrant: inject a fake with monkeypatch.setattr(qdrant_store, 'get_client', ...)"
        )

    monkeypatch.setattr(qdrant_store, "get_client", refuse)


@pytest.fixture(autouse=True)
def mock_ml_moderation(monkeypatch):
    monkeypatch.setattr(moderation, "_ml_malicious_score", _benign_ml_score)


# Kept so the one test that proves this guard works (tests/core/test_default_suite_makes_no_classify_call.py) can put the real function back.
REAL_CHOOSE = classification.choose


async def _no_answer(question, text: str):
    return classification.NO_ANSWER


@pytest.fixture(autouse=True)
def mock_classification(monkeypatch):
    """`route_model` asks the ML service's /v1/decide for a topic (app/core/classification.py). The router is OFF by default, so an ordinary
    test never reaches it; but a test that turns it on, or one that builds the whole graph with a setting leaking in from a developer's
    own environment, must not make a real HTTP call to whatever happens to be listening on :8083 (Constitution VII). Every hermetic test
    therefore sees "no answer". A test of the router supplies its own `choose` (or its own transport), which simply replaces this."""
    monkeypatch.setattr(classification, "choose", _no_answer)


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

    monkeypatch.setattr(graph_build, "default_cache_get", fake_cache_get)
    monkeypatch.setattr(graph_build, "default_cache_set", fake_cache_set)


@pytest.fixture(autouse=True)
def mock_appdata_postgres(monkeypatch):
    from app.agent.cost import budget_holds, spend
    from app.agent.runtime import sessions
    from app.core import tool_idempotency as tool_idempotency
    from credit_billing import credits

    monkeypatch.setattr(budget_holds, "get_connection", _no_postgres_in_tests)
    # The spend read behind every dollar cap and GET /usage (specs/010 T030): it moved out of the retired usage_ledger module, so it
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
def usage_event_sink(monkeypatch):
    """Every model call now writes one usage event (`usage_events.record_call`, via
    `metering.metered_invoke`), which means a real INSERT into `appdata` Postgres and a real
    `resolve_model` HTTP call on the turn path. Left alone, an ordinary agent-node test would pay a
    connection timeout and an HTTP request per call and its result would depend on what is running
    on the machine (the leak `mock_appdata_postgres` closes for its
    modules). The default world records nothing real and CAPTURES each event row in a list the test
    can read, so "this call was metered" is assertable without a database. The real `_insert`
    statement is proven by tests/agent/cost/test_usage_events.py (fake connection) and
    tests/integration/test_usage_events_real_postgres.py (a real one); both restore it from the
    reference saved at import time, which is before this fixture patches anything."""
    from app.agent.cost import usage_events

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
    tests/agent/cost/test_budget_policies.py restores the real function to exercise it. The
    module's per-process cache is cleared on both sides."""
    from app.agent.cost import budget_policies

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
    from app.agent.cost import budgets

    budgets.reset_logged_crossings()
    yield
    budgets.reset_logged_crossings()


@pytest.fixture(autouse=True)
def mock_model_pricing(monkeypatch):
    """`pricing.get_price` reads every model's price from LiteLLM's
    `GET /model/info` on the agent node's hot path, so the same leak
    `usage_event_sink` closes for the model-alias lookup applies: a test's cost must not depend on
    whether a proxy answers on the configured address. The default world is the
    one this app ships with — a local, free model (LiteLLM reports Ollama as a
    KNOWN $0, not an unknown price) — so an ordinary turn costs nothing and
    never counts as unpriced. A test that needs a price patches
    `pricing._fetch_model_info` itself; tests/agent/cost/test_pricing.py restores the
    real fetch to exercise it over a MockTransport. The module's price cache is
    process-wide, so it is cleared on both sides of every test."""
    from app.agent.cost import pricing
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


@pytest.fixture(autouse=True)
def header_auth_mode(monkeypatch):
    """The hermetic suite runs the API in HEADER mode, chosen explicitly (specs/018, task T059).

    Identity mode is the product default: the API trusts only the signed identity the authlimit front door adds, and refuses a request without
    one. Every API test written before that existed sends `X-Tenant-Id` / `X-Principal-Id`, so each one must say that it means header mode;
    this says it once, for all of them, and nothing else about those tests changes. A test of identity mode asks for `identity_mode` below.
    `settings` is a plain object read per request (app/api/deps.py::get_ctx), so there is no cache to clear."""
    monkeypatch.setattr(settings, "auth_mode", "headers")


@pytest.fixture
def identity_mode(monkeypatch):
    """Opt a test back in to the default: identity mode, with no verifier built yet, so the test installs its own (tests/api/test_deps_auth_modes.py
    shows how) and none leaks into the next test."""
    monkeypatch.setattr(settings, "auth_mode", "identity")
    monkeypatch.setattr(identity, "_verifier", None)
    monkeypatch.setattr(identity, "_keys", None)


def pytest_sessionfinish(session, exitstatus):
    """Tears down every Docker container `tests/containers.py::ensure_*()`
    started this run (Postgres/Redis/Qdrant/Ollama, for
    tests/agent/runtime/test_durable_checkpoint.py, tests/integration/, tests/live/) —
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
