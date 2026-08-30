"""Tests for app/agent/tools.py — mainly `add_note`/`remember` (the mutating
tools) and the tenant/owner isolation `search_docs`/`add_note`/`remember`
all enforce via app/core/security.py's Policy.

`search_docs`/`calculator`'s *routing* is exercised indirectly throughout
test_graph_integration.py already; their ctx-enforcement specifically, and
`add_note`/`remember`'s implementations, get direct coverage here since
nothing else calls them (add_note/remember are gated behind human_approval
in every graph scenario, so a graph-level test would need to drive a full
pause/resume cycle just to reach the implementation).
"""
import time
from pathlib import Path

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, SystemMessage

from app.agent import skills as skills_module
from app.agent import sql_store, tools
from app.agent.skills import SkillRecord
from app.agent.subagents import SubagentRecord
from app.agent.tools import (
    _ALL_TOOL_NAMES,
    TOOL_CAPABILITIES,
    TOOLS,
    AddNoteArgs,
    AskClarificationArgs,
    RememberArgs,
    SubagentName,
    Topic,
    _resolve_subagent_tools,
    _run_subagent_impl,
    add_note,
    ask_clarification,
    query_employees,
    recall_memories,
    remember,
    run_subagent,
    search_docs,
    skill_search,
    use_skill,
)
from app.core import metrics
from app.core.config import SKILLS_COLLECTION
from app.retrieval import qdrant_store
from tests.conftest import TEST_CTX, metric_value

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
        monkeypatch.setattr(tools, "embed_sparse", lambda text: ([1, 2], [0.5, 0.5]))
        # `tools.qdrant_store` is the same module object as the `qdrant_store`
        # imported above (tools.py does `from app.retrieval import qdrant_store`) —
        # this patches the one `.upsert` attribute both names resolve to.
        monkeypatch.setattr(qdrant_store, "upsert", fake_upsert)

        result = tools._add_note_impl("Refunds", "30-day window.", Topic.company, TEST_CTX)

        assert "points" in captured
        assert len(captured["points"]) == 1
        point = captured["points"][0]
        assert point.vector["dense"] == [0.1, 0.2, 0.3]
        assert point.vector["sparse"].indices == [1, 2]
        assert point.payload["topic"] == "company"
        assert point.payload["title"] == "Refunds"
        assert point.payload["kind"] == "document"
        assert point.payload["tenant"] == TEST_CTX["tenant"]
        assert "Refunds" in point.payload["text"]
        assert "30-day window." in point.payload["text"]
        assert "Refunds" in result
        assert "company" in result

    def test_sparse_embedding_failure_degrades_to_dense_only_point(self, monkeypatch):
        """A local BM25 model hiccup must not block a human-approved write
        — see _sparse_vector_or_none's docstring."""
        captured = {}

        monkeypatch.setattr(tools, "embed_text", lambda text: [0.1])

        def failing_sparse(text):
            raise RuntimeError("model not loaded")

        monkeypatch.setattr(tools, "embed_sparse", failing_sparse)
        monkeypatch.setattr(qdrant_store, "upsert", lambda points: captured.update(points=points))

        tools._add_note_impl("Refunds", "30-day window.", Topic.company, TEST_CTX)

        point = captured["points"][0]
        assert "sparse" not in point.vector
        assert point.vector["dense"] == [0.1]

    def test_each_call_gets_a_fresh_id_never_overwriting(self, monkeypatch):
        """A fresh UUID id per call means add_note can only ever append a
        point, never target/overwrite an existing one by guessing its id —
        see _add_note_impl's docstring."""
        seen_ids = []

        def fake_upsert(points):
            seen_ids.append(points[0].id)

        monkeypatch.setattr(tools, "embed_text", lambda text: [0.0])
        monkeypatch.setattr(tools, "embed_sparse", lambda text: ([1], [1.0]))
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
        monkeypatch.setattr(tools, "embed_sparse", lambda text: ([1], [1.0]))
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


