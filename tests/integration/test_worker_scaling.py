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

TestScaledWorkersAbsorbConcurrentLoad is a regression guard for a real
capacity bug found while manually verifying this exact scenario before
writing it: 250 concurrent requests against 5 agent_worker.py processes at
AGENT_WORKER_MAX_CONCURRENCY=50 each initially failed ~83% of the time with
redis.exceptions.MaxConnectionsError — app/turns/queue.py::get_client()'s
Redis pool silently inherited redis-py's own 100-connection default, a
ceiling with nothing to do with how many workers or how much per-worker
concurrency was configured (every POST /chat/stream/queued SSE connection
holds a pooled connection for the FULL blocking-read duration of its turn,
so concurrent SSE connections map ~1:1 onto pool connections held). Fixed
via REDIS_MAX_CONNECTIONS (app/core/config.py). Correctness alone wouldn't
have caught this — every individual turn that DID get a connection still
completed correctly; only running at a concurrency level past the old
silent default surfaces it, which is exactly what that class does on every
run.

TestScaledWorkersHandleHITLAndSubagentDelegation extends the same real
stack to the two other real-production paths a queued turn can take: a
paused human_approval gate resumed via POST /chat/resume, and a
run_subagent delegation whose OWN nested LLM call goes back over the wire
to loadtest/fake_llm_server.py — both real, at real concurrency, across
the same 5 real worker processes.
"""
import asyncio
import json
import os
import random
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from qdrant_client import QdrantClient
from qdrant_client.models import FieldCondition, Filter, MatchValue

from app.retrieval import qdrant_store
from tests.containers import ensure_postgres, ensure_qdrant, ensure_redis

# `xdist_group` forces every test in this module onto the SAME xdist
# worker — needed because `scaled_stack` below is only `scope="module"`,
# which under plain `-n auto`/`--dist=load` is per-WORKER-PROCESS, not
# per-session: pytest-xdist's default scheduler assigns individual test
# ITEMS to whichever worker is free, with no awareness of which fixtures
# they share, so this module's 3 tests landing on 3 different workers means
# 3 separate 7-real-subprocess `scaled_stack`s (5 agent_worker.py + uvicorn
# + fake_llm_server each) all running at once — verified directly this is
# real, not theoretical: reproduced a genuine intermittent failure in
# TestScaledWorkersHandleHITLAndSubagentDelegation under `-n auto` (12
# workers, 3 heavy tests landing on separate ones) that disappeared when
# the same test ran alone. tests/containers.py's own module docstring
# already establishes "cross-worker sharing under xdist is the whole
# point, and it's not free" for the Postgres/Redis/Qdrant containers this
# fixture itself calls into — this marker is the same fix applied to the
# real-subprocess stack layered on top, which didn't inherit that sharing
# just by depending on the containers that have it. Requires
# `--dist=loadgroup` on the invoking `pytest` command (plain `load`/
# `loadscope` ignore this marker entirely) — see .github/workflows/ci.yml's
# `test-integration` job and the Makefile's own `test-integration` target,
# the only two places `-m integration` (and therefore this module) ever
# runs; every other job keeps its default scheduler untouched.
pytestmark = [pytest.mark.integration, pytest.mark.xdist_group(name="scaled_stack")]

_REPO_ROOT = Path(__file__).resolve().parents[2]
_NUM_WORKERS = 5
_PER_WORKER_CONCURRENCY = 50
_TOTAL_REQUESTS = _NUM_WORKERS * _PER_WORKER_CONCURRENCY  # 250
# Matches REQUEST_TIMEOUT_SECONDS below (the app's own internal per-turn
# budget, set in `scaled_stack`'s subprocess env) — real (if fake)
# embedding/Qdrant work on every turn's automatic retrieve_context
# pre-fetch adds real latency beyond a plain chat completion, and CI's
# more contended, likely fewer-core runners need more headroom than this
# dev machine did. Verified directly: a real CI run hit httpx.ReadTimeout
# on a 60s client timeout under exactly this load.
_CLIENT_TIMEOUT_SECONDS = 120


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
def qdrant_collection() -> str:
    """A collection name unique to this test module's run —
    app/retrieval/qdrant_store.py's default `COLLECTION` ("docs") is a
    single, production-shaped name, and `ensure_qdrant()`'s container is
    shared across the WHOLE pytest session (and, under `pytest -n auto`,
    across every xdist worker — exactly what CI runs). Verified directly
    on a real CI run: this fixture's own `ensure_collection()` call (a
    DESTRUCTIVE `recreate_collection`) and tests/agent/test_concurrent_turns.py's
    own identical call, racing on different xdist workers against the SAME
    shared default collection, wiped out each other's seeded documents/
    memories mid-test. `collection: str` (app/core/config.py) is a real,
    already env-configurable setting — no app code change needed, just
    threading this same unique name into the subprocess env below. Same
    isolation idiom tests/integration/test_queue_real_redis.py's own
    `real_redis` fixture already applies to REQUESTS_STREAM/CONSUMER_GROUP,
    applied here to Qdrant's collection name instead."""
    return f"test-worker-scaling-{uuid.uuid4().hex}"


