"""Fixtures for tests/live/: tests that need a real, small Ollama model (`@pytest.mark.llm`, `@pytest.mark.e2e`;
design in GRAPH_PATTERNS.md pattern 48). Three session fixtures of deliberately different weight:

`ollama_endpoint`: `tests/containers.py::ensure_ollama()`, which pulls a real CHAT model and a real embedding
model (`nomic-embed-text`, the `EMBED_MODEL` default) into one shared container. Used by:
- test_agent_tool_calling.py, which drives `build_graph()` in-process with a real `ChatOpenAI` against this
  endpoint and everything else mocked as tests/conftest.py's autouse fixtures do. It tests only whether the
  real model's native tool-calling integrates with the real graph code, which a scripted
  `GenericFakeChatModel` can't prove because it doesn't reason.
- test_qdrant_real.py, which needs the real embedding model (`embed_text` is a network call, not local
  fastembed; it began in tests/integration/, which has no LLM, and a CI run surfaced the
  `openai.APIConnectionError`).

`crawl4ai_server`: `ensure_crawl4ai()`, a real crawl4ai server for `test_domain_crawl_tools_live.py`.
`test_web_crawler_live.py` moved to `tests/integration/` (with its own copy) once self-provisioning removed
what made it live-shaped; it has no LLM/graph dependency, unlike this one, which drives the real graph through
a `human_approval` interrupt.

`real_stack`: the full backing stack (Postgres + Redis + Qdrant + Ollama) PLUS a real `uvicorn
app.api.main:app` and one real `python -m app.job_queue.agent_worker` as subprocesses. Only test_chat_ui.py's
Playwright test uses it: the web UI calls `POST /chat/stream/queued` and `/chat/resume`, which work end to end
only through the real Redis queue and a real worker.

The CONTAINERS are expensive (model pull, boot) and shared across xdist workers (see tests/containers.py). The
uvicorn/worker PROCESSES are cheap, so `real_stack` is NOT shared: each xdist worker gets its own uvicorn
(OS-assigned free port) and agent-worker against the SAME shared containers. Each worker's `CONSUMER_NAME` is
randomized per process (`f"{socket.gethostname()}-{uuid4().hex[:8]}"`), so they join one Redis consumer group
without colliding, as in production scaling.

Every dependency is wired through environment variables (`app/core/config.py` settings) with no change to app/
(pattern 48, design decision #4). `OPENAI_API_BASE` points straight at Ollama's `/v1`, bypassing LiteLLM:
verified that Ollama's OpenAI-compatible endpoint emits real native tool calls when given `tools=[...]`, not
the prompt-injected fakes litellm-config.yaml warns about for its `ollama/` provider (a LiteLLM translation
detail, not Ollama's API).
"""
import contextlib
import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest

from tests.containers import (
    ensure_crawl4ai,
    ensure_ml_service,
    ensure_ollama,
    ensure_postgres,
    ensure_qdrant,
    ensure_redis,
)

TEST_LLM_MODEL = os.environ.get("TEST_LLM_MODEL", "qwen2.5:3b")
_REPO_ROOT = Path(__file__).resolve().parents[2]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def ollama_endpoint() -> dict[str, str]:
    return ensure_ollama(TEST_LLM_MODEL)


@pytest.fixture(scope="session")
def crawl4ai_server() -> dict[str, str]:
    """`tests/containers.py::ensure_crawl4ai()` (2026-09-17) — used by test_domain_crawl_tools_live.py, which
    monkeypatches this fixture's `crawl4ai_server_url`/`crawl4ai_api_token` into `app.ingestion.web_crawler`'s
    own module globals itself (see that helper's own docstring for why this fixture only returns connection
    info rather than doing the monkeypatching itself — same division of labor `ollama_endpoint` above and the
    deepeval files' `real_ollama_chat_model` fixtures already use).
    """
    return ensure_crawl4ai()


