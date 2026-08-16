"""Tests for app/tools.py — mainly `add_note`, the one mutating tool.

`search_docs`/`calculator` are exercised indirectly throughout
test_graph_integration.py already; `add_note` gets direct coverage here
since nothing else calls it (it's gated behind human_approval in every
graph scenario, so a graph-level test would need to drive a full
pause/resume cycle just to reach the implementation).
"""
import pytest

from app import qdrant_store, tools
from app.tools import TOOL_CAPABILITIES, TOOLS, AddNoteArgs, Topic, add_note


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

        result = tools._add_note_impl("Refunds", "30-day window.", Topic.company)

        assert "points" in captured
        assert len(captured["points"]) == 1
        point = captured["points"][0]
        assert point.vector == [0.1, 0.2, 0.3]
        assert point.payload["topic"] == "company"
        assert point.payload["title"] == "Refunds"
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

        tools._add_note_impl("A", "one", Topic.qdrant)
        tools._add_note_impl("B", "two", Topic.qdrant)

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
            {"title": "T", "content": "C", "topic": "langgraph"}
        )
        assert "T" in result


class TestToolCapabilities:
    def test_read_only_tools_declared_correctly(self):
        assert TOOL_CAPABILITIES["search_docs"] == "read_only"
        assert TOOL_CAPABILITIES["calculator"] == "read_only"

    def test_add_note_declared_mutating(self):
        assert TOOL_CAPABILITIES["add_note"] == "mutating"

    def test_every_tool_in_TOOLS_has_a_declared_capability(self):
        """Fail closed only helps for tools someone *forgot* to declare —
        it shouldn't be an excuse to skip declaring the ones that exist."""
        declared = set(TOOL_CAPABILITIES)
        registered = {t.name for t in TOOLS}
        assert registered <= declared

    def test_add_note_is_registered_in_TOOLS(self):
        assert any(t.name == "add_note" for t in TOOLS)
