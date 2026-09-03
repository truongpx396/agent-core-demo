"""Subagent definitions: bundled, on-disk descriptions of scoped, isolated
nested agent runs the main agent can delegate a task to (GRAPH_PATTERNS.md
pattern 46).

A subagent is one directory under `SUBAGENTS_DIR` (app/core/config.py)
containing an `AGENT.md` — YAML frontmatter (`name`, `description`, and
optionally `tools`, `model`, `domains`) followed by a markdown system-prompt
body, the same shape `app/agent/skills.py`'s `SKILL.md` uses:

    ---
    name: researcher
    description: Look up Acme Corp facts, docs, or people...
    tools: [search_docs, calculator, query_employees]
    model: chat
    ---

    You are a focused research assistant...

This module is deliberately domain-agnostic, mirroring `app/agent/skills.py`'s
own scope exactly: it knows nothing about `TOOL_CAPABILITIES`, read_only-ness,
or which domain's tools exist. All of that validation (which declared tools
are actually safe to hand a subagent, the run_subagent recursion block) lives
in `app/agent/tools.py`, the module that already owns `TOOL_CAPABILITIES` —
keeping this module a pure, reusable disk parser avoids any import-order
coupling between the two. That split applies to `domains` too: this module
only parses and carries the raw list (or `None`); app/agent/tools.py decides
what an absent `domains` DEFAULTS to (unlike app/agent/skills.py's SkillRecord,
where `None` means "every domain," an untagged subagent here stays exactly
where it's always been — visible only when `app/agent/tools.py` builds
Acme's own registry — precisely BECAUSE a subagent's declared `tools:` are
only ever meaningful against one specific tool universe: a nested run
resolves each declared name against the CALLING domain's own tools, so an
untagged subagent silently exposed to every domain would mostly just resolve
to nothing useful in a domain its tools were never written for).
"""
import logging
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from app.core.config import SUBAGENTS_DIR

logger = logging.getLogger(__name__)

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?\n)---\s*\n?", re.DOTALL)


class SubagentLoadError(ValueError):
    """An AGENT.md file doesn't match the required shape (frontmatter with
    non-empty `name`/`description`, an optional `tools` list of strings, an
    optional `model` string, an optional `domains` list of strings, plus a
    non-empty body). Raised by `_parse_subagent_file`; `load_subagents`
    catches this per-file and skips the offending file rather than letting
    one bad subagent take down the whole catalog (GRAPH_PATTERNS.md pattern
    10's degrade-gracefully posture, same as `app/agent/skills.py::SkillLoadError`)."""


@dataclass(frozen=True)
class SubagentRecord:
    name: str
    description: str
    system_prompt: str
    tools: tuple[str, ...] | None  # None = "inherit the domain's read_only
    # tools" (resolved by app/agent/tools.py, not here — see module docstring).
    model: str | None  # None = use the parent's own CHAT_MODEL alias.
    domains: tuple[str, ...] | None  # None = defaults applied by
    # app/agent/tools.py, not here — see module docstring.
    path: Path


def _parse_subagent_file(path: Path) -> SubagentRecord:
    text = path.read_text(encoding="utf-8")
    match = _FRONTMATTER_RE.match(text)
    if not match:
        raise SubagentLoadError(
            f"{path}: missing YAML frontmatter (expected a leading '---' block)."
        )
    try:
        frontmatter = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as exc:
        raise SubagentLoadError(f"{path}: invalid YAML frontmatter: {exc}") from exc
    if not isinstance(frontmatter, dict):
        raise SubagentLoadError(f"{path}: frontmatter must be a YAML mapping.")

    name = frontmatter.get("name")
    description = frontmatter.get("description")
    if not isinstance(name, str) or not name.strip():
        raise SubagentLoadError(f"{path}: frontmatter must set a non-empty 'name'.")
    if not isinstance(description, str) or not description.strip():
        raise SubagentLoadError(f"{path}: frontmatter must set a non-empty 'description'.")

    raw_tools = frontmatter.get("tools")
    tools: tuple[str, ...] | None
    if raw_tools is None:
        tools = None
    elif isinstance(raw_tools, list) and all(isinstance(t, str) and t.strip() for t in raw_tools):
        tools = tuple(raw_tools)
    else:
        raise SubagentLoadError(f"{path}: 'tools', if set, must be a list of non-empty strings.")

    model = frontmatter.get("model")
    if model is not None and (not isinstance(model, str) or not model.strip()):
        raise SubagentLoadError(f"{path}: 'model', if set, must be a non-empty string.")

    raw_domains = frontmatter.get("domains")
    domains: tuple[str, ...] | None
    if raw_domains is None:
        domains = None
    elif isinstance(raw_domains, list) and all(
        isinstance(d, str) and d.strip() for d in raw_domains
    ):
        domains = tuple(raw_domains)
    else:
        raise SubagentLoadError(f"{path}: 'domains', if set, must be a list of non-empty strings.")

    body = text[match.end() :].strip()
    if not body:
        raise SubagentLoadError(f"{path}: subagent body (markdown after frontmatter) must not be empty.")

    return SubagentRecord(
        name=name.strip(),
        description=description.strip(),
        system_prompt=body,
        tools=tools,
        model=model.strip() if model else None,
        domains=domains,
        path=path,
    )


def load_subagents(subagents_dir: str | Path | None = None) -> dict[str, SubagentRecord]:
    """Scans `subagents_dir/*/AGENT.md` (default `SUBAGENTS_DIR`) and returns
    `{name: SubagentRecord}`. A malformed file is logged and skipped — never
    raised past this function — so one bad subagent can't take the whole
    catalog down. A duplicate `name` across two directories keeps whichever
    was parsed first (directory order) and logs a warning, same posture as
    `app/agent/skills.py::load_skills`."""
    root = Path(subagents_dir) if subagents_dir is not None else Path(SUBAGENTS_DIR)
    if not root.is_dir():
        return {}

    subagents: dict[str, SubagentRecord] = {}
    for agent_md in sorted(root.glob("*/AGENT.md")):
        try:
            record = _parse_subagent_file(agent_md)
        except SubagentLoadError as exc:
            logger.warning("skipping invalid subagent file", extra={"error": str(exc)})
            continue
        if record.name in subagents:
            logger.warning(
                "duplicate subagent name; keeping the first one found",
                extra={"subagent_name": record.name, "path": str(agent_md)},
            )
            continue
        subagents[record.name] = record
    return subagents


_subagents_cache: dict[str, SubagentRecord] | None = None


def get_subagents() -> dict[str, SubagentRecord]:
    """Lazy-loaded, process-wide catalog — same lazy-singleton shape as
    `app/agent/skills.py::get_skills`. app/agent/tools.py calls this ONCE, at
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