def _build_app_env(
    postgres: dict[str, str],
    redis: dict[str, str],
    qdrant: dict[str, str],
    ollama: dict[str, str],
    ml_service: dict[str, str],
    *,
    embed_model: str | None = None,
) -> dict[str, str]:
    """Shared env both `real_stack` and `real_stack_with_retrieval` build the api/agent-worker subprocesses from
    — same dependency wiring, only `embed_model` differs between them.
    """
    env = {
        **os.environ,
        "CHECKPOINTER_DATABASE_URL": postgres["checkpointer_database_url"],
        "APPDATA_DATABASE_URL": postgres["appdata_database_url"],
        "REDIS_URL": redis["redis_url"],
        "QDRANT_URL": qdrant["qdrant_url"],
        # Real, purely to satisfy /health/ready's own `ml_service` check
        # (app/api/health.py) — added once that endpoint started checking
        # it; neither test using this fixture depends on real reranking/
        # prompt-guard output (see tests/containers.py::ensure_ml_service).
        "ML_SERVICE_URL": ml_service["ml_service_url"],
        "OPENAI_API_BASE": ollama["openai_api_base"],
        "OPENAI_API_KEY": "sk-not-checked-by-ollama",
        "CHAT_MODEL": ollama["model"],
        # REQUEST_TIMEOUT_SECONDS: widened from the 60s default (already
        # made a real, deliberately configurable `app/core/config.py`
        # setting for exactly this reason, not a test-only hack) — a real
        # CI run measured a single real `qwen2.5:1.5b` tool-calling turn
        # taking 67.7s end to end, past the 60s default that's comfortably
        # enough headroom on this demo's own local, GPU-backed dev setup.
        # TEST_LLM_MODEL later moved to `qwen2.5:3b` (this demo's own real
        # default, litellm-config.yaml) after the 1.5b model proved too
        # unreliable at chaining a SECOND real tool decision within one
        # turn (skill_search -> use_skill, run_subagent) — but this
        # fixture's Ollama is a plain `testcontainers.ollama.OllamaContainer`
        # with no GPU passthrough (Docker Desktop on macOS can't pass Metal
        # through to a Linux container), so it runs CPU-only inference:
        # verified directly via `docker stats` showing >1300% CPU during a
        # real run. A run_subagent turn chains THREE real model round trips
        # inside one request (parent's tool-call decision -> a full nested
        # subagent run -> parent's synthesis of that result) and measured
        # over 180s end to end on 3b under CPU-only inference — 420s leaves
        # real margin above that without making a genuinely stuck turn wait
        # forever.
        "REQUEST_TIMEOUT_SECONDS": "420",
        # CHAT_FIRST_RESPONSE_DEADLINE_SECONDS (app/core/config.py, default 30s, added in #57):
        # how long the API waits for a queued job's FIRST event before giving up with an `error`
        # event ("No response for ... is an agent-worker running for this domain?"). Same
        # CPU-only-Ollama reasoning as REQUEST_TIMEOUT_SECONDS above: the first event can trail a
        # cold model load by far more than 30s, and the first browser test to hit a fresh
        # container pays it. CI showed exactly that — the calculator and citation E2Es, which
        # passed before #57, failed with that error in the answer box. The first event cannot
        # legitimately take longer than the whole request is allowed to, so it gets the same
        # ceiling; 30s stays the production default, where "nobody is listening" is the case
        # it exists to catch.
        "CHAT_FIRST_RESPONSE_DEADLINE_SECONDS": "420",
        # SUBAGENT_TIMEOUT_SECONDS: a SEPARATE, INNER wall-clock cap
        # (app/agent/tools/run_subagent.py) on just the nested subagent's own
        # run, independent of REQUEST_TIMEOUT_SECONDS above — real CI
        # failure, caught live (test-live, 2026-09-21): widening the OUTER
        # request timeout to 420s never helped
        # test_a_subagent_delegates_and_returns_a_real_answer, because the
        # 45s production default on THIS inner timeout was firing first,
        # well before the outer budget was ever in play (a `tool_failed`/
        # `TimeoutError` on run_subagent itself, not a
        # "[error: Request exceeded Ns timeout]"). 240s leaves the
        # remaining ~180s of the 420s outer budget for the parent's own
        # decision-to-delegate and synthesis-of-the-result calls on either
        # side of it.
        "SUBAGENT_TIMEOUT_SECONDS": "240",
        # Disables OTel metrics export (app/core/telemetry.py's own
        # blank-endpoint short-circuit) — this subprocess genuinely enters
        # app/api/main.py's lifespan, so it WOULD otherwise retry a real
        # OTLP export every ~15s for its entire lifetime against a
        # collector that doesn't exist anywhere in this fixture, pure
        # guaranteed-to-fail noise (and a little real overhead) for as
        # long as this session-scoped fixture stays alive.
        "OTEL_EXPORTER_OTLP_ENDPOINT": "",
        # The API's default is identity mode (AUTH_MODE, app/api/deps.py): it trusts only a signed identity from the front door, and its lifespan
        # refuses to start without AUTHLIMIT_KEYS_URL. This fixture talks to the API directly, with X-Tenant-Id / X-Principal-Id, and has no front
        # door, so it says header mode out loud. Without this line the subprocess exits with code 3 before it is ready (CI, #208).
        "AUTH_MODE": "headers",
    }
    if embed_model:
        env["EMBED_MODEL"] = embed_model
    return env


