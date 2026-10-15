"""Tests for app/agent/memory.py::delete_memories — mocks qdrant_store's
count_by_filter/delete_by_filter (same boundary tests/agent/test_tools.py mocks
qdrant_store.upsert/hybrid_search at) so these stay hermetic.

`delete_memories` is `async def` now (it awaits `qdrant_store`'s own now-
async `count_by_filter`/`delete_by_filter`, a real `AsyncQdrantClient`), so
every call below runs through `asyncio.run(...)`, this repo's established
pattern for exercising async code from a plain `def test_...`.
"""

import pytest

from app.agent import memory
from app.retrieval import qdrant_store
from tests.conftest import TEST_CTX


def _mock_qdrant(monkeypatch, count=3):
    captured = {}

    async def fake_count(f):
        captured["count_filter"] = f
        return count

    async def fake_delete(f):
        captured["delete_filter"] = f

    monkeypatch.setattr(qdrant_store, "count_by_filter", fake_count)
    monkeypatch.setattr(qdrant_store, "delete_by_filter", fake_delete)
    return captured


class TestSelectorValidation:
    async def test_refuses_without_ctx(self):
        with pytest.raises(ValueError):
            await memory.delete_memories(None, memory_id="abc")

    async def test_refuses_when_neither_selector_given(self, monkeypatch):
        captured = _mock_qdrant(monkeypatch)
        with pytest.raises(ValueError):
            await memory.delete_memories(TEST_CTX)
        assert "count_filter" not in captured  # never reached Qdrant

    async def test_refuses_when_both_selectors_given(self, monkeypatch):
        captured = _mock_qdrant(monkeypatch)
        with pytest.raises(ValueError):
            await memory.delete_memories(TEST_CTX, memory_id="abc", older_than_days=30)
        assert "count_filter" not in captured


class TestScoping:
    async def test_always_scopes_to_ctx_tenant(self, monkeypatch):
        captured = _mock_qdrant(monkeypatch)
        await memory.delete_memories(TEST_CTX, memory_id="abc")
        must = captured["delete_filter"].must
        values = {c.key: c.match.value for c in must if getattr(c, "match", None) is not None}
        assert values["tenant"] == TEST_CTX["tenant"]
        assert values["kind"] == "memory"

    async def test_defaults_to_deleting_the_callers_own_memories(self, monkeypatch):
        captured = _mock_qdrant(monkeypatch)
        await memory.delete_memories(TEST_CTX, memory_id="abc")
        must = captured["delete_filter"].must
        values = {c.key: c.match.value for c in must if getattr(c, "match", None) is not None}
        assert values["owner"] == TEST_CTX["principal"]

    async def test_target_principal_overrides_the_default_but_stays_in_tenant(self, monkeypatch):
        captured = _mock_qdrant(monkeypatch)
        await memory.delete_memories(TEST_CTX, memory_id="abc", target_principal="someone-else")
        must = captured["delete_filter"].must
        values = {c.key: c.match.value for c in must if getattr(c, "match", None) is not None}
        assert values["owner"] == "someone-else"
        assert values["tenant"] == TEST_CTX["tenant"]  # never escapes the tenant

    async def test_memory_id_selector_uses_has_id_condition(self, monkeypatch):
        captured = _mock_qdrant(monkeypatch)
        await memory.delete_memories(TEST_CTX, memory_id="the-specific-id")
        has_id = [c for c in captured["delete_filter"].must if getattr(c, "has_id", None)]
        assert has_id and has_id[0].has_id == ["the-specific-id"]

    async def test_older_than_days_selector_uses_a_range_condition(self, monkeypatch):
        captured = _mock_qdrant(monkeypatch)
        await memory.delete_memories(TEST_CTX, older_than_days=90)
        range_conditions = [c for c in captured["delete_filter"].must if c.key == "created_at"]
        assert len(range_conditions) == 1
        assert range_conditions[0].range.lt is not None


class TestReturnValueAndAudit:
    async def test_returns_the_count_from_count_by_filter(self, monkeypatch):
        _mock_qdrant(monkeypatch, count=7)
        result = await memory.delete_memories(TEST_CTX, memory_id="abc")
        assert result == 7

    async def test_counts_before_deleting_using_the_same_filter(self, monkeypatch):
        captured = _mock_qdrant(monkeypatch)
        await memory.delete_memories(TEST_CTX, memory_id="abc")
        assert captured["count_filter"] == captured["delete_filter"]

    async def test_records_a_deleted_outcome_metric(self, monkeypatch):
        from app.core import metrics
        from tests.conftest import metric_value

        _mock_qdrant(monkeypatch)
        before = metric_value(metrics.agent_memory_deletion_total, outcome="deleted")

        await memory.delete_memories(TEST_CTX, memory_id="abc")

        after = metric_value(metrics.agent_memory_deletion_total, outcome="deleted")
        assert after == before + 1

    async def test_records_a_refused_outcome_metric_on_ambiguous_selector(self):
        from app.core import metrics
        from tests.conftest import metric_value

        before = metric_value(metrics.agent_memory_deletion_total, outcome="refused")

        with pytest.raises(ValueError):
            await memory.delete_memories(TEST_CTX)

        after = metric_value(metrics.agent_memory_deletion_total, outcome="refused")
        assert after == before + 1
