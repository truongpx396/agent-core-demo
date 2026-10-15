"""Tests for app/agent/tools/subagent_catalog.py — the on-disk subagent-definition loader
(GRAPH_PATTERNS.md pattern 46). All isolated to `tmp_path`; nothing here
touches the real `subagents/` directory shipped with the app.
"""
import re
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from app.agent.graph import core as graph
from app.agent.graph.build import build_graph
from app.agent.graph.core import GraphDeps
from app.agent.manifest import AgentManifest
from app.agent.runtime import stream as stream_module
from app.agent.tools.subagent_catalog import (
    SubagentLoadError,
    _parse_subagent_file,
    load_subagents,
)
from tests.agent.fakes import Domain, call, cfg, scripted_llm


def _write_subagent(tmp_path, slug, content):
    agent_dir = tmp_path / slug
    agent_dir.mkdir()
    (agent_dir / "AGENT.md").write_text(content, encoding="utf-8")
    return agent_dir / "AGENT.md"


_VALID = """---
name: researcher
description: Look up Ecorp facts without pulling search steps into the main conversation.
tools: [search_docs, calculator]
model: chat
---

# Researcher
You are a focused research assistant.
"""


class TestParseSubagentFile:
    def test_parses_name_description_tools_model_and_body(self, tmp_path):
        path = _write_subagent(tmp_path, "researcher", _VALID)

        record = _parse_subagent_file(path)

        assert record.name == "researcher"
        assert record.description.startswith("Look up Ecorp facts")
        assert record.tools == ("search_docs", "calculator")
        assert record.model == "chat"
        assert "focused research assistant" in record.system_prompt
        assert record.path == path

    def test_omitted_tools_and_model_default_to_none(self, tmp_path):
        content = "---\nname: generalist\ndescription: A generalist subagent.\n---\nBody.\n"
        path = _write_subagent(tmp_path, "generalist", content)

        record = _parse_subagent_file(path)

        assert record.tools is None
        assert record.model is None
        assert record.domains is None

    def test_parses_domains_list(self, tmp_path):
        content = _VALID.replace("model: chat\n---", "model: chat\ndomains: [support]\n---")
        path = _write_subagent(tmp_path, "researcher", content)
        assert _parse_subagent_file(path).domains == ("support",)

    def test_non_list_domains_raises(self, tmp_path):
        content = _VALID.replace("model: chat\n---", "model: chat\ndomains: support\n---")
        path = _write_subagent(tmp_path, "bad", content)
        with pytest.raises(SubagentLoadError):
            _parse_subagent_file(path)

    def test_missing_frontmatter_raises(self, tmp_path):
        path = _write_subagent(tmp_path, "bad", "# Just a body, no frontmatter at all.")
        with pytest.raises(SubagentLoadError):
            _parse_subagent_file(path)

    def test_missing_name_raises(self, tmp_path):
        content = "---\ndescription: has a description but no name\n---\nBody text.\n"
        path = _write_subagent(tmp_path, "bad", content)
        with pytest.raises(SubagentLoadError):
            _parse_subagent_file(path)

    def test_missing_description_raises(self, tmp_path):
        content = "---\nname: no-description\n---\nBody text.\n"
        path = _write_subagent(tmp_path, "bad", content)
        with pytest.raises(SubagentLoadError):
            _parse_subagent_file(path)

    def test_empty_body_raises(self, tmp_path):
        content = "---\nname: empty-body\ndescription: has frontmatter, no body\n---\n"
        path = _write_subagent(tmp_path, "bad", content)
        with pytest.raises(SubagentLoadError):
            _parse_subagent_file(path)

    def test_invalid_yaml_raises(self, tmp_path):
        content = "---\nname: [unterminated\n---\nBody text.\n"
        path = _write_subagent(tmp_path, "bad", content)
        with pytest.raises(SubagentLoadError):
            _parse_subagent_file(path)

    def test_non_list_tools_raises(self, tmp_path):
        content = "---\nname: bad-tools\ndescription: d\ntools: search_docs\n---\nBody.\n"
        path = _write_subagent(tmp_path, "bad", content)
        with pytest.raises(SubagentLoadError):
            _parse_subagent_file(path)

    def test_non_string_model_raises(self, tmp_path):
        content = "---\nname: bad-model\ndescription: d\nmodel: 123\n---\nBody.\n"
        path = _write_subagent(tmp_path, "bad", content)
        with pytest.raises(SubagentLoadError):
            _parse_subagent_file(path)


