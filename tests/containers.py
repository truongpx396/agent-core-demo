"""Shared, Docker-backed real services for tests that need more than a fake:
tests/agent/runtime/test_durable_checkpoint.py (real Postgres), tests/integration/ (Postgres + Redis + Qdrant)
and tests/live/ (adds a small real Ollama). Design: GRAPH_PATTERNS.md pattern 48. This covers the two
mechanics every `ensure_*()` depends on.

**Skip, don't fail, when Docker isn't reachable.** Each `ensure_*()` probes connectivity and `pytest.skip`s on
failure, but starts its own ephemeral container rather than assuming an external one (no `make up`), so
`pytest -q` on a laptop without Docker stays green and fast.

**Cross-worker sharing under pytest-xdist.** `-n auto` runs each worker as a SEPARATE process, so a plain
session fixture would start one container PER WORKER, the waste a shared Ollama avoids (its request queueing
lets many workers hit one warm model; see `test-live`). Every `ensure_*` uses a `filelock.FileLock` over a
small JSON cache file: the first worker to take the lock starts the container (and pulls the Ollama model) and
caches its connection info; every other worker, and that one on later tests, reads it back.

The cache lives under a FIXED path (`tempfile.gettempdir()`, namespaced by a hash of the repo root, see
`_shared_root`), NOT pytest's `tmp_path_factory` basetemp, the usual xdist recipe. That fails here (verified):
workers derive the directory from a value the controller broadcast at start, but nothing broadcasts it back to
the controller's `pytest_sessionfinish`, where a fresh `TempPathFactory` allocates the NEXT rotating number
instead of finding the workers'. A fixed path sidesteps that, at the cost of treating a cached entry as
possibly stale (`_acquire` validates it).

Teardown can't be "whichever worker finishes first" (it would pull containers from under the others) or
testcontainers' Ryuk reaper (its session is per Python process, so the first worker to exit would reap the
rest's containers). So `TESTCONTAINERS_RYUK_DISABLED` is set before any container starts, and
`teardown_all()`, called from `tests/conftest.py`'s `pytest_sessionfinish` and gated to run once ALL workers
have reported, removes every container this run started by id, read from the cache files. A container id is
process-independent (the live object exists only in the worker that started it), so the controller, which
started none, can tear down.
"""
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import psycopg
import pytest
from filelock import FileLock

# `docker` (the PyPI package `testcontainers` itself depends on) is
# deliberately NOT imported at module level, unlike `psycopg`/`pytest`/
# `filelock` above (all three are installed in EVERY test job, including
# the fast `test` job, which never runs anything from this module beyond
# `tests/conftest.py::pytest_sessionfinish`'s own `teardown_all()` call —
# see that function's own docstring). A module-level `import docker` would
# make importing THIS MODULE AT ALL fail with `ModuleNotFoundError` in that
# job, which doesn't install `docker`/`testcontainers` (correctly — it
# never starts a container) — caught directly in CI: every real test
# passed, then the whole session still exited non-zero because
# `pytest_sessionfinish` crashed importing this module. Each function that
# actually touches Docker imports it locally instead.

# Must be set before testcontainers' first container start (every
# testcontainers.* submodule is imported lazily, inside each ensure_*()'s own
# `_start()` closure below, specifically so this line always runs first) —
# see module docstring on why the default Ryuk-on-process-exit reaper is
# wrong for a container many xdist worker processes, and the controller,
# all share.
os.environ.setdefault("TESTCONTAINERS_RYUK_DISABLED", "true")

_REPO_ROOT = Path(__file__).resolve().parent.parent
_POSTGRES_INIT_DIR = _REPO_ROOT / "postgres-init"
_CACHE_PREFIX = "live-containers"


def _shared_root() -> Path:
    """A fixed directory, shared by every worker of an xdist run AND by the xdist controller's own later
    `pytest_sessionfinish` — see module docstring for why this deliberately isn't pytest's own rotating
    `tmp_path_factory` basetemp.
    """
    digest = hashlib.sha1(str(_REPO_ROOT).encode()).hexdigest()[:12]
    root = Path(tempfile.gettempdir()) / f"agent-core-demo-live-containers-{digest}"
    root.mkdir(exist_ok=True)
    return root


def _container_is_running(container_id: str) -> bool:
    try:
        import docker

        return docker.from_env().containers.get(container_id).status == "running"
    except Exception:  # noqa: BLE001 - not found / daemon gone / anything else all mean "not usable"
        return False