class TestRunWithTimeoutScrubbing:
    """_run_with_timeout (app/agent/tools.py) is the one chokepoint every
    read/write tool impl funnels through — proving scrubbing is wired in
    HERE, not just that app/core/scrubbing.py::scrub() works in isolation
    (already covered in tests/core/test_scrubbing.py), is what actually
    guards against a future tool bypassing it (GRAPH_PATTERNS.md
    pattern 32)."""

    def test_a_credential_shaped_result_is_scrubbed(self):
        result = tools._run_with_timeout(lambda: "here is a key: sk-abcdefghijklmnopqrstuvwx")
        assert "sk-abcdefghijklmnopqrstuvwx" not in result
        assert "[REDACTED]" in result

    def test_an_ordinary_result_is_unaffected(self):
        result = tools._run_with_timeout(lambda: "Refunds — 30-day window.")
        assert result == "Refunds — 30-day window."

    def test_a_non_string_result_passes_through_unscrubbed(self):
        # No current tool returns a non-str, but _run_with_timeout itself
        # doesn't assume one — scrub() only applies to str results.
        result = tools._run_with_timeout(lambda: 42)
        assert result == 42


class TestSearchDocsCtx:
    def test_refuses_without_ctx(self):
        result = search_docs.invoke({"query": "anything"})
        assert "Refused" in result

    def test_applies_tenant_prefilter(self, monkeypatch):
        captured = {}

        def fake_hybrid_search(query_text, topic=None, k=None, tenant_filter=None, rerank_results=True, doc_ids=None):
            captured["tenant_filter"] = tenant_filter
            return []

        monkeypatch.setattr(qdrant_store, "hybrid_search", fake_hybrid_search)

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
        this is what "pre-filter, not post-filter" (app/retrieval/qdrant_store.py)
        actually buys."""
        seen_filters = []

        def fake_hybrid_search(query_text, topic=None, k=None, tenant_filter=None, rerank_results=True, doc_ids=None):
            seen_filters.append(tenant_filter)
            return []

        monkeypatch.setattr(qdrant_store, "hybrid_search", fake_hybrid_search)

        search_docs.invoke({"query": "hello"}, config=_cfg(TEST_CTX))
        search_docs.invoke({"query": "hello"}, config=_cfg(_OTHER_TENANT_CTX))

        tenants = [
            next(c.match.value for c in f.must if c.key == "tenant") for f in seen_filters
        ]
        assert tenants[0] != tenants[1]

    def test_doc_ids_is_anded_onto_the_tenant_filter_not_a_replacement(self, monkeypatch):
        """doc_ids narrows an already tenant-scoped query — it must never
        be able to substitute for the tenant predicate. Checked at the
        qdrant_store.hybrid_search boundary (mocked) since that's where
        _build_filter actually composes the two."""
        captured = {}

        def fake_hybrid_search(
            query_text, topic=None, k=None, tenant_filter=None, rerank_results=True, doc_ids=None
        ):
            captured["tenant_filter"] = tenant_filter
            captured["doc_ids"] = doc_ids
            return []

        monkeypatch.setattr(qdrant_store, "hybrid_search", fake_hybrid_search)

        search_docs.invoke({"query": "hello", "doc_ids": ["abc", "def"]}, config=_cfg())

        assert captured["doc_ids"] == ["abc", "def"]
        # The tenant filter is passed through UNCHANGED alongside doc_ids
        # — _build_filter (app/retrieval/qdrant_store.py) is what ANDs them
        # together server-side, not search_docs replacing one with the other.
        must = captured["tenant_filter"].must
        values = {c.key: c.match.value for c in must}
        assert values["tenant"] == TEST_CTX["tenant"]

    def test_doc_ids_actually_narrows_the_qdrant_filter(self):
        """Unmocked, at the real qdrant_store._build_filter boundary:
        doc_ids becomes a HasIdCondition ANDed alongside the tenant/kind
        predicate — not a filter that could stand in for it."""
        from app.core.security import DEFAULT_POLICY

        tenant_filter = DEFAULT_POLICY.lower(TEST_CTX, "documents")
        built = qdrant_store._build_filter(None, tenant_filter, doc_ids=["abc", "def"])

        assert any(getattr(c, "has_id", None) == ["abc", "def"] for c in built.must)
        assert any(
            getattr(c, "key", None) == "tenant" and c.match.value == TEST_CTX["tenant"]
            for c in built.must
        )


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

        def fake_hybrid_search(query_text, topic=None, k=None, tenant_filter=None, rerank_results=True, doc_ids=None):
            captured["tenant_filter"] = tenant_filter
            captured["rerank_results"] = rerank_results
            return []

        monkeypatch.setattr(qdrant_store, "hybrid_search", fake_hybrid_search)

        recall_memories(TEST_CTX, "coffee")

        must = captured["tenant_filter"].must
        values = {c.key: c.match.value for c in must if c.match is not None}
        assert values["tenant"] == TEST_CTX["tenant"]
        assert values["kind"] == "memory"
        assert values["owner"] == TEST_CTX["principal"]
        # Reranking is deliberately skipped for memory recall — see
        # _memory_hits's docstring.
        assert captured["rerank_results"] is False

    def test_two_different_principals_get_different_filters(self, monkeypatch):
        """The concrete manifestation of "a memory belongs to whoever wrote
        it, not the tenant at large" — two principals in the SAME tenant
        must produce filters that scope to different owners."""
        seen_filters = []

        def fake_hybrid_search(query_text, topic=None, k=None, tenant_filter=None, rerank_results=True, doc_ids=None):
            seen_filters.append(tenant_filter)
            return []

        monkeypatch.setattr(qdrant_store, "hybrid_search", fake_hybrid_search)

        other_principal_same_tenant = {**TEST_CTX, "principal": "someone-else"}
        recall_memories(TEST_CTX, "coffee")
        recall_memories(other_principal_same_tenant, "coffee")

        owners = [
            next(c.match.value for c in f.must if c.key == "owner") for f in seen_filters
        ]
        assert owners[0] != owners[1]


class TestQueryEmployees:
    def test_refuses_without_ctx(self):
        result = query_employees.invoke({})
        assert "Refused" in result

    def test_passes_tenant_and_filters_through_to_sql_store(self, monkeypatch):
        captured = {}

        def fake_query_employees(tenant, department=None, name_contains=None, limit=None):
            captured["tenant"] = tenant
            captured["department"] = department
            captured["name_contains"] = name_contains
            return [{"name": "Priya Nair", "department": "Engineering", "title": "Staff Engineer", "hired_on": "2021-03-01"}]

        monkeypatch.setattr(sql_store, "query_employees", fake_query_employees)

        result = query_employees.invoke(
            {"department": "Engineering", "name_contains": "pri"}, config=_cfg()
        )

        assert captured["tenant"] == TEST_CTX["tenant"]
        assert captured["department"] == "Engineering"
        assert captured["name_contains"] == "pri"
        assert "Priya Nair" in result

    def test_passes_cap_plus_one_as_the_sql_limit(self, monkeypatch):
        """See TOOL_RESULT_CAPS's docstring: limit=cap+1 is exactly enough
        to detect "more rows exist" without a second COUNT(*) query."""
        captured = {}
        monkeypatch.setattr(
            sql_store,
            "query_employees",
            lambda tenant, department=None, name_contains=None, limit=None: captured.update(limit=limit) or [],
        )

        query_employees.invoke({}, config=_cfg())

        assert captured["limit"] == tools.TOOL_RESULT_CAPS["query_employees"] + 1

    def test_result_beyond_the_cap_is_marked_truncated_not_silently_dropped(self, monkeypatch):
        cap = tools.TOOL_RESULT_CAPS["query_employees"]
        rows = [
            {"name": f"Employee {i}", "department": "Engineering", "title": "Engineer", "hired_on": "2021-01-01"}
            for i in range(cap + 1)  # sql_store.query_employees(limit=cap+1) returning cap+1 rows means "more exist"
        ]
        monkeypatch.setattr(sql_store, "query_employees", lambda **kw: rows)

        result = query_employees.invoke({}, config=_cfg())

        assert "truncated" in result
        assert result.count("Employee") == cap  # exactly `cap` rows shown, not cap + 1

    def test_result_at_or_under_the_cap_is_not_marked_truncated(self, monkeypatch):
        cap = tools.TOOL_RESULT_CAPS["query_employees"]
        rows = [
            {"name": f"Employee {i}", "department": "Engineering", "title": "Engineer", "hired_on": "2021-01-01"}
            for i in range(cap)  # exactly at the cap, not over it
        ]
        monkeypatch.setattr(sql_store, "query_employees", lambda **kw: rows)

        result = query_employees.invoke({}, config=_cfg())

        assert "truncated" not in result

    def test_no_matches_returns_a_friendly_string(self, monkeypatch):
        monkeypatch.setattr(sql_store, "query_employees", lambda **kw: [])

        result = query_employees.invoke({}, config=_cfg())

        assert "No matching employees found." == result

    def test_two_different_tenants_get_different_tenant_param(self, monkeypatch):
        seen = []
        monkeypatch.setattr(
            sql_store,
            "query_employees",
            lambda tenant, department=None, name_contains=None, limit=None: seen.append(tenant) or [],
        )

        query_employees.invoke({}, config=_cfg(TEST_CTX))
        query_employees.invoke({}, config=_cfg(_OTHER_TENANT_CTX))

        assert seen[0] != seen[1]


class TestAskClarificationArgsValidation:
    def test_fewer_than_two_options_rejected(self):
        with pytest.raises(ValueError):
            AskClarificationArgs(question="Which one?", options=["only one"])

    def test_more_than_four_options_rejected(self):
        with pytest.raises(ValueError):
            AskClarificationArgs(question="Which one?", options=["a", "b", "c", "d", "e"])

    def test_two_to_four_options_accepted(self):
        args = AskClarificationArgs(question="Which one?", options=["a", "b", "c"])
        assert args.options == ["a", "b", "c"]


class TestAskClarification:
    def test_formats_the_question_and_numbered_options(self):
        result = ask_clarification.invoke(
            {
                "question": "Do you mean the LangGraph checkpointer or the database one?",
                "options": ["The LangGraph checkpointer", "A database checkpoint"],
            }
        )
        assert "Do you mean the LangGraph checkpointer" in result
        assert "1. The LangGraph checkpointer" in result
        assert "2. A database checkpoint" in result

    def test_needs_no_ctx_it_is_read_only_and_has_no_side_effects(self):
        # Unlike search_docs/add_note/remember/query_employees, this tool
        # never touches SecurityCtx at all — it's pure text formatting.
        result = ask_clarification.invoke(
            {"question": "Which?", "options": ["A", "B"]}
        )
        assert "Refused" not in result


class TestToolCapabilities:
    def test_read_only_tools_declared_correctly(self):
        assert TOOL_CAPABILITIES["search_docs"] == "read_only"
        assert TOOL_CAPABILITIES["calculator"] == "read_only"
        assert TOOL_CAPABILITIES["query_employees"] == "read_only"
        assert TOOL_CAPABILITIES["ask_clarification"] == "read_only"
        assert TOOL_CAPABILITIES["skill_search"] == "read_only"
        assert TOOL_CAPABILITIES["use_skill"] == "read_only"

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

    def test_skill_search_and_use_skill_are_registered_in_TOOLS(self):
        names = {t.name for t in TOOLS}
        assert "skill_search" in names
        assert "use_skill" in names


class _FakeHit:
    def __init__(self, payload):
        self.payload = payload


class TestSkillSearch:
    def test_needs_no_ctx_it_is_a_bundled_capability_not_tenant_data(self, monkeypatch):
        # Same posture as calculator: no SecurityCtx at all, no config arg.
        monkeypatch.setattr(qdrant_store, "hybrid_search", lambda *a, **k: [])
        result = skill_search.invoke({"query": "anything"})
        assert "Refused" not in result

    def test_searches_the_dedicated_skills_collection(self, monkeypatch):
        captured = {}

        def fake_hybrid_search(query_text, **kwargs):
            captured["query_text"] = query_text
            captured["collection"] = kwargs.get("collection")
            return []

        monkeypatch.setattr(qdrant_store, "hybrid_search", fake_hybrid_search)

        skill_search.invoke({"query": "onboard a new hire"})

        assert captured["query_text"] == "onboard a new hire"
        assert captured["collection"] == SKILLS_COLLECTION

    def test_formats_hits_as_name_and_description(self, monkeypatch):
        hits = [
            _FakeHit({"name": "onboarding-brief", "description": "Compose a new-hire brief."}),
            _FakeHit({"name": "expense-summary", "description": "Summarize expense line items."}),
        ]
        monkeypatch.setattr(qdrant_store, "hybrid_search", lambda *a, **k: hits)

        result = skill_search.invoke({"query": "onboard a new hire"})

        assert "- onboarding-brief: Compose a new-hire brief." in result
        assert "- expense-summary: Summarize expense line items." in result

    def test_no_hits_tells_the_model_to_proceed_without_a_skill(self, monkeypatch):
        monkeypatch.setattr(qdrant_store, "hybrid_search", lambda *a, **k: [])
        result = skill_search.invoke({"query": "something with no matching skill"})
        assert "No matching skills" in result

    def test_missing_collection_degrades_to_a_clear_message_not_a_crash(self, monkeypatch):
        def raises(*args, **kwargs):
            raise RuntimeError("collection 'skills' doesn't exist")

        monkeypatch.setattr(qdrant_store, "hybrid_search", raises)

        result = skill_search.invoke({"query": "anything"})

        assert "index-skills" in result


class TestUseSkill:
    def test_needs_no_ctx_it_is_a_bundled_capability_not_tenant_data(self, monkeypatch):
        monkeypatch.setattr(skills_module, "get_skills", lambda: {})
        result = use_skill.invoke({"name": "whatever"})
        assert "Refused" not in result

    def test_returns_the_matched_skills_full_body_from_disk_not_qdrant(self, monkeypatch):
        record = SkillRecord(
            name="onboarding-brief",
            description="Compose a new-hire brief.",
            body="# Onboarding Brief\n1. Call query_employees...",
            path=Path("skills/onboarding-brief/SKILL.md"),
        )
        monkeypatch.setattr(skills_module, "get_skills", lambda: {"onboarding-brief": record})

        result = use_skill.invoke({"name": "onboarding-brief"})

        assert result == record.body

    def test_unknown_name_returns_a_clear_message_not_an_exception(self, monkeypatch):
        monkeypatch.setattr(skills_module, "get_skills", lambda: {})
        result = use_skill.invoke({"name": "does-not-exist"})
        assert "No skill named" in result
        assert "skill_search" in result


class _RecordingFakeLLM:
    """A fake chat model that records every `messages` list it's invoked
    with, so a test can assert on what the NESTED agent's own system prompt
    actually contained. Composition over subclassing GenericFakeChatModel:
    that class is a pydantic BaseModel, and a subclass adding a plain `calls`
    attribute would be misread as a new pydantic field."""

    def __init__(self, *responses):
        self._inner = GenericFakeChatModel(messages=iter(responses))
        self.calls: list = []

    def invoke(self, messages, *args, **kwargs):
        self.calls.append(list(messages))
        return self._inner.invoke(messages, *args, **kwargs)


def _fake_subagent_record(name="researcher", tools_=("search_docs", "calculator"), model=None):
    return SubagentRecord(
        name=name,
        description="A test subagent.",
        system_prompt="You are a test subagent.",
        tools=tools_,
        model=model,
        path=Path(f"subagents/{name}/AGENT.md"),
    )


def _subagent_cfg(ctx=TEST_CTX, thread_id="parent-thread"):
    return {"configurable": {"thread_id": thread_id, "ctx": ctx}}


class TestResolveSubagentTools:
    def test_keeps_declared_read_only_tools(self):
        resolved = _resolve_subagent_tools(
            "x", ("search_docs", "calculator"), _ALL_TOOL_NAMES, TOOL_CAPABILITIES
        )
        assert resolved == ("search_docs", "calculator")

    def test_drops_a_declared_mutating_tool(self):
        resolved = _resolve_subagent_tools(
            "x", ("search_docs", "add_note"), _ALL_TOOL_NAMES, TOOL_CAPABILITIES
        )
        assert resolved == ("search_docs",)

    def test_drops_a_declared_unknown_tool(self):
        resolved = _resolve_subagent_tools(
            "x", ("search_docs", "not_a_real_tool"), _ALL_TOOL_NAMES, TOOL_CAPABILITIES
        )
        assert resolved == ("search_docs",)

    def test_strips_run_subagent_even_if_explicitly_declared(self):
        """The actual recursion block: read_only-ness alone would not
        exclude run_subagent, since it's declared read_only itself."""
        resolved = _resolve_subagent_tools(
            "x", ("search_docs", "run_subagent"), _ALL_TOOL_NAMES, TOOL_CAPABILITIES
        )
        assert resolved == ("search_docs",)

    def test_omitted_tools_falls_back_to_every_read_only_tool(self):
        resolved = _resolve_subagent_tools("x", None, _ALL_TOOL_NAMES, TOOL_CAPABILITIES)
        assert "add_note" not in resolved
        assert "remember" not in resolved
        assert "run_subagent" not in resolved
        assert "search_docs" in resolved
        assert "calculator" in resolved

    def test_empty_declared_list_resolves_to_empty_not_everything(self):
        """An explicit `tools: []` must mean zero tools, not fall back to
        the "omitted means everything" default — only a genuinely absent
        `tools:` key (None) gets that fallback."""
        resolved = _resolve_subagent_tools("x", (), _ALL_TOOL_NAMES, TOOL_CAPABILITIES)
        assert resolved == ()


