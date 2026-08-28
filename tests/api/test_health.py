"""Tests for app/api/health.py's readiness checks — no live services: every
dependency client is monkeypatched to a fake that either succeeds or
raises, the same "fake the collaborator, not the network" approach
tests/agent/test_sql_store.py/tests/agent/test_tools.py already use.

No pytest-asyncio plugin is installed in this project (see
tests/turns/test_queue.py's own module docstring) — async calls go through
`asyncio.run(...)` directly, same as every other async test here.
"""
import asyncio
import time

from app.agent import sql_store
from app.api import health
from app.retrieval import qdrant_store
from app.turns import queue


class _FakeConnection:
    def __init__(self, *, fails=False):
        self._fails = fails

    def execute(self, sql):
        if self._fails:
            raise ConnectionError("db unreachable")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeQdrantClient:
    def __init__(self, *, fails=False):
        self._fails = fails

    def get_collections(self):
        if self._fails:
            raise ConnectionError("qdrant unreachable")


class _HangingQdrantClient:
    def get_collections(self):
        # Longer than the patched _CHECK_TIMEOUT_SECONDS below but short
        # enough not to leave a slow orphaned thread lingering past this
        # test — asyncio.to_thread's underlying thread can't actually be
        # killed once wait_for gives up on awaiting it, only abandoned.
        time.sleep(0.3)


class _FakeRedisClient:
    def __init__(self, *, fails=False):
        self._fails = fails

    async def ping(self):
        if self._fails:
            raise ConnectionError("redis unreachable")
        return True


def _patch_all_healthy(monkeypatch):
    monkeypatch.setattr(sql_store, "get_connection", lambda: _FakeConnection())
    monkeypatch.setattr(health.psycopg, "connect", lambda *a, **k: _FakeConnection())
    monkeypatch.setattr(qdrant_store, "get_client", lambda: _FakeQdrantClient())
    monkeypatch.setattr(queue, "get_client", lambda: _FakeRedisClient())


class TestCheckDependencies:
    def test_all_healthy(self, monkeypatch):
        _patch_all_healthy(monkeypatch)
        result = asyncio.run(health.check_dependencies())
        assert result == {
            "qdrant": True,
            "appdata_postgres": True,
            "checkpointer_postgres": True,
            "redis": True,
        }

    def test_one_dependency_down_reports_only_that_one_as_false(self, monkeypatch):
        _patch_all_healthy(monkeypatch)
        monkeypatch.setattr(qdrant_store, "get_client", lambda: _FakeQdrantClient(fails=True))
        result = asyncio.run(health.check_dependencies())
        assert result["qdrant"] is False
        assert result["appdata_postgres"] is True
        assert result["checkpointer_postgres"] is True
        assert result["redis"] is True

    def test_every_dependency_down(self, monkeypatch):
        monkeypatch.setattr(
            sql_store, "get_connection", lambda: _FakeConnection(fails=True)
        )
        monkeypatch.setattr(
            health.psycopg, "connect", lambda *a, **k: _FakeConnection(fails=True)
        )
        monkeypatch.setattr(qdrant_store, "get_client", lambda: _FakeQdrantClient(fails=True))
        monkeypatch.setattr(queue, "get_client", lambda: _FakeRedisClient(fails=True))
        result = asyncio.run(health.check_dependencies())
        assert not any(result.values())

    def test_a_hung_check_is_bounded_by_its_own_timeout(self, monkeypatch):
        """A dependency that never returns must not hang readiness itself
        — this is the entire reason each check is wrapped in
        asyncio.wait_for rather than awaited directly."""
        _patch_all_healthy(monkeypatch)
        monkeypatch.setattr(health, "_CHECK_TIMEOUT_SECONDS", 0.05)
        monkeypatch.setattr(qdrant_store, "get_client", lambda: _HangingQdrantClient())

        result = asyncio.run(asyncio.wait_for(health.check_dependencies(), timeout=2.0))
        assert result["qdrant"] is False