def _require_docker(what: str) -> None:
    try:
        import docker

        docker.from_env().ping()
    except Exception as exc:  # noqa: BLE001 - any failure here means "skip", not "fail"
        pytest.skip(
            f"Docker isn't reachable ({exc}) — {what} starts its own ephemeral "
            "container (no `make up` needed), but still needs a running Docker "
            "daemon. Start Docker and re-run, or accept this skip for a fast, "
            "hermetic local run."
        )


def _cache_paths(key: str) -> tuple[Path, Path]:
    root = _shared_root()
    return root / f"{_CACHE_PREFIX}-{key}.json", root / f"{_CACHE_PREFIX}-{key}.lock"


# A marker FILE, not a plain Python global. `pytest_sessionfinish` (tests/conftest.py) must tell "this
# invocation never touched the shared cache" from "this invocation used it and should tear down its share".
# Under pytest-xdist (CI's `test-live`: `-n auto`) the process that calls `_acquire` (a WORKER) and the one
# that reaches the teardown check (the CONTROLLER) are DIFFERENT OS processes with separate memory, so an
# in-memory flag set by one is invisible to the other; an earlier version used exactly that and would have
# silently disabled teardown for every xdist run, CI included, without an error. A worker's `os.getppid()` is
# the xdist controller's pid (workers are its direct subprocesses; xdist sets `PYTEST_XDIST_WORKER` in a
# worker's environment, the documented way to detect this); a plain run has no controller, so it uses its own
# pid. Either way, `pytest_sessionfinish` (the controller, or standing in for one) checks for a marker under
# its OWN pid, written by whichever process(es) called `_acquire`, itself or its worker children.
#
# Real bug, caught live: an ordinary `pytest -q` run (no live/e2e/integration marker, never calling any
# `ensure_*()`) still reached `teardown_all()` unconditionally and tore down the ENTIRE shared cache,
# including a real Postgres/Qdrant/Ollama a SEPARATE, still-running `tests/live/` invocation was using, the
# moment the unrelated run finished. The existing `workerinput` guard protects only against an xdist WORKER
# finishing early within one invocation, not a second independent pytest process. It doesn't solve the reverse
# (two live-marked invocations overlapping still race), a narrower "don't do that" case; a routine fast-suite
# run wiping out a concurrent live run is the common, surprising one this closes.
def _controller_pid() -> int:
    return os.getppid() if "PYTEST_XDIST_WORKER" in os.environ else os.getpid()


def _used_marker(pid: int) -> Path:
    return _shared_root() / f"{_CACHE_PREFIX}-used-by-{pid}"


def _mark_cache_used() -> None:
    _used_marker(_controller_pid()).touch(exist_ok=True)


