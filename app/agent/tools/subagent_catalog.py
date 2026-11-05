"""Subagent definitions: bundled, on-disk descriptions of scoped, isolated nested agent runs the main agent can
delegate to (GRAPH_PATTERNS.md pattern 46).

A subagent is one directory, `<DOMAINS_DIR>/<domain>/subagents/<name>/` (app/core/config.py), containing an `AGENT.md`:
YAML frontmatter (`name`, `description`, optionally `tools`, `model`) then a markdown system-prompt body, the shape
`skill_catalog.py`'s `SKILL.md` uses:

    ---
    name: researcher
    description: Look up Ecorp facts, docs, or people...
    tools: [search_docs, calculator, query_employees]
    model: chat
    ---

    You are a focused research assistant...

Like `skill_catalog.py`, this module is domain-agnostic: it knows nothing about `TOOL_CAPABILITIES`,
read_only-ness, or which domain's tools exist. That validation lives in `app/agent/tools/builtin.py`, which
owns `TOOL_CAPABILITIES`, avoiding import-order coupling. A subagent belongs to exactly one domain, the folder
it sits in: its declared `tools:` only make sense against ONE tool universe, and exposed to every domain it would
mostly resolve to nothing useful. So, unlike a skill, there is no `shared` folder for subagents.
"""
from dataclasses import dataclass
from pathlib import Path

from app.agent.tools import frontmatter as fm
from app.core.config import DOMAINS_DIR


class SubagentLoadError(ValueError):
    """An AGENT.md file doesn't match the required shape (frontmatter with
    non-empty `name`/`description`, an optional `tools` list of strings, an
    optional `model` string, plus a non-empty body, in a domain's folder). Raised by `_parse_subagent_file`; `load_subagents` skips
    the offending file rather than letting one bad subagent take down the whole
    catalog (GRAPH_PATTERNS.md pattern 10's degrade-gracefully posture, same as
    `app/agent/tools/skill_catalog.py::SkillLoadError`)."""


@dataclass(frozen=True)
class SubagentRecord:
    name: str
    description: str
    system_prompt: str
    tools: tuple[str, ...] | None  # None = "inherit the domain's read_only
    # tools" (resolved by app/agent/tools/run_subagent.py, not here — see module docstring).
    model: str | None  # None = use the parent's own CHAT_MODEL alias.
    domains: tuple[str, ...]  # the one domain whose folder it sits in
    path: Path


def _parse_subagent_file(path: Path) -> SubagentRecord:
    frontmatter, body = fm.split_file(path, SubagentLoadError)
    if "domains" in frontmatter:
        raise SubagentLoadError(f"{path}: 'domains' is not frontmatter: the folder a subagent sits in is its domain.")
    owner = fm.owner_of(path)
    if owner == fm.SHARED:
        raise SubagentLoadError(f"{path}: a subagent belongs to one domain; there is no shared folder for subagents.")
    return SubagentRecord(
        name=fm.required_text(frontmatter, "name", path, SubagentLoadError),
        description=fm.required_text(frontmatter, "description", path, SubagentLoadError),
        system_prompt=fm.required_body(body, path, "subagent", SubagentLoadError),
        tools=fm.optional_string_list(frontmatter, "tools", path, SubagentLoadError),
        model=fm.optional_text(frontmatter, "model", path, SubagentLoadError),
        domains=(owner,),
        path=path,
    )


def load_subagents(domains_dir: str | Path | None = None) -> dict[str, SubagentRecord]:
    """Scans `domains_dir/*/subagents/*/AGENT.md` (default `DOMAINS_DIR`) and returns
    `{name: SubagentRecord}`; see `frontmatter.load_catalog` for how a malformed file
    or a duplicate name is handled (same posture as `skill_catalog.load_skills`)."""
    root = Path(domains_dir) if domains_dir is not None else Path(DOMAINS_DIR)
    return fm.load_catalog(root, "AGENT.md", _parse_subagent_file, SubagentLoadError, "subagent")


_subagents_cache: dict[str, SubagentRecord] | None = None


def get_subagents() -> dict[str, SubagentRecord]:
    """Lazy-loaded, process-wide catalog — same lazy-singleton shape as
    `app/agent/tools/skill_catalog.py::get_skills`. app/agent/tools/builtin.py calls this ONCE, at
    its own module-import time, to build run_subagent's closed name enum —
    see that module's docstring for why subagents need eager resolution
    where skills don't."""
    global _subagents_cache
    if _subagents_cache is None:
        _subagents_cache = load_subagents()
    return _subagents_cache


def reload_subagents() -> None:
    """Test/ops hook: force the next `get_subagents()` call to re-scan disk
    (e.g. after editing/adding an AGENT.md without restarting the process)."""
    global _subagents_cache
    _subagents_cache = None
