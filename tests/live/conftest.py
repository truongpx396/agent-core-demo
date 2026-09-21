"""Fixtures for tests/live/ — the tests that genuinely need a real, small
Ollama model (`@pytest.mark.llm`, `@pytest.mark.e2e`; see GRAPH_PATTERNS.md
pattern 48 for the full design writeup). Two session fixtures, deliberately
different weights for deliberately different needs:

`seed_thread` moved to `tests/seeding.py` (2026-09-16, alongside the
`deepeval`-marked tests moving to their own `tests/deepeval/` package) —
see that module's own docstring for the full seeding rationale. Still
imported directly by this file's own tests that build a graph themselves
via `build_graph()` (`from tests.seeding import seed_thread`); this
conftest no longer needs to define or re-export it since nothing here
calls it internally.

`ollama_endpoint` — just `tests/containers.py::ensure_ollama()`, which pulls
both a real CHAT model and a real embedding model (`nomic-embed-text`,
this app's own `EMBED_MODEL` default) into the same shared container. Used
by test_agent_tool_calling.py, which drives `app.agent.graph.build_graph()`
directly, in-process, with a real `ChatOpenAI` pointed at this endpoint and
every OTHER dependency mocked exactly the way tests/conftest.py's autouse
fixtures already mock them for the rest of this suite (no retrieval/cache/
checkpoint-durability claim is being tested here — only "does the real
model's real native tool-calling integrate with this app's real graph
code," the one thing a fake `GenericFakeChatModel` can't prove because its
responses are scripted, not actually reasoned) — and by test_qdrant_real.py,
which needs the real embedding model specifically (`embed_text` is a real
network call, not local fastembed — see that file's own docstring for how
this was caught: it started out in tests/integration/, which provisions no
LLM at all, and a real CI run surfaced the `openai.APIConnectionError` that
proved it belonged here instead).

`crawl4ai_server` — just `tests/containers.py::ensure_crawl4ai()`, a real
crawl4ai server for `test_domain_crawl_tools_live.py` (2026-09-17,
replacing what had been no reachability handling of its own at all — see
that helper's own docstring for the full writeup). `test_web_crawler_live.py`
used to share this same fixture but moved to `tests/integration/`
(`@pytest.mark.integration`, its own copy of this fixture there) the same
day, once self-provisioning removed the one thing that had made it
`tests/live/`-shaped in the first place — it has no LLM/graph dependency
of its own, unlike `test_domain_crawl_tools_live.py`, which stays here
since it drives the real graph through a `human_approval` interrupt.

`real_stack` — the full backing stack (Postgres + Redis + Qdrant + Ollama)
PLUS a real `uvicorn app.api.main:app` and one real `python -m
app.job_queue.agent_worker`, started as OS subprocesses. Used only by
test_chat_ui.py's Playwright browser test, which needs a genuinely running
HTTP server — the built-in web UI (app/api/static/index.html) actually
calls `POST /chat/stream/queued`/`POST /chat/resume` (see that file's own
`send()`/`resume()`), which only work end-to-end through the real Redis
queue + a real agent-worker process, not an in-process call.

Unlike the CONTAINERS `ensure_*()` provides (expensive — a model pull,
several seconds of container boot — genuinely worth sharing across every
xdist worker, see tests/containers.py's own docstring), the uvicorn/
agent-worker PROCESSES `real_stack` starts are cheap: no model loading, no
schema migration, just a Python interpreter starting up. So `real_stack` is
deliberately NOT shared across workers the way the containers are — each
xdist worker gets its own uvicorn (on its own OS-assigned free port) and
its own agent-worker, both pointed at the SAME shared containers. This
sidesteps needing the containers' whole cross-process cache/lock/teardown
machinery a second time for something inventing it wouldn't actually pay
for: a plain `scope="session"` fixture with a normal generator teardown is
correct and sufficient here, since nothing about a subprocess's lifecycle
is shared across workers in the first place. The agent-worker's own
`CONSUMER_NAME` is independently randomized per process
(app/job_queue/agent_worker.py's own `f"{socket.gethostname()}-{uuid4().hex[:8]}"`),
so N workers' agent-worker processes all correctly join the SAME Redis
consumer group without colliding — exactly how this app's own production
scaling story already works (`make agent-worker`, run several times).

Every real dependency here is wired in purely via environment variables
(`app/core/config.py`'s pydantic-settings fields) — no code changes to
app/ needed; see GRAPH_PATTERNS.md pattern 48's design decision #4.
`OPENAI_API_BASE` points directly at Ollama's own `/v1`, bypassing the
LiteLLM proxy entirely — verified empirically (this module's own manual
check, before writing any of this) that Ollama's OpenAI-compatible endpoint
emits real, native tool calls when called directly with `tools=[...]`, not
the prompt-injected fake litellm-config.yaml's own comment warns about for
its `ollama/` (as opposed to `ollama_chat/`) provider — that distinction is
a LiteLLM-side translation detail, not a property of Ollama's own API.
"""
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
    """`tests/containers.py::ensure_crawl4ai()` (2026-09-17) — used by
    test_domain_crawl_tools_live.py, which monkeypatches this fixture's
    `crawl4ai_server_url`/`crawl4ai_api_token` into
    `app.ingestion.web_crawler`'s own module globals itself (see that
    helper's own docstring for why this fixture only returns connection
    info rather than doing the monkeypatching itself — same division of
    labor `ollama_endpoint` above and the deepeval files'
    `real_ollama_chat_model` fixtures already use). `test_web_crawler_live.py`
    used to share this fixture too but moved to `tests/integration/`
    (own copy there) the same day this fixture was added — see this
    module's own docstring for why."""
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
    """Shared env both `real_stack` and `real_stack_with_retrieval` build
    the api/agent-worker subprocesses from — same dependency wiring, only
    `embed_model` differs between them. Split out so the two fixtures can't
    silently drift apart on everything BUT the one thing that's actually
    supposed to differ."""
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
    # context rather than failing the turn (app/agent/tools.py::
    # gather_context's own docstring) — so this was never load-bearing for
    # this fixture's own calculator/remember prompts, which don't need
    # retrieval to pass, and skipping it keeps those two tests as cheap as
    # they've always been. Tried making it real here once before for full-
    # path coverage; reverted after a real CI run timed out — turned out
    # NOT to be the (sole) cause (see REQUEST_TIMEOUT_SECONDS in
    # _build_app_env), but it was still needless added latency for what
    # these two tests actually need.
    env = _build_app_env(postgres, redis, qdrant, ollama, ml_service)
    yield from _start_app_processes(env)