def _acquire(key: str, start: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """Run `start()` exactly once across every xdist worker of this pytest
    run (see module docstring); every call, including the first, returns
    `start()`'s result. A cached entry is trusted only while its container is
    still actually running — the fixed cache path (see `_shared_root`) means
    a leftover file from an earlier, already-torn-down invocation (a killed
    `-x` run, a canceled CI job) is a real possibility, not just a theoretical
    one, so a dead reference is discarded and `start()` runs again rather
    than handing back a connection string nothing is listening on."""
    _mark_cache_used()
    cache_file, lock_file = _cache_paths(key)
    with FileLock(str(lock_file)):
        if cache_file.exists():
            info = json.loads(cache_file.read_text())
            if _container_is_running(info.get("container_id", "")):
                return info
            cache_file.unlink(missing_ok=True)
        info = start()
        cache_file.write_text(json.dumps(info))
        return info


def used_shared_cache() -> bool:
    """True when THIS invocation (this process, or — under xdist — one of its own worker children) called
    `_acquire` at least once this session.
    """
    marker = _used_marker(_controller_pid())
    used = marker.exists()
    marker.unlink(missing_ok=True)
    return used


def teardown_all() -> None:
    """Removes every container an `ensure_*()` call started this run, by the container id each cached alongside
    its connection info, and clears the cache files themselves (so a later, unrelated invocation never
    mistakes them for a still-live container — see `_acquire`'s own defense in depth for when it does anyway).
    """
    root = _shared_root()
    cache_files = list(root.glob(f"{_CACHE_PREFIX}-*.json"))
    if not cache_files:
        return
    try:
        import docker

        client = docker.from_env()
    except Exception:  # noqa: BLE001 - nothing to tear down without Docker (including `docker` itself not being installed — see module docstring)
        return
    for cache_file in cache_files:
        try:
            info = json.loads(cache_file.read_text())
            container_id = info.get("container_id")
            if container_id:
                client.containers.get(container_id).remove(force=True, v=True)
        except Exception:  # noqa: BLE001,S110 - best-effort cleanup; a leaked ephemeral CI container is a cost, not a correctness bug
            pass
        finally:
            cache_file.unlink(missing_ok=True)
            Path(str(cache_file).removesuffix(".json") + ".lock").unlink(missing_ok=True)


def ensure_postgres() -> dict[str, str]:
    """Real Postgres 16 (same image docker-compose.yml's own `postgres` service uses), pre-seeded via the EXACT
    same `postgres-init/*.sql` files docker-compose feeds `/docker-entrypoint-initdb.d` — mounted read-only
    into that same path so the official postgres image's own entrypoint runs them, unmodified, in filename
    order.
    """
    _require_docker("a real checkpointer/appdata Postgres")

    def _start() -> dict[str, Any]:
        from testcontainers.postgres import PostgresContainer

        container = PostgresContainer(
            image="postgres:16",
            username="langfuse",
            password="langfuse",
            dbname="langfuse",  # matches docker-compose.yml's POSTGRES_DB, so postgres-init's `\connect appdata` etc. resolve identically
        ).with_volume_mapping(str(_POSTGRES_INIT_DIR), "/docker-entrypoint-initdb.d", mode="ro")
        # Sized for the stack this container hosts, not left at Postgres's default 100:
        # tests/integration/test_worker_scaling.py runs 5 agent_worker processes + the API, and
        # EACH holds up to two pools (appdata `max_size=10`, checkpointer
        # `CHECKPOINTER_POOL_MAX_SIZE=10`) — 6 x 20 = 120 potential connections before counting
        # health probes or whatever other tests are using this same shared container. Under
        # 250 concurrent turns that exceeded 100 in CI ("FATAL: sorry, too many clients already"),
        # pool checkouts timed out, and 245/250 turns came back with an empty answer even though
        # every HTTP response was 200. See WORKER_CONCURRENCY.md's "Postgres connection budget".
        container.with_command("postgres -c max_connections=300")
        container.start()  # blocks until the container's own psql-based readiness check succeeds — see this module's own verification of that
        host = container.get_container_host_ip()
        port = int(container.get_exposed_port(5432))
        checkpointer_url = f"postgresql://langfuse:langfuse@{host}:{port}/checkpointer"
        appdata_url = f"postgresql://langfuse:langfuse@{host}:{port}/appdata"
        # Belt-and-suspenders: PostgresContainer.start()'s own readiness probe
        # connects to `langfuse` (the default db) via an in-container exec, so
        # this separately confirms postgres-init/05-checkpointer-db.sql's
        # `CREATE DATABASE checkpointer` specifically has landed, over the real
        # exposed TCP port every real caller will actually use.
        for attempt in range(30):
            try:
                with psycopg.connect(checkpointer_url, connect_timeout=2):
                    break
            except Exception:  # noqa: BLE001 - retry until the timeout below
                if attempt == 29:
                    raise
                time.sleep(1)
        _setup_checkpointer_schema_once(checkpointer_url)
        return {
            "container_id": container.get_wrapped_container().id,
            "checkpointer_database_url": checkpointer_url,
            "appdata_database_url": appdata_url,
        }

    return _acquire("postgres", _start)


def _setup_checkpointer_schema_once(checkpointer_url: str) -> None:
    """Runs `AsyncPostgresSaver.setup()` exactly ONCE, inside `ensure_postgres`'s cross-worker lock, before any
    test can call it again concurrently. It is the same call `app/agent/runtime/core.py::_open_checkpointer`
    makes on every real process start ("idempotently, on every process including each independently-scaled
    agent_worker.py instance", per postgres-init/05-checkpointer-db.sql).
    """
    import asyncio

    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    async def _setup() -> None:
        async with AsyncPostgresSaver.from_conn_string(checkpointer_url) as saver:
            await saver.setup()

    # A bare `asyncio.new_event_loop()` + `run_until_complete()` + `close()`, NOT `asyncio.run(_setup())`.
    # Real bug (test-live CI, 2026-09-21): `asyncio.run()` calls `set_event_loop(...)` on entry and
    # `set_event_loop(None)` on exit, a THREAD-GLOBAL mutation. This runs inside `ensure_postgres`, a sync
    # fixture shared by every `tests/live/` file, and whichever xdist worker wins the cache-miss race runs it
    # once; `asyncio.run()` there cleared that worker's event-loop registry from under pytest-asyncio's
    # Runner, so every later async test on that worker failed with `RuntimeError: Runner.run() cannot be
    # called from a running event loop` (4 unrelated tests in one run). A throwaway loop that never registers
    # as the thread's current loop avoids it; `_setup()` only needs a loop to run ON.
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_setup())
    finally:
        loop.close()


