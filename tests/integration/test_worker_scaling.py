"""Real-subprocess capacity test: do N independently-scaled
app/turns/agent_worker.py processes, each running M turns concurrently
(AGENT_WORKER_MAX_CONCURRENCY), genuinely absorb N*M concurrent real HTTP
requests against POST /chat/stream/queued — correctly, not just without
crashing?

tests/agent/test_concurrent_turns.py already proves the underlying
mechanism is correct at small scale, in-process (direct function calls,
one Python interpreter). This is the complementary question at PRODUCTION
shape and real scale: real OS processes, a real HTTP server, real
concurrent client connections, real Redis queueing across real worker
processes — the one level nothing else in this suite drives.

Deliberately doesn't need a real LLM: loadtest/fake_llm_server.py stands in
(see that module's own docstring for why — native Ollama on a dev machine
serializes to exactly one in-flight generation, which would make a test
like this measure Ollama's ceiling, not this app's own). `@pytest.mark.integration`,
not `@pytest.mark.llm`/`e2e`: needs Docker (real Postgres + Redis via
tests/containers.py) but no Ollama/GPU sidecar, so it runs in the same CI
job as the rest of tests/integration/ — same reasoning tests/live/conftest.py's
own module docstring gives for why THAT file's `real_stack` needs the
`llm`/`e2e` markers and this one doesn't.

This test is a regression guard for a real capacity bug found while
manually verifying this exact scenario before writing it: 250 concurrent
requests against 5 agent_worker.py processes at AGENT_WORKER_MAX_CONCURRENCY=50
each initially failed ~83% of the time with redis.exceptions.MaxConnectionsError
— app/turns/queue.py::get_client()'s Redis pool silently inherited redis-py's
own 100-connection default, a ceiling with nothing to do with how many
workers or how much per-worker concurrency was configured (every
POST /chat/stream/queued SSE connection holds a pooled connection for the
FULL blocking-read duration of its turn, so concurrent SSE connections map
~1:1 onto pool connections held). Fixed via REDIS_MAX_CONNECTIONS
(app/core/config.py). Correctness alone wouldn't have caught this — every
individual turn that DID get a connection still completed correctly; only
running at a concurrency level past the old silent default surfaces it,
which is exactly what this test does on every run.
"""
import asyncio
import json
import os
import random
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from tests.containers import ensure_postgres, ensure_qdrant, ensure_redis

pytestmark = pytest.mark.integration

_REPO_ROOT = Path(__file__).resolve().parents[2]
_NUM_WORKERS = 5
_PER_WORKER_CONCURRENCY = 50
_TOTAL_REQUESTS = _NUM_WORKERS * _PER_WORKER_CONCURRENCY  # 250


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_until_ready(url: str, proc: subprocess.Popen, timeout: float = 60.0) -> None:
    """Same shape as tests/live/conftest.py's own `_wait_until_ready` —
    polls rather than sleeping a fixed amount, and fails fast (rather than
    just waiting out the full timeout) if the process already exited on
    its own, so a real startup/config error reads as "process crashed,"
    not a misleading "took too long to start.\""""
    deadline = time.monotonic() + timeout
    last_error: Exception | str | None = None
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(
                f"{proc.args} exited early (code {proc.returncode}) before becoming ready"
            )
        try:
            response = httpx.get(url, timeout=5)
            if response.status_code == 200:
                return
            last_error = response.text
        except Exception as exc:  # noqa: BLE001 - retry until the deadline above
            last_error = exc
        time.sleep(0.5)
    raise RuntimeError(f"{url} never returned 200 within {timeout}s: {last_error}")


