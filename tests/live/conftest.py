"""Fixtures for tests/live/ — the tests that genuinely need a real, small
Ollama model (`@pytest.mark.llm`, `@pytest.mark.e2e`; see GRAPH_PATTERNS.md
pattern 48 for the full design writeup). Two session fixtures, deliberately
different weights for deliberately different needs:

`seed_thread(graph, thread_id)` (2026-09-16) — call this before the FIRST
`graph.ainvoke()`/`.astream()` on any thread this file's tests build
themselves via `build_graph()` directly. A real, disclosed finding from
actually running one of these tests, not assumed: `app/agent/graph.py`'s
own `agent()` node docstring says the base SYSTEM_PROMPT "is seeded once
per thread by app/agent/runtime.py::_ensure_seeded_async before the graph
ever runs, so we don't repeat it here" — every production path (API,
Telegram, agent-worker) goes through that seeding via `runtime.py`'s own
`astream_events_turn`/`_unattended`, but a test that calls
`build_graph().ainvoke()` directly, bypassing `runtime.py` entirely, never
triggers it. Caught live: a test asking "What are Ecorp support hours?"
against a graph built this way got an EMPTY first response, a synthetic
retry nudge, then a tool call computing `24 * 7 = 168` and a fabricated
"Ecorp is open 24/7, 168 support hours a week" answer — the model had ZERO
tool-routing guidance, only each tool's own individual docstring (which
LangChain always sends regardless of SystemMessage presence). This means
every test in this file, and `scripts/eval.py`'s own release-gating golden
dataset, had been exercising an UN-GUIDED agent this whole time, not the
one real users actually get — reused `app.agent.runtime._ensure_seeded_async`
directly rather than re-deriving the seeding logic (it reads
`graph.manifest.system_prompt`, the correct prompt for whatever domain
`graph` was actually built for, and is safe/idempotent to call before
every turn, not just the first).

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

`real_stack` — the full backing stack (Postgres + Redis + Qdrant + Ollama)
PLUS a real `uvicorn app.api.main:app` and one real `python -m
app.turns.agent_worker`, started as OS subprocesses. Used only by
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
(app/turns/agent_worker.py's own `f"{socket.gethostname()}-{uuid4().hex[:8]}"`),
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
from pathlib import Path

import httpx
import pytest

from app.agent.runtime import _ensure_seeded_async
from tests.containers import (
    ensure_ml_service,
    ensure_ollama,
    ensure_postgres,
    ensure_qdrant,
    ensure_redis,
)

TEST_LLM_MODEL = os.environ.get("TEST_LLM_MODEL", "qwen2.5:1.5b")
# Separate from TEST_LLM_MODEL, deliberately: the `deepeval`-marked tests
# (test_rag_quality_deepeval.py, test_conversation_simulator_deepeval.py)
# are manual-only, unlike this file's CI-speed 1.5b. DEEPEVAL_MODEL drives
# the TARGET only (the real graph's own CHAT_MODEL, monkeypatched in) — it
# stays local/offline, same reasoning as promptfoo's PROMPTFOO_MODEL: a
# finding needs to be about the model this app actually deploys.
DEEPEVAL_MODEL = os.environ.get("DEEPEVAL_MODEL", "qwen2.5:3b")

# The JUDGE, a SEPARATE knob from DEEPEVAL_MODEL above — same
# target/judge split as promptfoo's PROMPTFOO_MODEL/PROMPTFOO_REDTEAM_MODEL
# (GRAPH_PATTERNS.md pattern 48), fixed 2026-09-16 for the identical reason:
# strengthening the grader shouldn't require also changing what's under
# test. Originally just a bigger LOCAL model (qwen2.5:3b doing double duty
# as both target and judge) — real, verified finding from that setup:
# FaithfulnessMetric scored a hand-verified good answer 0.0 with a `reason`
# that contradicted its own score outright. Moved to Groq's
# `openai/gpt-oss-120b` (a plain instruct model, not `compound` —
# `compound` autonomously invokes web search/code execution mid-request,
# up to 10 tool calls per call, a bad fit for a judge that needs one
# predictable structured verdict, not an agentic loop) for the same reason
# promptfoo's redteam.provider moved to Gemini: a stronger judge is worth
# more than staying local for a MANUAL, occasional, non-target role. Needs
# GROQ_API_KEY (.env.example). `llama-3.3-70b-versatile` (the original
# choice here) doesn't exist on Groq's current API at all — caught by
# actually hitting GET /v1/models rather than trusting the web search
# results that suggested it. This account's real limits for the actual
# model, read off its own `x-ratelimit-*` response headers across several
# rapid real calls, not guessed: ~8,000 TPM (the token bucket refills back
# to full within about a second), and 1,000 RPD — NOT RPM — that refills
# continuously afterward (`reset-requests` grew +86.4s per call across 3
# back-to-back requests; 86.4s * 1000 = 24h exactly). Comfortably above
# this suite's low call volume (a couple of test files, not a
# redteam-scale sweep) either way, so no extra pacing/concurrency limiting
# was added here the way `make promptfoo-redteam` needed for its much
# higher volume.
DEEPEVAL_JUDGE_MODEL = os.environ.get("DEEPEVAL_JUDGE_MODEL", "openai/gpt-oss-120b")
_REPO_ROOT = Path(__file__).resolve().parents[2]


async def seed_thread(graph, thread_id: str) -> None:
    """Seed `thread_id` with `graph`'s own system prompt before the first
    real turn — see this module's own docstring for why this is required
    for a test that calls `build_graph().ainvoke()`/`.astream()` directly.
    Thin wrapper, not a reimplementation: delegates straight to
    `app.agent.runtime._ensure_seeded_async`, the exact function every
    production path already relies on, so this can never drift out of
    sync with what real users actually get."""
    await _ensure_seeded_async(graph, thread_id)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def ollama_endpoint() -> dict[str, str]:
    return ensure_ollama(TEST_LLM_MODEL)


@pytest.fixture(scope="session")
def deepeval_ollama() -> dict[str, str]:
    return ensure_ollama(DEEPEVAL_MODEL)


@pytest.fixture(scope="session")
def deepeval_judge():
    """The GRADER, pointed at Groq — see DEEPEVAL_JUDGE_MODEL's own module
    comment for why this is separate from `deepeval_ollama` (the TARGET).
    `deepeval.models.LocalModel` is a plain OpenAI-SDK client under a
    generic name (confirmed by reading its own source, not assumed from
    the name) — any OpenAI-compatible `base_url` works, same shape this
    app's own `OllamaModel` usage and its production LiteLLM proxy already
    take, just pointed at Groq's real endpoint instead of a local one.
    Fails fast with a clear message if GROQ_API_KEY isn't set, rather than
    an opaque 401 mid-test.
    """
    from deepeval.models import LocalModel

    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        pytest.fail(
            "GROQ_API_KEY is not set — required for the deepeval judge "
            "(DEEPEVAL_JUDGE_MODEL, see tests/live/conftest.py). Get one at "
            "https://console.groq.com/keys and set it in .env."
        )
    return LocalModel(
        model=DEEPEVAL_JUDGE_MODEL,
        api_key=api_key,
        base_url="https://api.groq.com/openai/v1",
        temperature=0,
    )


@pytest.fixture(scope="session")
def real_stack() -> Iterator[str]:
    postgres = ensure_postgres()
    redis = ensure_redis()
    qdrant = ensure_qdrant()
    ollama = ensure_ollama(TEST_LLM_MODEL)
    ml_service = ensure_ml_service()

    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
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
        # Deliberately NOT setting EMBED_MODEL here (leaving the app
        # subprocess's own default, which resolves to nothing real on this
        # Ollama container): every real turn calls retrieve_context ->
        # search_docs -> hybrid_search -> embed_text unconditionally
        # (GRAPH_PATTERNS.md pattern 20), and retrieve_context's own
        # try/except degrades a failure there to empty context rather than
        # failing the turn (app/agent/tools.py::gather_context's own
        # docstring) — so this was never load-bearing for
        # test_chat_ui.py's calculator/remember prompts, which don't need
        # retrieval to pass. Tried making it real anyway for full-path
        # coverage; reverted after a real CI run timed out — turned out
        # NOT to be the (sole) cause, see REQUEST_TIMEOUT_SECONDS below,
        # but it's still needless added latency for what these two tests
        # actually need. tests/live/test_qdrant_real.py still gets a real
        # embedding model — via its own fixture, independent of this
        # subprocess entirely.
        #
        # REQUEST_TIMEOUT_SECONDS: widened from the 60s default (already
        # made a real, deliberately configurable `app/core/config.py`
        # setting for exactly this reason, not a test-only hack) — a real
        # CI run measured a single real `qwen2.5:1.5b` tool-calling turn
        # taking 67.7s end to end, past the 60s default that's comfortably
        # enough headroom on this demo's own local, GPU-backed dev setup.
        # 180s leaves real margin without masking a GENUINE hang (a turn
        # that's actually stuck, not just slow) — this suite would still
        # want to know about that.
        "REQUEST_TIMEOUT_SECONDS": "180",
    }

    api_proc = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn", "app.api.main:app",
            "--host", "127.0.0.1", "--port", str(port),
        ],
        env=env,
        cwd=str(_REPO_ROOT),
    )
    worker_proc = subprocess.Popen(
        [sys.executable, "-m", "app.turns.agent_worker"],
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
