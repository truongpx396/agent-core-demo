"""Tests for app/tools.py — mainly `add_note`/`remember` (the mutating
tools) and the tenant/owner isolation `search_docs`/`add_note`/`remember`
all enforce via app/security.py's Policy.

`search_docs`/`calculator`'s *routing* is exercised indirectly throughout
test_graph_integration.py already; their ctx-enforcement specifically, and
`add_note`/`remember`'s implementations, get direct coverage here since
nothing else calls them (add_note/remember are gated behind human_approval
in every graph scenario, so a graph-level test would need to drive a full
pause/resume cycle just to reach the implementation).
"""
import pytest

from app import qdrant_store, tools
from app.tools import (
    TOOL_CAPABILITIES,
    TOOLS,
    AddNoteArgs,
    RememberArgs,
    Topic,
    add_note,
    recall_memories,
    remember,
    search_docs,
)
from tests.conftest import TEST_CTX

_OTHER_TENANT_CTX = {"tenant": "other-co", "principal": "test-user", "claims": {}}


def _cfg(ctx=TEST_CTX):
    return {"configurable": {"ctx": ctx}}


class TestAddNoteArgsValidation:
    def test_blank_title_rejected(self):
        with pytest.raises(ValueError):
            AddNoteArgs(title="   ", content="some content", topic=Topic.company)

    def test_blank_content_rejected(self):
        with pytest.raises(ValueError):
            AddNoteArgs(title="a title", content="  ", topic=Topic.company)

    def test_invalid_topic_rejected(self):
        with pytest.raises(ValueError):
            AddNoteArgs(title="a title", content="content", topic="not_a_real_topic")

    def test_valid_args_construct(self):
        args = AddNoteArgs(title="Refunds", content="30-day window.", topic=Topic.company)
        assert args.title == "Refunds"
        assert args.topic is Topic.company


class TestAddNoteImpl:
    def test_embeds_and_upserts_a_single_point(self, monkeypatch):
        captured = {}

        def fake_embed_text(text):
            captured["embedded_text"] = text
            return [0.1, 0.2, 0.3]

        def fake_upsert(points):
            captured["points"] = points

        monkeypatch.setattr(tools, "embed_text", fake_embed_text)
        # `tools.qdrant_store` is the same module object as the `qdrant_store`
        # imported above (tools.py does `from app import qdrant_store`) — this
        # patches the one `.upsert` attribute both names resolve to.
        monkeypatch.setattr(qdrant_store, "upsert", fake_upsert)

        result = tools._add_note_impl("Refunds", "30-day window.", Topic.company, TEST_CTX)

        assert "points" in captured
        assert len(captured["points"]) == 1
        point = captured["points"][0]
        assert point.vector == [0.1, 0.2, 0.3]
        assert point.payload["topic"] == "company"
        assert point.payload["title"] == "Refunds"
        assert point.payload["kind"] == "document"
        assert point.payload["tenant"] == TEST_CTX["tenant"]
        assert "Refunds" in point.payload["text"]
        assert "30-day window." in point.payload["text"]
        assert "Refunds" in result
        assert "company" in result

    def test_each_call_gets_a_fresh_id_never_overwriting(self, monkeypatch):
        """A fresh UUID id per call means add_note can only ever append a
        point, never target/overwrite an existing one by guessing its id —
        see _add_note_impl's docstring."""
        seen_ids = []

        def fake_upsert(points):
            seen_ids.append(points[0].id)

        monkeypatch.setattr(tools, "embed_text", lambda text: [0.0])
        monkeypatch.setattr(qdrant_store, "upsert", fake_upsert)

        tools._add_note_impl("A", "one", Topic.qdrant, TEST_CTX)
        tools._add_note_impl("B", "two", Topic.qdrant, TEST_CTX)

        assert len(seen_ids) == 2
        assert seen_ids[0] != seen_ids[1]

    def test_run_with_timeout_wraps_the_impl(self, monkeypatch):
        """add_note (the @tool-decorated function) must route through
        _run_with_timeout like search_docs/calculator do — a hung
        embedding/Qdrant call shouldn't be able to stall the turn
        indefinitely either."""
        monkeypatch.setattr(tools, "embed_text", lambda text: [0.0])
        monkeypatch.setattr(qdrant_store, "upsert", lambda points: None)

        result = add_note.invoke(
            {"title": "T", "content": "C", "topic": "langgraph"}, config=_cfg()
        )
        assert "T" in result

    def test_refuses_without_ctx(self, monkeypatch):
        upserted = []
        monkeypatch.setattr(qdrant_store, "upsert", lambda points: upserted.append(points))

        result = add_note.invoke({"title": "T", "content": "C", "topic": "langgraph"})

        assert "Refused" in result
        assert upserted == []  # never reached Qdrant