@pytest.fixture(scope="module")
def scaled_stack() -> Iterator[str]:
    """`_NUM_WORKERS` real app/turns/agent_worker.py OS processes, each at
    `AGENT_WORKER_MAX_CONCURRENCY=_PER_WORKER_CONCURRENCY` (`_TOTAL_REQUESTS`
    system-wide capacity), plus a real `uvicorn app.api.main` and a real
    `loadtest/fake_llm_server.py` — the exact shape manually verified
    before this test was written (see this module's own docstring)."""
    postgres = ensure_postgres()
    redis = ensure_redis()
    qdrant = ensure_qdrant()

    fake_llm_port = _free_port()
    api_port = _free_port()
    base_url = f"http://127.0.0.1:{api_port}"

    env = {
        **os.environ,
        "CHECKPOINTER_DATABASE_URL": postgres["checkpointer_database_url"],
        "APPDATA_DATABASE_URL": postgres["appdata_database_url"],
        "REDIS_URL": redis["redis_url"],
        # A real Qdrant IS needed here, even though this test's own
        # assertions never depend on retrieval quality — verified directly:
        # GET /health/ready (app/api/health.py) reports "degraded" (a
        # non-200 status) whenever ANY checked dependency, Qdrant included,
        # is unreachable, so _wait_until_ready below would time out without
        # this — same reason tests/live/conftest.py's own real_stack always
        # provisions Qdrant too, even for its own two tests that don't need
        # real retrieval either. retrieve_context's own try/except still
        # degrades a search miss to empty context rather than failing a
        # turn (app/agent/tools.py::gather_context) — this is purely about
        # satisfying the readiness check, not about what a turn needs to
        # succeed.
        "QDRANT_URL": qdrant["qdrant_url"],
        "OPENAI_API_BASE": f"http://127.0.0.1:{fake_llm_port}/v1",
        "OPENAI_API_KEY": "sk-not-checked-by-the-fake-server",
        "CHAT_MODEL": "fake-llm",
        "AGENT_WORKER_MAX_CONCURRENCY": str(_PER_WORKER_CONCURRENCY),
        # The semantic cache (app/retrieval/semantic_cache.py) still goes to
        # real, ephemeral Redis but never actually hits:
        # loadtest/fake_llm_server.py has no /v1/embeddings route, so
        # embed_text 404s and the cache degrades to a permanent miss —
        # graceful by design (semantic_cache.get/set's own broad except),
        # not a failure this test needs to route around; unlike
        # /health/ready's Qdrant check, nothing here depends on the cache
        # actually working.
    }

    fake_llm_proc = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn", "loadtest.fake_llm_server:app",
            "--host", "127.0.0.1", "--port", str(fake_llm_port),
        ],
        cwd=str(_REPO_ROOT),
    )
    _wait_until_ready(f"http://127.0.0.1:{fake_llm_port}/health", fake_llm_proc)

    api_proc = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn", "app.api.main:app",
            "--host", "127.0.0.1", "--port", str(api_port),
        ],
        env=env,
        cwd=str(_REPO_ROOT),
    )
    _wait_until_ready(f"{base_url}/health/ready", api_proc)

    worker_procs = [
        subprocess.Popen(
            [sys.executable, "-m", "app.turns.agent_worker"], env=env, cwd=str(_REPO_ROOT)
        )
        for _ in range(_NUM_WORKERS)
    ]
    # Cheap processes — no model loading, no schema migration beyond the
    # checkpointer's idempotent setup() (same reasoning
    # tests/live/conftest.py's own real_stack docstring gives for not
    # needing the containers' own cross-worker sharing machinery a second
    # time here). A short fixed wait is enough for each to connect to
    # Redis and join the consumer group; nothing is lost if a request gets
    # published before every worker has subscribed — Redis just queues it
    # for whichever worker reads it next.
    time.sleep(2)

    try:
        yield base_url
    finally:
        procs = [fake_llm_proc, api_proc, *worker_procs]
        for proc in procs:
            proc.terminate()
        for proc in procs:
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)


def _parse_final_answer(sse_text: str) -> str:
    """Concatenates every `"token"` event's content from a raw
    POST /chat/stream/queued SSE response body — the same shape
    app/api/static/index.html's own `pumpSSE`/`handleEvent` parse, just
    without needing a real browser here."""
    answer = ""
    for line in sse_text.splitlines():
        if not line.startswith("data: "):
            continue
        event = json.loads(line[len("data: ") :])
        if event["type"] == "token":
            answer += event["content"]
    return answer


class TestScaledWorkersAbsorbConcurrentLoad:
    def test_250_concurrent_calculator_turns_all_succeed_with_correct_answers(self, scaled_stack):
        base_url = scaled_stack
        n = _TOTAL_REQUESTS

        async def _one_turn(i: int) -> tuple[int, str, int]:
            a, b = random.randint(1, 99), random.randint(1, 99)
            expected = a * b
            headers = {
                # Own synthetic tenant per request, same reasoning
                # loadtest/locustfile.py's ManyTenantsUser gives: bypasses
                # the per-tenant rate limiter (app/api/rate_limit.py) so
                # this test measures the worker/queue's own concurrency
                # ceiling, not the rate limiter doing its job.
                "X-Tenant-Id": f"loadtest-{i}",
                "X-Principal-Id": f"user-{i}",
            }
            async with httpx.AsyncClient(timeout=60) as client:
                response = await client.post(
                    f"{base_url}/chat/stream/queued",
                    json={"message": f"what is {a} * {b}?", "thread_id": f"thread-{i}", "images": []},
                    headers=headers,
                )
            return response.status_code, response.text, expected

        async def _run_all():
            return await asyncio.gather(*(_one_turn(i) for i in range(n)))

        results = asyncio.run(_run_all())

        statuses = [status for status, _, _ in results]
        succeeded = statuses.count(200)
        assert succeeded == n, (
            f"only {succeeded}/{n} requests succeeded ({n - succeeded} failed) — "
            f"status codes seen: {sorted(set(statuses))}. If these are Redis "
            f"connection errors, app/turns/queue.py::get_client()'s "
            f"REDIS_MAX_CONNECTIONS ceiling has regressed below what "
            f"{n} concurrent SSE connections need."
        )

        # Business correctness under load, not just "didn't crash": each
        # turn's real calculator tool call must have produced the RIGHT
        # number for THAT request specifically — proves the fake LLM's
        # tool_call decision, the real calculator tool's execution, and the
        # real per-thread checkpoint round trip all stayed correctly
        # matched to their OWN request even at n-way concurrency across 5
        # separate worker processes, never cross-wired with a sibling's.
        mismatches = [
            (expected, _parse_final_answer(body))
            for _, body, expected in results
            if str(expected) not in _parse_final_answer(body)
        ]
        assert not mismatches, (
            f"{len(mismatches)}/{n} turns had a wrong or missing answer "
            f"(expected, got) — first few: {mismatches[:5]}"
        )
