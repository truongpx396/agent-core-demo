"""Tests for app/agent/tools/skill_catalog.py — the on-disk skill-package loader
(GRAPH_PATTERNS.md pattern 45). All isolated to `tmp_path`; nothing here
touches the skills shipped with the app (`test_every_shipped_skill_loads` aside) or a live Qdrant.
"""
from pathlib import Path

import pytest

from app.agent.tools.skill_catalog import SkillLoadError, _parse_skill_file, load_skills


def _write_skill(tmp_path, slug, content, owner="shared"):
    skill_dir = tmp_path / owner / "skills" / slug
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")
    return skill_dir / "SKILL.md"


_VALID = """---
name: expense-summary
description: Summarize expense line items into a reimbursement report.
---

# Expense Summary
1. Use the calculator tool for every sum.
"""


class TestParseSkillFile:
    def test_parses_name_description_and_body(self, tmp_path):
        path = _write_skill(tmp_path, "expense-summary", _VALID)

        record = _parse_skill_file(path)

        assert record.name == "expense-summary"
        assert record.description == "Summarize expense line items into a reimbursement report."
        assert "Use the calculator tool" in record.body
        assert record.path == path

    def test_a_skill_in_the_shared_folder_is_for_every_domain(self, tmp_path):
        path = _write_skill(tmp_path, "expense-summary", _VALID, owner="shared")
        assert _parse_skill_file(path).domains is None

    def test_a_skill_in_a_domains_folder_is_for_that_domain_only(self, tmp_path):
        path = _write_skill(tmp_path, "expense-summary", _VALID, owner="support")
        assert _parse_skill_file(path).domains == ("support",)

    @pytest.mark.parametrize("tag", ["domains: [support]", "domains: support"])
    def test_a_domains_tag_is_refused_because_the_folder_is_the_owner(self, tmp_path, tag):
        content = _VALID.replace("---\n\n# Expense", f"{tag}\n---\n\n# Expense")
        path = _write_skill(tmp_path, "expense-summary", content, owner="sales")
        with pytest.raises(SkillLoadError, match="owner folder"):
            _parse_skill_file(path)

    def test_missing_frontmatter_raises(self, tmp_path):
        path = _write_skill(tmp_path, "bad", "# Just a body, no frontmatter at all.")
        with pytest.raises(SkillLoadError):
            _parse_skill_file(path)

    def test_missing_name_raises(self, tmp_path):
        content = "---\ndescription: has a description but no name\n---\nBody text.\n"
        path = _write_skill(tmp_path, "bad", content)
        with pytest.raises(SkillLoadError):
            _parse_skill_file(path)

    def test_missing_description_raises(self, tmp_path):
        content = "---\nname: no-description\n---\nBody text.\n"
        path = _write_skill(tmp_path, "bad", content)
        with pytest.raises(SkillLoadError):
            _parse_skill_file(path)

    def test_empty_body_raises(self, tmp_path):
        content = "---\nname: empty-body\ndescription: has frontmatter, no body\n---\n"
        path = _write_skill(tmp_path, "bad", content)
        with pytest.raises(SkillLoadError):
            _parse_skill_file(path)

    def test_invalid_yaml_raises(self, tmp_path):
        content = "---\nname: [unterminated\n---\nBody text.\n"
        path = _write_skill(tmp_path, "bad", content)
        with pytest.raises(SkillLoadError):
            _parse_skill_file(path)


class TestLoadSkills:
    def test_returns_empty_dict_for_a_missing_directory(self, tmp_path):
        assert load_skills(tmp_path / "does-not-exist") == {}

    def test_loads_every_valid_skill_keyed_by_name(self, tmp_path):
        _write_skill(tmp_path, "expense-summary", _VALID)
        _write_skill(
            tmp_path,
            "onboarding-brief",
            "---\nname: onboarding-brief\ndescription: Compose a new-hire brief.\n---\nBody.\n",
        )

        catalog = load_skills(tmp_path)

        assert set(catalog) == {"expense-summary", "onboarding-brief"}
        assert catalog["expense-summary"].description.startswith("Summarize expense")

    def test_each_skill_is_owned_by_the_folder_it_sits_in(self, tmp_path):
        _write_skill(tmp_path, "everyone", _VALID.replace("expense-summary", "everyone"), owner="shared")
        _write_skill(tmp_path, "ops-only", _VALID.replace("expense-summary", "ops-only"), owner="ops")

        catalog = load_skills(tmp_path)

        assert catalog["everyone"].domains is None
        assert catalog["ops-only"].domains == ("ops",)

    def test_a_malformed_skill_is_skipped_not_fatal_to_the_whole_catalog(self, tmp_path):
        """One bad SKILL.md must not take down every other skill — the
        same 'degrade a single failing leg, not the whole call' posture
        app/adapters/outbound/qdrant/qdrant_store.py already applies to hybrid_search."""
        _write_skill(tmp_path, "good-skill", _VALID.replace("expense-summary", "good-skill"))
        _write_skill(tmp_path, "bad-skill", "no frontmatter here at all")

        catalog = load_skills(tmp_path)

        assert set(catalog) == {"good-skill"}

    def test_duplicate_name_keeps_the_first_and_does_not_raise(self, tmp_path):
        _write_skill(tmp_path, "aaa-first", _VALID)
        _write_skill(tmp_path, "zzz-second", _VALID)  # same `name:` in frontmatter

        catalog = load_skills(tmp_path)

        assert len(catalog) == 1
        assert catalog["expense-summary"].path.parent.name == "aaa-first"


def test_every_shipped_skill_loads():
    """A shipped SKILL.md that fails to parse is skipped with a warning, which would leave a domain without it unnoticed."""
    domains_dir = Path(__file__).resolve().parents[3] / "app" / "domains"
    on_disk = list(domains_dir.glob("*/skills/*/SKILL.md"))
    assert on_disk, "no shipped skills found: the layout moved"
    assert {p.parent.name for p in on_disk} == {r.path.parent.name for r in load_skills(domains_dir).values()}
