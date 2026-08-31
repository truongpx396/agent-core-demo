"""Shared, Docker-backed real services for tests that need more than a fake
— `tests/agent/test_durable_checkpoint.py` (real Postgres), `tests/integration/`
(Postgres + Redis + Qdrant, no LLM), and `tests/live/` (adds a real, small
Ollama model). See GRAPH_PATTERNS.md pattern 48 for the full design writeup;
this module docstring covers the two mechanics every `ensure_*()` below
depends on.

**Skip, don't fail, when Docker isn't reachable.** Same UX
`tests/agent/test_durable_checkpoint.py`'s original `_require_postgres` already
established (a real connectivity probe, `pytest.skip` on failure) — the only
change is these no longer need `make up` already running: each starts its
own ephemeral container instead of probing an assumed-external one. A `pytest
-q` run on a laptop with no Docker daemon at all stays exactly as green and
fast as it is today.

**Cross-worker sharing under pytest-xdist is the whole point, and it's not
free.** `-n auto` runs each worker as a SEPARATE OS process — a plain
`@pytest.fixture(scope="session")` calling `SomeContainer().start()` would
start one container PER WORKER, which is precisely the waste a shared Ollama
endpoint is supposed to avoid (Ollama's own request queueing/thread pooling
is what lets many parallel workers hit ONE warm model concurrently, cutting
wall-clock time — see the Makefile's `test-live` target). So every `ensure_*`
here uses a `filelock.FileLock` over a small JSON cache file: the first
worker to grab the lock actually starts the container (and, for Ollama,
pulls the model) and caches its connection info there; every other worker —
and that same worker, on every later test — just reads the cache back.

The cache lives under a FIXED path (`tempfile.gettempdir()`, namespaced by a
hash of this repo's own root — see `_shared_root`), deliberately NOT
pytest's own per-invocation `tmp_path_factory` basetemp, even though that's
the directory the standard pytest-xdist "run a session fixture once" recipe
uses. Verified empirically that it doesn't fit this case: that recipe shares
a directory across workers WITHIN one invocation by having each worker
derive it from a value the xdist CONTROLLER computed and broadcast at
session start — but nothing broadcasts it back to the controller's own,
separate `pytest_sessionfinish` at session END, and a fresh
`TempPathFactory` built there lazily allocates the NEXT rotating number
instead of rediscovering the one workers actually used. A fixed path sidesteps
that rediscovery problem entirely, at the cost of needing to treat a cached
entry as possibly stale (see `_acquire`'s validation) rather than assuming a
fresh directory is always empty.

Given that, teardown can't be "whichever worker's fixture finishes first
tears it down" (the other workers would be pulled out from under) or
testcontainers' own default Ryuk-reaper-on-process-exit (Ryuk's reaper
session is per-Python-process; the FIRST worker to finish and exit would
reap a container the other workers still need). Both are disabled/replaced
deliberately: `TESTCONTAINERS_RYUK_DISABLED` is set before any container
starts, and `teardown_all()` — called from `tests/conftest.py`'s
`pytest_sessionfinish`, gated to only actually run once ALL workers have
reported back (see that hook) — removes every container this run started,
by id, read back from the same cache files. A container's id is genuinely
process-independent (unlike the live testcontainers Python object, which
only ever exists in whichever single worker process happened to start it),
so teardown from the xdist controller process — which itself never started
any container — works correctly.
"""
import hashlib
import json
import os
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
    """A fixed directory, shared by every worker of an xdist run AND by the
    xdist controller's own later `pytest_sessionfinish` — see module
    docstring for why this deliberately isn't pytest's own rotating
    `tmp_path_factory` basetemp. Namespaced by a hash of this repo's own root
    path so two independent clones of this repo on one machine (or CI
    runner) don't share containers — a real but unlikely-enough edge case
    that a cheap namespace, rather than solving cross-clone coordination
    properly, is the right amount of effort for a demo project's test infra.
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


def _acquire(key: str, start: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """Run `start()` exactly once across every xdist worker of this pytest
    run (see module docstring); every call, including the first, returns
    `start()`'s result. A cached entry is trusted only while its container is
    still actually running — the fixed cache path (see `_shared_root`) means
    a leftover file from an earlier, already-torn-down invocation (a killed
    `-x` run, a canceled CI job) is a real possibility, not just a theoretical
    one, so a dead reference is discarded and `start()` runs again rather
    than handing back a connection string nothing is listening on."""
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


def teardown_all() -> None:
    """Removes every container an `ensure_*()` call started this run, by the
    container id each cached alongside its connection info, and clears the
    cache files themselves (so a later, unrelated invocation never mistakes
    them for a still-live container — see `_acquire`'s own defense in depth
    for when it does anyway). Safe to call even when nothing was ever started
    (no cache files — the common case for a plain `pytest -q` run) or when
    Docker itself is gone by the time this runs. See tests/conftest.py's
    `pytest_sessionfinish` for the one place this is actually called from,
    and the module docstring for why teardown lives here instead of each
    container's own `.stop()`/Ryuk."""
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
    """Real Postgres 16 (same image docker-compose.yml's own `postgres`
    service uses), pre-seeded via the EXACT same `postgres-init/*.sql` files
    docker-compose feeds `/docker-entrypoint-initdb.d` — mounted read-only
    into that same path so the official postgres image's own entrypoint runs
    them, unmodified, in filename order. No hand-maintained duplicate schema:
    if a new postgres-init/*.sql file is added for docker-compose, this picks
    it up automatically. Returns `checkpointer_database_url`/
    `appdata_database_url` — the two databases those scripts create — ready
    to assign straight to `CHECKPOINTER_DATABASE_URL`/`APPDATA_DATABASE_URL`.
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
    """Runs `AsyncPostgresSaver.setup()` — the same call
    `app/agent/runtime.py::_open_checkpointer` makes on every real process
    startup, "idempotently, on every process including each
    independently-scaled agent_worker.py instance" per postgres-init/
    05-checkpointer-db.sql's own comment — exactly ONCE here, inside
    `ensure_postgres`'s own cross-worker lock, before any test gets a chance
    to call it again concurrently.

    Found empirically, not theoretically: running this file's tests under
    real `-n auto` parallelism (multiple xdist worker PROCESSES, each
    independently calling `init_graph_async()`/`init_graph_sync()` against
    this ONE shared container) reliably hit
    `psycopg.errors.UniqueViolation: duplicate key value violates unique
    constraint "checkpoint_migrations_pkey"` — `.setup()`'s own
    check-then-insert migration bookkeeping isn't safe against a genuine
    concurrent race between two FIRST-EVER callers. That's a real
    concurrency gap in `langgraph-checkpoint-postgres` itself (arguably a
    latent one in production too, if the api process and several
    agent_worker.py processes ever start up at the exact same instant
    against a brand-new database — this app has just never happened to hit
    that exact timing window), not something fixable here. Sidestepped,
    not patched: pre-running `.setup()` once while every worker is still
    serialized behind `ensure_postgres`'s own FileLock means every REAL
    test's own later `.setup()` call lands on an already-fully-migrated
    schema — a no-op read, not a racing write — regardless of how many
    workers call it at once afterward.
    """
    import asyncio

    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    async def _setup() -> None:
        async with AsyncPostgresSaver.from_conn_string(checkpointer_url) as saver:
            await saver.setup()

    asyncio.run(_setup())


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
    """Real Qdrant (same image docker-compose.yml's `qdrant` service uses).
    No dedicated testcontainers-python module for Qdrant as of this writing,
    so this drives it via the generic `DockerContainer` API — still part of
    the base `testcontainers` package, and testcontainers' own documented
    approach for an image without a first-party module. Readiness is a real
    `qdrant_client` call in a retry loop (mirrors this app's own
    `app/api/health.py::_check_qdrant`), not a log-line match, since exact
    startup log text is the more fragile of the two across image versions.
    Returns `qdrant_url`."""
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


_OLLAMA_HOME = Path.home() / ".cache" / "agent-core-demo-ollama-models"


def ensure_ollama(model: str) -> dict[str, str]:
    """Real Ollama serving a real, small model — the expensive one, used
    only by `tests/live/`. Deliberately overrides testcontainers-python's own
    default image pin (`ollama/ollama:0.1.44`, from before Qwen2.5 existed)
    with `latest`: verified empirically (see tests/live/conftest.py's own
    docstring) that a current Ollama's OpenAI-compatible endpoint emits real
    native tool calls, which is the entire point of these tests — an old
    pinned Ollama predating native tool-calling support would silently
    defeat that. Returns `openai_api_base` (Ollama's own `/v1`, no LiteLLM
    proxy in front — see GRAPH_PATTERNS.md pattern 48) and the model tag
    actually pulled.

    `ollama_home=_OLLAMA_HOME` bind-mounts a stable HOST directory as the
    container's own `/root/.ollama` (verified in `OllamaContainer.start()`'s
    own source: `with_volume_mapping(self.ollama_home, "/root/.ollama",
    "rw")`) — a fresh container still gets the model instantly if a PRIOR
    run (this session's, an earlier local run, or — see .github/workflows/
    ci.yml's `test-live`/`promptfoo`/`garak` jobs, each restoring this same
    path via `actions/cache` keyed on the model tag — an earlier CI run)
    already pulled it into this same directory. Purely a speed optimization,
    not a correctness dependency: an empty/missing directory just means
    `pull_model` downloads fresh, exactly as it did before this existed.
    """
    _require_docker(f"a real Ollama serving {model}")

    def _start() -> dict[str, Any]:
        from testcontainers.ollama import OllamaContainer

        _OLLAMA_HOME.mkdir(parents=True, exist_ok=True)
        container = OllamaContainer(image="ollama/ollama:latest", ollama_home=str(_OLLAMA_HOME))
        container.start()
        container.pull_model(model)  # instant if _OLLAMA_HOME already has this model — see above
        return {
            "container_id": container.get_wrapped_container().id,
            "openai_api_base": f"{container.get_endpoint()}/v1",
            "model": model,
        }

    # Keyed by model, not just "ollama": two suites requesting different
    # models must not silently hand each other the wrong one back.
    return _acquire(f"ollama-{model.replace(':', '_')}", _start)