def ensure_redis() -> dict[str, str]:
    """Real Redis Stack (same image docker-compose.yml's `redis` service
    uses — plain `redis:*` doesn't ship RediSearch, which the semantic cache
    and the Streams queue both need). Returns `redis_url`."""
    _require_docker("a real Redis")

    def _start() -> dict[str, Any]:
        from testcontainers.redis import RedisContainer

        container = RedisContainer(image="redis/redis-stack-server:latest")
        container.start()  # blocks on a real PING — see RedisContainer._connect
        host = container.get_container_host_ip()
        port = int(container.get_exposed_port(6379))
        return {
            "container_id": container.get_wrapped_container().id,
            "redis_url": f"redis://{host}:{port}",
        }

    return _acquire("redis", _start)


def ensure_qdrant() -> dict[str, str]:
    """Real Qdrant (same image docker-compose.yml's `qdrant` service uses)."""
    _require_docker("a real Qdrant")

    def _start() -> dict[str, Any]:
        from testcontainers.core.container import DockerContainer

        container = DockerContainer("qdrant/qdrant:latest").with_exposed_ports(6333)
        container.start()
        host = container.get_container_host_ip()
        port = int(container.get_exposed_port(6333))
        url = f"http://{host}:{port}"
        from qdrant_client import QdrantClient

        for attempt in range(30):
            try:
                QdrantClient(url=url).get_collections()
                break
            except Exception:  # noqa: BLE001 - retry until the timeout below
                if attempt == 29:
                    raise
                time.sleep(1)
        return {
            "container_id": container.get_wrapped_container().id,
            "qdrant_url": url,
        }

    return _acquire("qdrant", _start)


# The page every REAL-browser crawl test renders (tests/integration/test_web_crawler_live.py,
# tests/live/test_domain_crawl_tools_live.py) and the heading its markdown must contain. One shared
# definition, so the next time a third-party page changes under these tests it is a one-line fix.
#
# History: these tests used https://example.com and asserted "Example Domain". That page was
# redesigned between 2026-09-28 (CI green) and 2026-10-01 (CI red): it now ships no <h1> — the
# words survive only in <title> — plus a script that injects translated paragraphs into the DOM,
# so crawl4ai's markdown of the RENDERED page no longer contains them (reproduced locally against
# the pinned crawl4ai 0.9.3: "Thisdomainisforuse…" followed by Arabic). IANA's own reference page
# about example domains has a static <h1> and is documentation, not a demo placeholder. Its
# heading, "Example Domains", also contains the old assertion's text.
CRAWL_TARGET_URL = "https://www.iana.org/help/example-domains"
CRAWL_TARGET_HEADING = "Example Domains"


