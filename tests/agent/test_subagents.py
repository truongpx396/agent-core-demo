"""Tests for app/agent/subagents.py — the on-disk subagent-definition loader
(GRAPH_PATTERNS.md pattern 46). All isolated to `tmp_path`; nothing here
touches the real `subagents/` directory shipped with the app.
"""
import pytest

from app.agent.subagents import SubagentLoadError, _parse_subagent_file, load_subagents


def _write_subagent(tmp_path, slug, content):
    agent_dir = tmp_path / slug
    agent_dir.mkdir()
    (agent_dir / "AGENT.md").write_text(content, encoding="utf-8")
    return agent_dir / "AGENT.md"


_VALID = """---
name: researcher
description: Look up Acme Corp facts without pulling search steps into the main conversation.
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
        assert record.description.startswith("Look up Acme Corp facts")
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
