"""`app/agent/tools/frontmatter.py`: the loader `skill_catalog` and `subagent_catalog` share."""
import logging

import pytest

from app.agent.tools import frontmatter as fm
from app.agent.tools.skill_catalog import SkillLoadError, load_skills
from app.agent.tools.subagent_catalog import SubagentLoadError, load_subagents


def _write(root, slug, filename, text):
    d = root / slug
    d.mkdir()
    (d / filename).write_text(text, encoding="utf-8")


@pytest.mark.parametrize("error", [SkillLoadError, SubagentLoadError])
def test_a_file_without_frontmatter_raises_the_callers_own_error_naming_the_path(tmp_path, error):
    path = tmp_path / "X.md"
    path.write_text("just a body\n", encoding="utf-8")
    with pytest.raises(error, match="missing YAML frontmatter"):
        fm.split_file(path, error)


@pytest.mark.parametrize(
    ("loader", "filename", "kind"),
    [(load_skills, "SKILL.md", "skill"), (load_subagents, "AGENT.md", "subagent")],
)
def test_a_bad_file_is_skipped_and_the_first_duplicate_wins_with_kind_worded_logs(
    tmp_path, caplog, loader, filename, kind
):
    ok = "---\nname: dup\ndescription: the first one\n---\nbody\n"
    _write(tmp_path, "a-first", filename, ok)
    _write(tmp_path, "b-bad", filename, "no frontmatter at all\n")
    _write(tmp_path, "c-second", filename, ok.replace("the first one", "the second one"))

    with caplog.at_level(logging.WARNING):
        catalog = loader(tmp_path)

    assert list(catalog) == ["dup"]
    assert catalog["dup"].description == "the first one"
    messages = [r.getMessage() for r in caplog.records]
    assert f"skipping invalid {kind} file" in messages
    assert f"duplicate {kind} name; keeping the first one found" in messages


def test_a_missing_directory_is_an_empty_catalog(tmp_path):
    assert load_skills(tmp_path / "nope") == {}
    assert load_subagents(tmp_path / "nope") == {}