def _start_app_processes(env: dict[str, str]) -> Iterator[str]:
    """Starts the api/agent-worker subprocesses `env` describes and yields
    the api's base_url once ready — the part `real_stack`/
    `real_stack_with_retrieval` share after building their own env."""
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    api_proc = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn", "app.api.main:app",
            "--host", "127.0.0.1", "--port", str(port),
        ],
        env=env,
        cwd=str(_REPO_ROOT),
    )
    worker_proc = subprocess.Popen(
        [sys.executable, "-m", "app.job_queue.agent_worker"],
        env=env,
        cwd=str(_REPO_ROOT),
    )
    try:
        _wait_until_ready(base_url, api_proc)
        yield base_url
    finally:
        for proc in (api_proc, worker_proc):
            proc.terminate()
        for proc in (api_proc, worker_proc):
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)


@pytest.fixture(scope="session")
def real_stack() -> Iterator[str]:
    postgres = ensure_postgres()
    redis = ensure_redis()
    qdrant = ensure_qdrant()
    ollama = ensure_ollama(TEST_LLM_MODEL)
    ml_service = ensure_ml_service()
    # Deliberately NOT a real EMBED_MODEL here (see
    # real_stack_with_retrieval below for the fixture that IS): every real
    # turn calls retrieve_context -> search_docs -> hybrid_search ->
    # embed_text unconditionally (GRAPH_PATTERNS.md pattern 20), and
    # retrieve_context's own try/except degrades a failure there to empty
    # context rather than failing the turn (app/agent/tools/builtin.py::
    # gather_context's own docstring) — so this was never load-bearing for
    # this fixture's own calculator prompts, which don't need retrieval to
    # pass, and skipping it keeps those tests as cheap as they've always been.
    # NOT true of a tool that EMBEDS WHAT IT WRITES: `remember` calls
    # embed_text itself (not inside any try/except that degrades), so it fails
    # here with the embedding model's 404. That was once assumed to be fine for
    # the approve-button test; it is not — see that test, which now uses
    # real_stack_with_retrieval. Tried making it real here once before for full-
    # path coverage; reverted after a real CI run timed out — turned out
    # NOT to be the (sole) cause (see REQUEST_TIMEOUT_SECONDS in
    # _build_app_env), but it was still needless added latency for what
    # these two tests actually need.
    env = _build_app_env(postgres, redis, qdrant, ollama, ml_service)
    yield from _start_app_processes(env)


def _seed_retrieval_data(env: dict[str, str]) -> None:
    """Populates Qdrant's docs + skills collections the same way a real deployment does (`make ingest`/`make
    index-skills`) — run as subprocesses against `env` (pointed at THIS fixture's own Qdrant/Ollama
    containers), not called in-process, so this never needs to monkeypatch this test process's own
    already-imported `app.retrieval.embeddings`/ `qdrant_store` module state (tests/live/test_qdrant_real.py's
    own fixture does that instead, for a different, in-process use case).
    """
    for module in ("scripts.seed", "scripts.index_skills"):
        subprocess.run(
            [sys.executable, "-m", module],
            env=env,
            cwd=str(_REPO_ROOT),
            check=True,
            timeout=120,
        )


@contextmanager
def _private_redis() -> Iterator[dict[str, str]]:
    """A dedicated Redis container for `real_stack_with_retrieval` —
    deliberately NOT `tests/containers.py::ensure_redis()`'s shared/cached
    one. Real, reproduced bug this avoids: `real_stack`'s own agent-worker
    (AGENT_DOMAIN defaults to "ecorp" for both fixtures, unchanged) stays
    connected to that shared Redis for the WHOLE session, and Redis
    Streams' consumer-group delivery is round-robin across every connected
    worker regardless of which fixture started it or what env IT was
    given — so a request published through THIS fixture's own API could
    get silently claimed by `real_stack`'s own worker instead, which has
    no real EMBED_MODEL. Caught live: `search_docs`/`skill_search` turns
    intermittently failed with "model \"embed\" not found" — the exact
    symptom of a request landing on the WRONG worker, not a config typo
    (the seeded-into-Qdrant data and this fixture's own worker's env were
    both already correct). A private Redis makes the collision structurally
    impossible: only this fixture's own worker is ever connected to it, so
    there is no second consumer to lose a race to. Not registered with
    tests/containers.py's own shared-container cache/teardown machinery on
    purpose — its lifecycle is scoped to exactly this fixture, via a plain
    try/finally, not the whole test session's shared containers."""
    from testcontainers.redis import RedisContainer

    container = RedisContainer(image="redis/redis-stack-server:latest")
    container.start()  # blocks on a real PING — see RedisContainer._connect
    try:
        host = container.get_container_host_ip()
        port = int(container.get_exposed_port(6379))
        yield {"redis_url": f"redis://{host}:{port}"}
    finally:
        container.stop()