def ensure_crawl4ai() -> dict[str, str]:
    """Real crawl4ai server (the image docker-compose.yml's `crawl4ai` service uses): the headless-Chromium
    rendering backend behind `app/ingestion/web_crawler.py` (GRAPH_PATTERNS.md pattern 50). Driven through the
    generic `DockerContainer` API like `ensure_qdrant()` above (there is no first-party testcontainers module
    for it either).

    A fresh random `CRAWL4AI_API_TOKEN` per container start (`secrets.token_hex`, not a fixed test value), as
    docker-compose.yml's `crawl4ai` comment discloses: crawl4ai 0.9.0+ is secure-by-default, and without it
    the server binds loopback-only INSIDE its container, so the published port would connection-reset.
    `shm_size="1g"` (via `with_kwargs`, the one docker-run option `DockerContainer` has no builder for)
    because Chromium needs a real `/dev/shm` (crawl4ai's docker-run docs). Readiness is a real `GET /health`
    poll (no auth needed per crawl4ai's docs), in the same retry-loop shape as `ensure_ml_service()` above,
    budgeted to 60s: the margin `.github/workflows/ci.yml`'s earlier `docker run` + `timeout 60 ... curl`
    sidecar steps used (crawl4ai's docs only promise "up to 40s").

    Callers monkeypatch the result into `app.ingestion.web_crawler`'s
    `CRAWL4AI_SERVER_URL`/`CRAWL4AI_API_TOKEN` module globals (the `monkeypatch.setattr(some_module,
    "SOME_CONSTANT", ...)` shape `tests/live/conftest.py`'s `real_ollama_chat_model`-style fixtures use for
    `graph_module.CHAT_MODEL`) rather than exporting an env var: the token is only known at container start,
    long after `app/core/config.py`'s pydantic-settings constants were read at import. Returns
    `crawl4ai_server_url`/`crawl4ai_api_token`.
    """
    _require_docker("a real crawl4ai server")

    def _start() -> dict[str, Any]:
        import secrets

        import httpx
        from testcontainers.core.container import DockerContainer

        token = secrets.token_hex(32)
        container = (
            DockerContainer("unclecode/crawl4ai:0.9.3")
            .with_exposed_ports(11235)
            .with_env("CRAWL4AI_API_TOKEN", token)
            .with_kwargs(shm_size="1g")
        )
        container.start()
        host = container.get_container_host_ip()
        port = int(container.get_exposed_port(11235))
        url = f"http://{host}:{port}"
        for attempt in range(60):
            try:
                if httpx.get(f"{url}/health", timeout=2).status_code == 200:
                    break
            except Exception:  # noqa: BLE001,S110 - retry until the timeout below (same idiom as ensure_ml_service above)
                pass
            if attempt == 59:
                raise RuntimeError(f"crawl4ai never became healthy: {url}/health")
            time.sleep(1)
        return {
            "container_id": container.get_wrapped_container().id,
            "crawl4ai_server_url": url,
            "crawl4ai_api_token": token,
        }

    return _acquire("crawl4ai", _start)


def _build_image(dockerfile: str, tag: str) -> None:
    """Build `dockerfile` (repo-relative) as `tag` with the Docker CLI, which is BuildKit, and not with the `docker` SDK.

    The SDK's `images.build` drives the LEGACY builder, which rejects `RUN --mount` ("the --mount option requires BuildKit"); the Dockerfiles
    use BuildKit's cache mounts, as `docker build` and `docker compose` build them by default (ENGINEERING_PATTERNS.md pattern 76). The first
    push of that change failed every test that starts the ML service, in the integration tier and in the live tier, on exactly this.
    """
    docker_cli = shutil.which("docker")
    if docker_cli is None:
        raise RuntimeError("the docker CLI is needed to build the image (the daemon is reachable, so this is a PATH problem)")
    result = subprocess.run(  # noqa: S603 - a fixed argv of this repo's own paths, no shell
        [docker_cli, "build", "--file", dockerfile, "--tag", tag, str(_REPO_ROOT)],
        env={**os.environ, "DOCKER_BUILDKIT": "1"},
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"`docker build --file {dockerfile}` failed ({result.returncode}):\n{result.stdout[-1500:]}\n{result.stderr[-3500:]}"
        )


def ensure_ml_service() -> dict[str, str]:
    """Real reranker + prompt-injection-classifier service — the same
    image docker-compose.yml's own `ml-service` builds from
    docker/ml-service.Dockerfile. Needed purely to satisfy
    `GET /health/ready`'s `ml_service` check (app/api/health.py), added
    once that endpoint started checking it: the same "a real instance, not
    a stub, even though this test's own assertions don't depend on it"
    reasoning `ensure_qdrant()` above already documents for retrieval
    quality — `_wait_until_ready`-style polling loops in
    tests/integration/test_worker_scaling.py and tests/live/conftest.py
    block on this exact endpoint returning 200, which it never will while
    ANY checked dependency stays unreachable.

    No first-party testcontainers module (this is this repo's own image,
    not a published one) and no pre-built public tag to pull — built once
    here with the Docker CLI (BuildKit, see `_build_image`), then driven through the generic
    `DockerContainer` API like `ensure_qdrant()`. Both ONNX models this
    service loads (~22M params each, docker/ml-service/ml_service/) are small
    enough that a cold download inside the container's own healthcheck-
    covered startup stays cheap — unlike `ensure_ollama()`'s multi-GB chat
    model, no extra host-side model cache/`actions/cache` step is needed
    here. Returns `ml_service_url`.
    """
    _require_docker("a real reranker/prompt-guard ml-service")

    def _start() -> dict[str, Any]:
        import httpx
        from testcontainers.core.container import DockerContainer

        tag = "agent-core-demo-ml-service:test"
        _build_image("docker/ml-service.Dockerfile", tag)
        container = DockerContainer(tag).with_exposed_ports(80)
        container.start()
        host = container.get_container_host_ip()
        port = int(container.get_exposed_port(80))
        url = f"http://{host}:{port}"
        # Generous retry budget, mirroring docker-compose.yml's own
        # ml-service healthcheck `start_period` comment: covers a cold
        # pull of EVERY model on first startup. The service answers /health only
        # once its lifespan has finished, and that includes the downloads. It was
        # 90 s when there were two models (reranker, prompt guard); spec 014 adds
        # bge-small (embeddings) and all-MiniLM (the classifier's fallback), and a
        # cold start of the four measured 101 s on a quiet machine, so 90 s failed
        # every fixture user at once ("never became healthy") while the container
        # was in fact fine and became healthy moments later.
        for attempt in range(300):
            try:
                if httpx.get(f"{url}/health", timeout=2).status_code == 200:
                    break
            except Exception:  # noqa: BLE001,S110 - retry until the timeout below (same idiom as ensure_qdrant/ensure_postgres above, just checked outside the except since a non-200 response here doesn't raise)
                pass
            if attempt == 299:
                raise RuntimeError(f"ml-service never became healthy: {url}/health")
            time.sleep(1)
        return {
            "container_id": container.get_wrapped_container().id,
            "ml_service_url": url,
        }

    return _acquire("ml-service", _start)


