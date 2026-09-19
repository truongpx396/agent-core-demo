"""Tests for app/api/health.py's readiness checks — no live services: every
dependency client is monkeypatched to a fake that either succeeds or
raises, the same "fake the collaborator, not the network" approach
tests/agent/test_sql_store.py/tests/agent/test_tools.py already use.

No pytest-asyncio plugin is installed in this project (see
tests/job_queue/test_queue.py's own module docstring) — async calls go through
`asyncio.run(...)` directly, same as every other async test here.
"""
import asyncio
from contextlib import asynccontextmanager

from app.agent import sql_store
from app.api import health
from app.retrieval import qdrant_store
from app.job_queue import queue


class _FakeConnection:
    def __init__(self, *, fails=False):
        self._fails = fails

    async def execute(self, sql):
        if self._fails:
            raise ConnectionError("db unreachable")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _fake_get_connection(*, fails=False):
    @asynccontextmanager
    async def get_connection():
        yield _FakeConnection(fails=fails)

    return get_connection


async def _fake_async_connect(*a, fails=False, **kw):
    return _FakeConnection(fails=fails)


class _FakeQdrantClient:
    def __init__(self, *, fails=False):
        self._fails = fails

    async def get_collections(self):
        if self._fails:
            raise ConnectionError("qdrant unreachable")


class _HangingQdrantClient:
    async def get_collections(self):
        # Longer than the patched _CHECK_TIMEOUT_SECONDS below but short
        # enough not to slow this test down much — a real `asyncio.sleep`,
        # genuinely cancellable by asyncio.wait_for's own timeout, unlike
        # a blocking `time.sleep` (which used to matter here because the
        # old sync `_check_qdrant` ran via `asyncio.to_thread` — now that
        # it's `async def` and awaited directly, a blocking sleep would
        # stall the whole event loop, including wait_for's own timer).
        await asyncio.sleep(0.3)


class _FakeRedisClient:
    def __init__(self, *, fails=False):
        self._fails = fails

    async def ping(self):
        if self._fails:
            raise ConnectionError("redis unreachable")
        return True


class _FakeMlServiceResponse:
    def raise_for_status(self):
        pass


class _FakeMlServiceClient:
    """Stands in for `httpx.AsyncClient` — matching this file's own
    "patch the low-level client constructor, not the check function"
    pattern (see `sql_store.get_connection`/`health.psycopg.connect`
    above)."""

    def __init__(self, *, fails=False):
        self._fails = fails

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url):
        if self._fails:
            raise ConnectionError("ml-service unreachable")
        return _FakeMlServiceResponse()


def _patch_all_healthy(monkeypatch):
    monkeypatch.setattr(sql_store, "get_connection", _fake_get_connection())
    monkeypatch.setattr(
        health.psycopg.AsyncConnection, "connect", lambda *a, **k: _fake_async_connect()
    )
    monkeypatch.setattr(qdrant_store, "get_client", lambda: _FakeQdrantClient())
    monkeypatch.setattr(queue, "get_client", lambda: _FakeRedisClient())
    monkeypatch.setattr(health.httpx, "AsyncClient", lambda **kw: _FakeMlServiceClient())


class TestCheckDependencies:
    async def test_all_healthy(self, monkeypatch):
        _patch_all_healthy(monkeypatch)
        result = await health.check_dependencies()
        assert result == {
            "qdrant": True,
            "appdata_postgres": True,
            "checkpointer_postgres": True,
            "redis": True,
            "ml_service": True,
        }

    async def test_one_dependency_down_reports_only_that_one_as_false(self, monkeypatch):
        _patch_all_healthy(monkeypatch)
        monkeypatch.setattr(qdrant_store, "get_client", lambda: _FakeQdrantClient(fails=True))
        result = await health.check_dependencies()
        assert result["qdrant"] is False
        assert result["appdata_postgres"] is True
        assert result["checkpointer_postgres"] is True
        assert result["redis"] is True
        assert result["ml_service"] is True

    async def test_every_dependency_down(self, monkeypatch):
        monkeypatch.setattr(sql_store, "get_connection", _fake_get_connection(fails=True))
        monkeypatch.setattr(
            health.psycopg.AsyncConnection,
            "connect",
            lambda *a, **k: _fake_async_connect(fails=True),
        )
        monkeypatch.setattr(qdrant_store, "get_client", lambda: _FakeQdrantClient(fails=True))
        monkeypatch.setattr(queue, "get_client", lambda: _FakeRedisClient(fails=True))
        monkeypatch.setattr(health.httpx, "AsyncClient", lambda **kw: _FakeMlServiceClient(fails=True))
        result = await health.check_dependencies()
        assert not any(result.values())

    async def test_a_hung_check_is_bounded_by_its_own_timeout(self, monkeypatch):
        """A dependency that never returns must not hang readiness itself
        — this is the entire reason each check is wrapped in
        asyncio.wait_for rather than awaited directly."""
        _patch_all_healthy(monkeypatch)
        monkeypatch.setattr(health, "_CHECK_TIMEOUT_SECONDS", 0.05)
        monkeypatch.setattr(qdrant_store, "get_client", lambda: _HangingQdrantClient())

        result = await asyncio.wait_for(health.check_dependencies(), timeout=2.0)
        assert result["qdrant"] is False
