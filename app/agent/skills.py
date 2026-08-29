"""Skill packages: bundled, on-disk instruction bundles the agent can
discover and load progressively (GRAPH_PATTERNS.md pattern 45).

A skill is one directory under `SKILLS_DIR` (app/core/config.py) containing
a `SKILL.md` — YAML frontmatter (`name`, `description`) followed by a
markdown instruction body, the same shape Anthropic's Agent Skills /
OpenClaw's `SKILL.md` use:

    ---
    name: onboarding-brief
    description: Compose a new-hire onboarding brief...
    ---

    # Onboarding Brief
    1. Look up the new hire with query_employees...

This module is the DISK side only — the source of truth for a skill's full
body. `app/agent/tools.py::use_skill` reads a skill's `body` from here by
exact `name`, never from Qdrant: the `skills` Qdrant collection
(`app/retrieval/qdrant_store.py`, built by `scripts/index_skills.py`) holds
only `{name, description}`, just enough for `skill_search`'s hybrid search
to find the right name. Keeping "what's searchable" (Qdrant) and "what's
authoritative" (this module, reading the file directly) as two different
systems means a skill's full instructions can never drift out of sync with
what's actually on disk — there's nothing to keep in sync in the first
place.

Skills are bundled app capabilities, not tenant data — no `SecurityCtx`
involved here, same as `app/agent/tools.py::calculator`. See that module's
`TOOL_CAPABILITIES` comment for why every tool this app ships defaults to
the most conservative capability unless declared otherwise; skills
themselves aren't tools, just content two tools (`skill_search`/
`use_skill`) load and return.
"""
import logging
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from app.core.config import SKILLS_DIR

logger = logging.getLogger(__name__)

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?\n)---\s*\n?", re.DOTALL)


class SkillLoadError(ValueError):
    """A SKILL.md file doesn't match the required shape (frontmatter with
    non-empty `name`/`description`, plus a non-empty body). Raised by
    `_parse_skill_file`; `load_skills` catches this per-file and skips the
    offending file rather than letting one bad skill take down the whole
    catalog (GRAPH_PATTERNS.md pattern 10's degrade-gracefully posture)."""


@dataclass(frozen=True)
class SkillRecord:
    name: str
    description: str
    body: str
    path: Path


def _parse_skill_file(path: Path) -> SkillRecord:
    text = path.read_text(encoding="utf-8")
    match = _FRONTMATTER_RE.match(text)
    if not match:
        raise SkillLoadError(
            f"{path}: missing YAML frontmatter (expected a leading '---' block)."
        )
    try:
        frontmatter = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as exc:
        raise SkillLoadError(f"{path}: invalid YAML frontmatter: {exc}") from exc
    if not isinstance(frontmatter, dict):
        raise SkillLoadError(f"{path}: frontmatter must be a YAML mapping.")

    name = frontmatter.get("name")
    description = frontmatter.get("description")
    if not isinstance(name, str) or not name.strip():
        raise SkillLoadError(f"{path}: frontmatter must set a non-empty 'name'.")
    if not isinstance(description, str) or not description.strip():
        raise SkillLoadError(f"{path}: frontmatter must set a non-empty 'description'.")

    body = text[match.end() :].strip()
    if not body:
        raise SkillLoadError(f"{path}: skill body (markdown after frontmatter) must not be empty.")

    return SkillRecord(name=name.strip(), description=description.strip(), body=body, path=path)


def load_skills(skills_dir: str | Path | None = None) -> dict[str, SkillRecord]:
    """Scans `skills_dir/*/SKILL.md` (default `SKILLS_DIR`) and returns
    `{name: SkillRecord}`. A malformed file is logged and skipped — never
    raised past this function — so one bad skill can't take the whole
    catalog down. A duplicate `name` across two directories keeps whichever
    was parsed first (directory order) and logs a warning; it's not an
    error, since a name collision is a content-authoring mistake, not a
    reason to refuse serving every OTHER skill."""
    root = Path(skills_dir) if skills_dir is not None else Path(SKILLS_DIR)
    if not root.is_dir():
        return {}

    skills: dict[str, SkillRecord] = {}
    for skill_md in sorted(root.glob("*/SKILL.md")):
        try:
            record = _parse_skill_file(skill_md)
        except SkillLoadError as exc:
            logger.warning("skipping invalid skill file", extra={"error": str(exc)})
            continue
        if record.name in skills:
            logger.warning(
                "duplicate skill name; keeping the first one found",
                extra={"skill_name": record.name, "path": str(skill_md)},
            )
            continue
        skills[record.name] = record
    return skills


_skills_cache: dict[str, SkillRecord] | None = None


def get_skills() -> dict[str, SkillRecord]:
    """Lazy-loaded, process-wide catalog — same lazy-singleton shape as
    `app/retrieval/embeddings.py`'s `_get_sparse_model()`/`_get_reranker()`,
    so a small, rarely-changing on-disk catalog isn't re-parsed on every
    single `use_skill` call. See `reload_skills` to force a re-scan."""
    global _skills_cache
    if _skills_cache is None:
        _skills_cache = load_skills()
    return _skills_cache


def reload_skills() -> None:
    """Test/ops hook: force the next `get_skills()` call to re-scan disk
    (e.g. after editing/adding a SKILL.md without restarting the process)."""
    global _skills_cache
    _skills_cache = None