@pytest.fixture(scope="module")
def scaled_stack(qdrant_collection: str) -> Iterator[str]:
    """`_NUM_WORKERS` real app/turns/agent_worker.py OS processes, each at
    `AGENT_WORKER_MAX_CONCURRENCY=_PER_WORKER_CONCURRENCY` (`_TOTAL_REQUESTS`
    system-wide capacity), plus a real `uvicorn app.api.main` and a real
    `loadtest/fake_llm_server.py` — the exact shape manually verified
    before this test was written (see this module's own docstring)."""
    postgres = ensure_postgres()
    redis = ensure_redis()
    qdrant = ensure_qdrant()

    # The collection must exist before any subprocess turn tries to read
    # or write it — Qdrant does NOT auto-create one on first use (verified
    # directly: TestScaledWorkersHandleHITLAndSubagentDelegation's own
    # `remember` writes raised until this was added). Created from THIS
    # process, not a subprocess, via the same app/retrieval/qdrant_store.py
    # function production ingestion (`make ingest`) uses, against
    # `qdrant_collection` (see that fixture's own docstring for why a
    # unique name matters here) — dimension must match
    # loadtest/fake_llm_server.py's own `FAKE_EMBED_DIM`, the only
    # embedding backend any subprocess here can reach. `scaled_stack` is
    # module-scoped, so this uses a plain attribute save/restore (not the
    # function-scoped `monkeypatch` fixture, which can't be requested from
    # a module-scoped one) — restored in the `finally` below alongside the
    # subprocess cleanup.
    from loadtest.fake_llm_server import FAKE_EMBED_DIM

    previous_qdrant_url = qdrant_store.QDRANT_URL
    previous_collection = qdrant_store.COLLECTION
    qdrant_store.QDRANT_URL = qdrant["qdrant_url"]
    qdrant_store.COLLECTION = qdrant_collection
    qdrant_store.ensure_collection(dim=FAKE_EMBED_DIM)

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
        "COLLECTION": qdrant_collection,
        "OPENAI_API_BASE": f"http://127.0.0.1:{fake_llm_port}/v1",
        "OPENAI_API_KEY": "sk-not-checked-by-the-fake-server",
        "CHAT_MODEL": "fake-llm",
        "AGENT_WORKER_MAX_CONCURRENCY": str(_PER_WORKER_CONCURRENCY),
        # The APP's OWN internal per-turn budget (app/core/config.py) needs
        # the same headroom _CLIENT_TIMEOUT_SECONDS does above, or the app
        # itself would cut a slow turn off before this test's own HTTP
        # client ever times out waiting for it — widened the same way
        # tests/live/conftest.py's own real_stack widens it for real
        # (Ollama) latency.
        "REQUEST_TIMEOUT_SECONDS": str(_CLIENT_TIMEOUT_SECONDS),
        # The semantic cache (app/retrieval/semantic_cache.py) goes to real,
        # ephemeral Redis and CAN now genuinely hit (loadtest/fake_llm_server.py's
        # own POST /v1/embeddings route makes embed_text work for real) —
        # not something any test here depends on, since
        # TestScaledWorkersAbsorbConcurrentLoad's own questions are
        # randomized per turn (rarely repeat) and the HITL/subagent tests
        # never ask the same question twice either; a stray hit or miss
        # either way degrades gracefully (semantic_cache.get/set's own
        # broad except) and doesn't change what any assertion here checks.
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
        qdrant_store.QDRANT_URL = previous_qdrant_url
        qdrant_store.COLLECTION = previous_collection