_OLLAMA_HOME = Path.home() / ".cache" / "agent-core-demo-ollama-models"


def ensure_ollama(model: str, embed_model: str = "nomic-embed-text") -> dict[str, str]:
    """Real Ollama serving a real, small CHAT model plus a real embedding model in the SAME shared container: the
    expensive fixture, used only by `tests/live/`. It overrides testcontainers-python's default image pin
    (`ollama/ollama:0.1.44`, from before Qwen2.5) with `latest`: verified (see tests/live/conftest.py) that a
    current Ollama's OpenAI-compatible endpoint emits real native tool calls, the point of these tests, which
    an old pinned Ollama would silently defeat. Returns `openai_api_base` (Ollama's own `/v1`, no LiteLLM
    proxy; see GRAPH_PATTERNS.md 48), `model` (the chat model tag) and `embed_model` (the embedding model
    tag).

    `embed_model` defaults to `nomic-embed-text`, this app's production `EMBED_MODEL` default
    (`app/core/config.py`, `docker-compose.yml`'s `pull-models`). It was added when
    `tests/live/test_qdrant_real.py` (real `qdrant_store.hybrid_search`, which calls `embeddings.embed_text`,
    a REAL dense-embedding call that `ensure_qdrant()` alone can't satisfy) failed in CI with
    `openai.APIConnectionError`, since `test-integration` provisions no LLM at all; the test moved to
    `tests/live/` beside this fixture rather than adding a second, integration-only Ollama.

    `ollama_home=_OLLAMA_HOME` bind-mounts a stable HOST directory as the container's `/root/.ollama`
    (verified in `OllamaContainer.start()`: `with_volume_mapping(self.ollama_home, "/root/.ollama", "rw")`),
    so a fresh container gets both models instantly if a PRIOR run (this session's, a local one, or a CI run:
    .github/workflows/ci.yml's `test-live`/`promptfoo`/`deepeval` jobs and redteam.yml restore this path via
    `actions/cache/restore` keyed on the model tag(s) and save it with .github/actions/save-ollama-cache)
    already pulled them there. Purely a speed optimization: an empty or missing directory means `pull_model`
    downloads fresh.
    """
    _require_docker(f"a real Ollama serving {model}/{embed_model}")

    def _start() -> dict[str, Any]:
        from testcontainers.ollama import OllamaContainer

        _OLLAMA_HOME.mkdir(parents=True, exist_ok=True)
        container = OllamaContainer(image="ollama/ollama:latest", ollama_home=str(_OLLAMA_HOME))
        container.start()
        container.pull_model(model)  # instant if _OLLAMA_HOME already has this model — see above
        container.pull_model(embed_model)
        return {
            "container_id": container.get_wrapped_container().id,
            "openai_api_base": f"{container.get_endpoint()}/v1",
            "model": model,
            "embed_model": embed_model,
        }

    # Keyed by both models, not just "ollama": two suites requesting
    # different models must not silently hand each other the wrong ones back.
    key = f"ollama-{model}-{embed_model}".replace(":", "_")
    return _acquire(key, _start)