class TestSearchDocsCtx:
    def test_refuses_without_ctx(self):
        result = search_docs.invoke({"query": "anything"})
        assert "Refused" in result

    def test_applies_tenant_prefilter(self, monkeypatch):
        captured = {}

        def fake_search(vector, topic=None, k=3, tenant_filter=None):
            captured["tenant_filter"] = tenant_filter
            return []

        monkeypatch.setattr(tools, "embed_text", lambda text: [0.0])
        monkeypatch.setattr(qdrant_store, "search", fake_search)

        search_docs.invoke({"query": "hello"}, config=_cfg())

        # Every FieldCondition in the lowered filter came from THIS ctx's
        # tenant, and the filter scopes to documents (never memories).
        must = captured["tenant_filter"].must
        values = {c.key: c.match.value for c in must}
        assert values["tenant"] == TEST_CTX["tenant"]
        assert values["kind"] == "document"

    def test_two_different_tenants_get_different_filters(self, monkeypatch):
        """The concrete manifestation of tenant isolation at the query
        layer: two tenants never produce the same server-side predicate,
        so one tenant's search can't be satisfied by another's data —
        this is what "pre-filter, not post-filter" (app/qdrant_store.py)
        actually buys."""
        seen_filters = []

        def fake_search(vector, topic=None, k=3, tenant_filter=None):
            seen_filters.append(tenant_filter)
            return []

        monkeypatch.setattr(tools, "embed_text", lambda text: [0.0])
        monkeypatch.setattr(qdrant_store, "search", fake_search)

        search_docs.invoke({"query": "hello"}, config=_cfg(TEST_CTX))
        search_docs.invoke({"query": "hello"}, config=_cfg(_OTHER_TENANT_CTX))

        tenants = [
            next(c.match.value for c in f.must if c.key == "tenant") for f in seen_filters
        ]
        assert tenants[0] != tenants[1]


class TestRememberArgsValidation:
    def test_blank_content_rejected(self):
        with pytest.raises(ValueError):
            RememberArgs(content="   ")

    def test_valid_content_constructs(self):
        assert RememberArgs(content="likes dark roast coffee").content


class TestRememberImpl:
    def test_writes_a_memory_owned_by_the_principal(self, monkeypatch):
        captured = {}

        monkeypatch.setattr(tools, "embed_text", lambda text: [0.4])
        monkeypatch.setattr(qdrant_store, "upsert", lambda points: captured.update(points=points))

        result = tools._remember_impl("likes dark roast coffee", TEST_CTX)

        point = captured["points"][0]
        assert point.payload["kind"] == "memory"
        assert point.payload["tenant"] == TEST_CTX["tenant"]
        assert point.payload["owner"] == TEST_CTX["principal"]
        assert point.payload["text"] == "likes dark roast coffee"
        assert result == "Remembered."

    def test_refuses_without_ctx(self, monkeypatch):
        upserted = []
        monkeypatch.setattr(qdrant_store, "upsert", lambda points: upserted.append(points))

        result = remember.invoke({"content": "likes dark roast coffee"})

        assert "Refused" in result
        assert upserted == []


class TestRecallMemories:
    def test_returns_empty_string_without_ctx(self):
        assert recall_memories(None, "coffee") == ""

    def test_scopes_to_tenant_and_owner(self, monkeypatch):
        captured = {}

        def fake_search(vector, topic=None, k=3, tenant_filter=None):
            captured["tenant_filter"] = tenant_filter
            return []

        monkeypatch.setattr(tools, "embed_text", lambda text: [0.0])
        monkeypatch.setattr(qdrant_store, "search", fake_search)

        recall_memories(TEST_CTX, "coffee")

        must = captured["tenant_filter"].must
        values = {c.key: c.match.value for c in must}
        assert values["tenant"] == TEST_CTX["tenant"]
        assert values["kind"] == "memory"
        assert values["owner"] == TEST_CTX["principal"]

    def test_two_different_principals_get_different_filters(self, monkeypatch):
        """The concrete manifestation of "a memory belongs to whoever wrote
        it, not the tenant at large" — two principals in the SAME tenant
        must produce filters that scope to different owners."""
        seen_filters = []

        def fake_search(vector, topic=None, k=3, tenant_filter=None):
            seen_filters.append(tenant_filter)
            return []

        monkeypatch.setattr(tools, "embed_text", lambda text: [0.0])
        monkeypatch.setattr(qdrant_store, "search", fake_search)

        other_principal_same_tenant = {**TEST_CTX, "principal": "someone-else"}
        recall_memories(TEST_CTX, "coffee")
        recall_memories(other_principal_same_tenant, "coffee")

        owners = [
            next(c.match.value for c in f.must if c.key == "owner") for f in seen_filters
        ]
        assert owners[0] != owners[1]


class TestToolCapabilities:
    def test_read_only_tools_declared_correctly(self):
        assert TOOL_CAPABILITIES["search_docs"] == "read_only"
        assert TOOL_CAPABILITIES["calculator"] == "read_only"

    def test_mutating_tools_declared_correctly(self):
        assert TOOL_CAPABILITIES["add_note"] == "mutating"
        assert TOOL_CAPABILITIES["remember"] == "mutating"

    def test_every_tool_in_TOOLS_has_a_declared_capability(self):
        """Fail closed only helps for tools someone *forgot* to declare —
        it shouldn't be an excuse to skip declaring the ones that exist."""
        declared = set(TOOL_CAPABILITIES)
        registered = {t.name for t in TOOLS}
        assert registered <= declared

    def test_add_note_and_remember_are_registered_in_TOOLS(self):
        names = {t.name for t in TOOLS}
        assert "add_note" in names
        assert "remember" in names