@pytest.fixture(scope="module")
def qdrant_url(scaled_stack) -> str:
    """The same Qdrant container `scaled_stack` already provisioned for
    `GET /health/ready` — `ensure_qdrant()` is idempotent/cached (see
    tests/containers.py), so calling it again here just exposes that same
    container's URL to THIS test process too, letting a test verify a
    real write (e.g. a HITL-approved `remember` call) actually landed,
    without needing `scaled_stack` itself to change its own return shape.
    Depends on `scaled_stack` only to document the ordering intent, not
    because it's strictly required — `ensure_qdrant()` would work fine
    called first too."""
    return ensure_qdrant()["qdrant_url"]


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
                # loadtest/locustfile_queued.py's QueuedTurnUser gives: bypasses
                # the per-tenant rate limiter (app/api/rate_limit.py) so
                # this test measures the worker/queue's own concurrency
                # ceiling, not the rate limiter doing its job.
                "X-Tenant-Id": f"loadtest-{i}",
                "X-Principal-Id": f"user-{i}",
            }
            async with httpx.AsyncClient(timeout=_CLIENT_TIMEOUT_SECONDS) as client:
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


class TestScaledWorkersHandleHITLAndSubagentDelegation:
    """Same real 5-process/`AGENT_WORKER_MAX_CONCURRENCY=50` stack as
    TestScaledWorkersAbsorbConcurrentLoad above, proving the two other
    things this queued path needs to get right at real concurrency: a
    real HITL pause/resume round trip through `POST /chat/resume` (not
    just `POST /chat/stream/queued`), and a real `run_subagent`
    delegation whose nested LLM call goes back over the wire to the same
    fake server — both via loadtest/fake_llm_server.py's own
    `_remember`/`_run_subagent` simulators, added specifically to support
    this. `n` is smaller than `_TOTAL_REQUESTS` here (HITL is two real
    HTTP round trips per turn, not one) to keep this class's own runtime
    reasonable while still exercising genuine concurrency across all 5
    workers — tests/agent/test_concurrent_turns.py's own
    TestHITLApprovalUnderConcurrency/TestSubagentCallUnderConcurrency
    already prove exhaustive cross-wiring isolation in-process; this
    class's job is proving the same mechanisms hold at real
    process/network scale, not re-deriving that proof a second time."""

    def test_concurrent_hitl_pauses_and_resumes_all_succeed_and_write_to_qdrant(
        self, scaled_stack, qdrant_url, qdrant_collection
    ):
        base_url = scaled_stack
        n = 40

        async def _one_turn(i: int) -> tuple[int, str, int]:
            # Own synthetic tenant per request too, not just principal — same
            # reasoning TestScaledWorkersAbsorbConcurrentLoad's own headers
            # comment gives: bypasses the per-tenant rate limiter
            # (app/api/rate_limit.py, default 30/min) so this measures HITL
            # concurrency itself. Verified directly: a first attempt sharing
            # one tenant across all n got every request past the first ~30
            # rejected with 429 — not a HITL bug, the rate limiter correctly
            # doing its job against traffic shaped like one real tenant.
            headers = {"X-Tenant-Id": f"loadtest-hitl-{i}", "X-Principal-Id": f"user-{i}"}
            thread_id = f"thread-hitl-{i}"
            async with httpx.AsyncClient(timeout=_CLIENT_TIMEOUT_SECONDS) as client:
                pause_response = await client.post(
                    f"{base_url}/chat/stream/queued",
                    json={
                        "message": f"please remember distinguishing fact {i}",
                        "thread_id": thread_id,
                        "images": [],
                    },
                    headers=headers,
                )
                if pause_response.status_code != 200:
                    return pause_response.status_code, pause_response.text, i
                resume_response = await client.post(
                    f"{base_url}/chat/resume",
                    json={"thread_id": thread_id, "approved": True},
                    headers=headers,
                )
            return resume_response.status_code, resume_response.text, i

        async def _run_all():
            return await asyncio.gather(*(_one_turn(i) for i in range(n)))

        results = asyncio.run(_run_all())

        statuses = [status for status, _, _ in results]
        succeeded = statuses.count(200)
        assert succeeded == n, (
            f"only {succeeded}/{n} HITL round trips succeeded — status codes seen: "
            f"{sorted(set(statuses))}"
        )

        mismatches = [
            (i, _parse_final_answer(body))
            for _, body, i in results
            if f"distinguishing fact {i}" not in _parse_final_answer(body)
        ]
        assert not mismatches, (
            f"{len(mismatches)}/{n} resumes had an answer not reflecting their own "
            f"approved memory — first few (thread, got): {mismatches[:5]}"
        )

        # The real write underneath every approval: real Qdrant (not the
        # HTTP response text) — same "read the real store back" check
        # tests/agent/test_concurrent_turns.py's own HITL test already
        # does in-process, proven here instead at real 5-process
        # concurrency, over the real network.
        client = QdrantClient(url=qdrant_url)
        for i in range(n):
            principal = f"user-{i}"
            points, _ = client.scroll(
                collection_name=qdrant_collection,
                scroll_filter=Filter(
                    must=[
                        FieldCondition(key="kind", match=MatchValue(value="memory")),
                        FieldCondition(key="owner", match=MatchValue(value=principal)),
                    ]
                ),
                limit=10,
            )
            assert len(points) == 1, f"expected exactly 1 memory for {principal}, got {len(points)}"
            memory_text = points[0].payload["text"]
            assert f"distinguishing fact {i}" in memory_text, memory_text

    def test_concurrent_subagent_delegations_all_succeed(self, scaled_stack):
        base_url = scaled_stack
        n = 40

        async def _one_turn(i: int) -> tuple[int, str, int]:
            headers = {"X-Tenant-Id": f"loadtest-subagent-{i}", "X-Principal-Id": f"user-{i}"}
            async with httpx.AsyncClient(timeout=_CLIENT_TIMEOUT_SECONDS) as client:
                response = await client.post(
                    f"{base_url}/chat/stream/queued",
                    json={
                        "message": f"please delegate task {i} to a subagent",
                        "thread_id": f"thread-subagent-{i}",
                        "images": [],
                    },
                    headers=headers,
                )
            return response.status_code, response.text, i

        async def _run_all():
            return await asyncio.gather(*(_one_turn(i) for i in range(n)))

        results = asyncio.run(_run_all())

        statuses = [status for status, _, _ in results]
        succeeded = statuses.count(200)
        assert succeeded == n, (
            f"only {succeeded}/{n} subagent delegations succeeded — status codes seen: "
            f"{sorted(set(statuses))}"
        )

        # The nested run's own LLM call went back over the wire to the
        # SAME fake server (its ChatOpenAI construction also reads
        # OPENAI_API_BASE) — the final answer being traceable back to its
        # own delegated task proves that whole real, two-hop round trip
        # (top-level tool_call -> real run_subagent execution -> nested
        # HTTP call -> nested answer -> back into the top-level synthesis)
        # stayed correctly matched to ITS OWN turn at real concurrency.
        mismatches = [
            (i, _parse_final_answer(body))
            for _, body, i in results
            if f"task {i} to a subagent" not in _parse_final_answer(body)
        ]
        assert not mismatches, (
            f"{len(mismatches)}/{n} subagent delegations had an answer not reflecting "
            f"their own delegated task — first few (thread, got): {mismatches[:5]}"
        )
