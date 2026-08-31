"""Fixtures for tests/live/ — the tests that genuinely need a real, small
Ollama model (`@pytest.mark.llm`, `@pytest.mark.e2e`; see GRAPH_PATTERNS.md
pattern 48 for the full design writeup). Two session fixtures, deliberately
different weights for deliberately different needs:

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

from tests.containers import ensure_ollama, ensure_postgres, ensure_qdrant, ensure_redis

TEST_LLM_MODEL = os.environ.get("TEST_LLM_MODEL", "qwen2.5:1.5b")
_REPO_ROOT = Path(__file__).resolve().parents[2]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def ollama_endpoint() -> dict[str, str]:
    return ensure_ollama(TEST_LLM_MODEL)


@pytest.fixture(scope="session")
def real_stack() -> Iterator[str]:
    postgres = ensure_postgres()
    redis = ensure_redis()
    qdrant = ensure_qdrant()
    ollama = ensure_ollama(TEST_LLM_MODEL)

    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    env = {
        **os.environ,
        "CHECKPOINTER_DATABASE_URL": postgres["checkpointer_database_url"],
        "APPDATA_DATABASE_URL": postgres["appdata_database_url"],
        "REDIS_URL": redis["redis_url"],
        "QDRANT_URL": qdrant["qdrant_url"],
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