class TestLoadSubagents:
    def test_returns_empty_dict_for_a_missing_directory(self, tmp_path):
        assert load_subagents(tmp_path / "does-not-exist") == {}

    def test_loads_every_valid_subagent_keyed_by_name(self, tmp_path):
        _write_subagent(tmp_path, "researcher", _VALID)
        _write_subagent(
            tmp_path,
            "generalist",
            "---\nname: generalist\ndescription: A generalist subagent.\n---\nBody.\n",
        )

        catalog = load_subagents(tmp_path)

        assert set(catalog) == {"researcher", "generalist"}
        assert catalog["researcher"].tools == ("search_docs", "calculator")

    def test_a_malformed_subagent_is_skipped_not_fatal_to_the_whole_catalog(self, tmp_path):
        _write_subagent(tmp_path, "good-agent", _VALID.replace("researcher", "good-agent"))
        _write_subagent(tmp_path, "bad-agent", "no frontmatter here at all")

        catalog = load_subagents(tmp_path)

        assert set(catalog) == {"good-agent"}

    def test_duplicate_name_keeps_the_first_and_does_not_raise(self, tmp_path):
        _write_subagent(tmp_path, "aaa-first", _VALID)
        _write_subagent(tmp_path, "zzz-second", _VALID)  # same `name:` in frontmatter

        catalog = load_subagents(tmp_path)

        assert len(catalog) == 1
        assert catalog["researcher"].path.parent.name == "aaa-first"


FACT = "Ecorp support hours are nine to five Monday through Friday Pacific time."


async def test_a_subagent_never_emits_citation_markers_or_events_into_the_parents_stream(monkeypatch):
    """Real bug: the nested graph's own `check_output` inserted a `[1]` into the subagent's answer
    (its sources, meaningless to the parent), the subagent received contradictory citation rules
    (its prompt said "no bracket markers", the tail reminder said "every sentence must end with
    one"), and the nested `check_output`'s `on_chain_end` reached the PARENT's stream: the client
    saw `retry`, then the subagent's text as a token, then `done` (GRAPH_PATTERNS.md pattern 46)."""
    from app.agent.tools import run_subagent as subagent_tools
    from app.agent.tools.subagent_catalog import SubagentRecord

    async def nested_search(query, ctx):
        citation = {"marker": "[1]", "doc_id": "d1", "title": "hours", "text": FACT, "score": 5.0}
        return f"[1] {FACT}", [citation]

    monkeypatch.setattr(graph, "_default_search", nested_search)  # the nested graph's retrieval
    record = SubagentRecord("researcher", "d", "You research.", (), None, None, Path("x"))
    nested_inputs, answers = [], []

    @tool
    async def delegate(task: str, config: RunnableConfig) -> str:
        """Delegate a lookup."""
        result = await subagent_tools._run_subagent_impl(
            "researcher", task, config,
            registry={"researcher": (record, ())}, tools_by_name={},
            llm=scripted_llm(lambda _m: AIMessage(content=FACT), nested_inputs),
        )
        answers.append(result.answer)
        return result.answer

    def parent(messages):
        if not any(isinstance(m, ToolMessage) for m in messages):
            return call("delegate", {"task": "find support hours"}, "d1")
        return AIMessage(content="The parent's own final answer text.")

    async def parent_search(query, ctx):
        return "", []

    g = build_graph(
        GraphDeps(llm=scripted_llm(parent), search_docs=parent_search),
        manifest=AgentManifest("t", "You help."),
        domain=Domain([delegate], {"delegate": "read_only"}),
    )
    events = [
        e async for e in stream_module._run_graph_stream(
            g, {"messages": [HumanMessage(content="hours?")]}, cfg(), None
        )
    ]
    assert "retry" not in [e["type"] for e in events]
    shown = "".join(e["content"] for e in events if e["type"] == "token")
    assert shown == "The parent's own final answer text."
    assert not re.search(r"\[\d+\]", answers[0])
    assert not any("bracket marker" in str(m.content) for m in nested_inputs[0])