@pytest.fixture(scope="session")
def real_stack_with_retrieval() -> Iterator[str]:
    """Same shape as `real_stack`, but with a REAL embedding model (`EMBED_MODEL=nomic-embed-text`, already
    pulled into the shared Ollama container by `ensure_ollama` regardless of which fixture asks for it — see
    that helper's own docstring), Qdrant actually seeded (`_seed_retrieval_data`), and its own PRIVATE Redis
    (`_private_redis`, see its own docstring for why sharing `real_stack`'s is a real bug, not just untidy) —
    for tests that need `search_docs` (real citations), `skill_search`/`use_skill` (both Qdrant-backed, same
    as search_docs), or `run_subagent` (the bundled `researcher` subagent calls search_docs/query_employees
    itself) to return real, non-empty results rather than the degraded-empty-context path `real_stack`
    deliberately accepts for its own simpler calculator/remember prompts.
    """
    postgres = ensure_postgres()
    qdrant = ensure_qdrant()
    ollama = ensure_ollama(TEST_LLM_MODEL)
    ml_service = ensure_ml_service()
    with _private_redis() as redis:
        env = _build_app_env(postgres, redis, qdrant, ollama, ml_service, embed_model=ollama["embed_model"])
        _seed_retrieval_data(env)
        yield from _start_app_processes(env)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo) -> Iterator[None]:
    """Attach what the page showed to the report of a failed browser test (see tests/live/transcript.py)."""
    outcome = yield
    report = outcome.get_result()
    if report.when != "call" or not report.failed:
        return
    page = getattr(item, "funcargs", {}).get("page")
    if page is None:
        return
    from tests.live.transcript import page_transcript

    # Best-effort diagnostics: a page that already closed, or a broken evaluate(), must never replace the failure
    # this is describing.
    with contextlib.suppress(Exception):
        report.sections.append(("what the page showed", page_transcript(page)))


@pytest.fixture(autouse=True)
def _a_test_leaves_no_memories_behind(request: pytest.FixtureRequest) -> Iterator[None]:
    """After every test that uses the retrieval stack, remove the memories it wrote."""
    if "real_stack_with_retrieval" not in request.fixturenames:
        yield
        return
    from qdrant_client import QdrantClient

    from app.retrieval.qdrant_store import live_name
    from tests.live.memories import forget_memories_not_in, memory_ids

    # The ALIAS, not the logical name: the stack's collection is the physical `docs-<identity hash>` reached through `docs-live`
    # (specs/014), so a collection literally named `docs` does not exist on a fresh stack and a scroll of it is a 404.
    collection = live_name()
    client = QdrantClient(url=ensure_qdrant()["qdrant_url"])
    before = memory_ids(client, collection)
    yield
    forget_memories_not_in(client, collection, before)


def _wait_until_ready(base_url: str, api_proc: subprocess.Popen, timeout: float = 120.0) -> None:
    """Polls `GET /health/ready` (app/api/health.py) — the same real
    dependency check a deployment's own orchestrator would use — rather
    than a fixed sleep, and separately fails fast if the process exited on
    its own (a real config/startup error, which a poll loop alone would
    otherwise just wait out until the timeout, misreporting a crash as
    "slow to start")."""
    deadline = time.monotonic() + timeout
    last_error: Exception | str | None = None
    while time.monotonic() < deadline:
        if api_proc.poll() is not None:
            raise RuntimeError(f"uvicorn exited early (code {api_proc.returncode}) before becoming ready")
        try:
            response = httpx.get(f"{base_url}/health/ready", timeout=5)
            if response.status_code == 200:
                return
            last_error = response.text
        except Exception as exc:  # noqa: BLE001 - retry until the deadline above
            last_error = exc
        time.sleep(1)
    raise RuntimeError(f"{base_url}/health/ready never returned 200 within {timeout}s: {last_error}")