class TestRunSubagentImpl:
    def test_refuses_without_ctx(self):
        result = _run_subagent_impl("researcher", "do something", {"configurable": {}})
        assert "Refused" in result

    def test_unknown_subagent_name_is_a_clear_message_not_an_exception(self):
        result = _run_subagent_impl("does-not-exist", "task", _subagent_cfg(), registry={})
        assert "No subagent named" in result

    def test_delegates_and_returns_the_final_answer(self):
        fake_llm = _RecordingFakeLLM(
            AIMessage(
                content="",
                tool_calls=[{"name": "calculator", "args": {"expression": "6*7"}, "id": "c1"}],
            ),
            AIMessage(content="The answer is 42."),
        )
        registry = {"researcher": (_fake_subagent_record(), ("search_docs", "calculator"))}

        result = _run_subagent_impl(
            "researcher", "what is 6*7?", _subagent_cfg(), registry=registry, llm=fake_llm
        )

        assert result == "The answer is 42."

    def test_nested_system_prompt_has_its_own_prompt_and_the_citation_warning(self):
        fake_llm = _RecordingFakeLLM(AIMessage(content="A plain answer, long enough."))
        record = _fake_subagent_record()
        registry = {"researcher": (record, ("calculator",))}

        _run_subagent_impl("researcher", "task", _subagent_cfg(), registry=registry, llm=fake_llm)

        first_call_messages = fake_llm.calls[0]
        system_text = "\n".join(
            m.content for m in first_call_messages if isinstance(m, SystemMessage)
        )
        assert record.system_prompt in system_text
        assert "[n]" in system_text  # the citation-marker warning text

    def test_task_is_the_subagents_sole_human_message_not_parent_history(self):
        from langchain_core.messages import HumanMessage

        fake_llm = _RecordingFakeLLM(AIMessage(content="A plain answer, long enough."))
        registry = {"researcher": (_fake_subagent_record(), ("calculator",))}

        _run_subagent_impl(
            "researcher", "the delegated task", _subagent_cfg(), registry=registry, llm=fake_llm
        )

        human_messages = [m for m in fake_llm.calls[0] if isinstance(m, HumanMessage)]
        assert len(human_messages) == 1
        assert human_messages[0].content == "the delegated task"

    def test_respects_its_own_smaller_iteration_ceiling_not_the_parents(self):
        """Distinct expressions each turn — never repeating identical args —
        so MAX_REPEATED_ACTIONS' no-progress detector never trips first;
        this isolates MAX_SUBAGENT_ITERATIONS specifically, proving
        build_graph(max_iterations=MAX_SUBAGENT_ITERATIONS) genuinely binds
        the nested run to its OWN, smaller ceiling rather than the parent's
        MAX_ITERATIONS (10 > MAX_SUBAGENT_ITERATIONS's 6)."""
        from app.agent.graph import MAX_SUBAGENT_ITERATIONS

        responses = [
            AIMessage(
                content="",
                tool_calls=[{"name": "calculator", "args": {"expression": f"{i}+1"}, "id": f"c{i}"}],
            )
            for i in range(MAX_SUBAGENT_ITERATIONS + 5)
        ]
        fake_llm = _RecordingFakeLLM(*responses)
        registry = {"researcher": (_fake_subagent_record(), ("calculator",))}

        result = _run_subagent_impl(
            "researcher", "never converge", _subagent_cfg(), registry=registry, llm=fake_llm
        )

        # Ended via a safety budget (no final AIMessage text), and never
        # consumed every one of the fake LLM's queued responses — proof it
        # stopped at ITS OWN ceiling rather than running until the parent's
        # larger MAX_ITERATIONS or exhausting the fake's whole queue.
        assert "did not produce a final answer" in result
        assert len(fake_llm.calls) <= MAX_SUBAGENT_ITERATIONS

    def test_hitting_its_own_no_progress_budget_reports_a_clear_message_not_empty(self):
        """Same identical-tool-call-batch loop MAX_REPEATED_ACTIONS already
        catches for the top-level agent (GRAPH_PATTERNS.md pattern 34) — this
        is one of should_continue's several "__end__" routes, not the
        iteration/token/cost caps specifically, which is exactly why the
        impl treats "no final answer" as one catch-all signal rather than
        re-deriving which specific budget tripped."""
        responses = [
            AIMessage(
                content="",
                tool_calls=[{"name": "calculator", "args": {"expression": "1+1"}, "id": f"c{i}"}],
            )
            for i in range(20)
        ]
        fake_llm = _RecordingFakeLLM(*responses)
        registry = {"researcher": (_fake_subagent_record(), ("calculator",))}

        before = metric_value(
            metrics.agent_subagent_run_total, subagent="researcher", outcome="budget_exceeded"
        )
        result = _run_subagent_impl(
            "researcher", "loop forever", _subagent_cfg(), registry=registry, llm=fake_llm
        )
        after = metric_value(
            metrics.agent_subagent_run_total, subagent="researcher", outcome="budget_exceeded"
        )

        assert "did not produce a final answer" in result
        assert after == before + 1

    def test_timeout_raises_and_is_recorded(self, monkeypatch):
        class _SlowLLM:
            def invoke(self, messages, *a, **kw):
                time.sleep(0.5)
                return AIMessage(content="too slow")

        monkeypatch.setattr(tools, "SUBAGENT_TIMEOUT_SECONDS", 0.05)
        registry = {"researcher": (_fake_subagent_record(), ())}

        before = metric_value(
            metrics.agent_subagent_run_total, subagent="researcher", outcome="timeout"
        )
        with pytest.raises(TimeoutError):
            _run_subagent_impl(
                "researcher", "task", _subagent_cfg(), registry=registry, llm=_SlowLLM()
            )
        after = metric_value(
            metrics.agent_subagent_run_total, subagent="researcher", outcome="timeout"
        )

        assert after == before + 1

    def test_records_usage_to_the_ledger_with_a_derived_thread_id(self, monkeypatch):
        from app.agent import meter

        captured = {}

        def fake_record_usage(ctx, thread_id, model_alias, total_tokens):
            captured["ctx"] = ctx
            captured["thread_id"] = thread_id

        monkeypatch.setattr(meter, "record_usage", fake_record_usage)
        fake_llm = _RecordingFakeLLM(AIMessage(content="An answer, long enough to pass."))
        registry = {"researcher": (_fake_subagent_record(), ("calculator",))}

        _run_subagent_impl(
            "researcher", "task", _subagent_cfg(), registry=registry, llm=fake_llm
        )

        assert captured["ctx"] == TEST_CTX
        assert captured["thread_id"].startswith("parent-thread:subagent:researcher:")


class TestRunSubagentTool:
    def test_registered_in_TOOLS_as_read_only(self):
        names = {t.name for t in TOOLS}
        assert "run_subagent" in names
        assert TOOL_CAPABILITIES["run_subagent"] == "read_only"

    def test_schema_lists_available_subagents_by_name_and_description(self):
        schema = run_subagent.args_schema.model_json_schema()
        assert "researcher" in schema["$defs"]["SubagentName"]["enum"]
        assert "Look up Acme Corp facts" in schema["properties"]["subagent_name"]["description"]

    def test_blank_task_rejected(self):
        from app.agent.tools import RunSubagentArgs

        with pytest.raises(ValueError):
            RunSubagentArgs(subagent_name=SubagentName.researcher, task="   ")