def _seed_retrieval_data(env: dict[str, str]) -> None:
    """Populates Qdrant's docs + skills collections the same way a real
    deployment does (`make ingest`/`make index-skills`) — run as
    subprocesses against `env` (pointed at THIS fixture's own Qdrant/Ollama
    containers), not called in-process, so this never needs to monkeypatch
    this test process's own already-imported `app.retrieval.embeddings`/
    `qdrant_store` module state (tests/live/test_qdrant_real.py's own
    fixture does that instead, for a different, in-process use case).
    `check=True`: a seeding failure must fail fast here, not surface later
    as a confusing "why did search_docs/skill_search return nothing" in
    whatever test actually runs."""
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
    """Same shape as `real_stack`, but with a REAL embedding model
    (`EMBED_MODEL=nomic-embed-text`, already pulled into the shared Ollama
    container by `ensure_ollama` regardless of which fixture asks for it —
    see that helper's own docstring), Qdrant actually seeded
    (`_seed_retrieval_data`), and its own PRIVATE Redis (`_private_redis`,
    see its own docstring for why sharing `real_stack`'s is a real bug, not
    just untidy) — for tests that need `search_docs` (real citations),
    `skill_search`/`use_skill` (both Qdrant-backed, same as search_docs),
    or `run_subagent` (the bundled `researcher` subagent calls
    search_docs/query_employees itself) to return real, non-empty results
    rather than the degraded-empty-context path `real_stack` deliberately
    accepts for its own simpler calculator/remember prompts. A separate,
    session-scoped fixture rather than changing `real_stack` itself — the
    seeding step and the real embedding calls both cost real time neither
    of `real_stack`'s two existing tests need to pay."""
    postgres = ensure_postgres()
    qdrant = ensure_qdrant()
    ollama = ensure_ollama(TEST_LLM_MODEL)
    ml_service = ensure_ml_service()
    with _private_redis() as redis:
        env = _build_app_env(postgres, redis, qdrant, ollama, ml_service, embed_model=ollama["embed_model"])
        _seed_retrieval_data(env)
        yield from _start_app_processes(env)


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
