"""Skill packages: bundled, on-disk instruction bundles the agent can discover and load progressively
(GRAPH_PATTERNS.md pattern 45).

A skill is one directory, `<DOMAINS_DIR>/<owner>/skills/<name>/` (app/core/config.py), containing a `SKILL.md`: YAML
frontmatter (`name`, `description`) then a markdown instruction body, the shape Anthropic's Agent Skills / OpenClaw's
`SKILL.md` use:

    ---
    name: onboarding-brief
    description: Compose a new-hire onboarding brief...
    ---

    # Onboarding Brief
    1. Look up the new hire with query_employees...

The OWNER folder says who may see the skill: a domain name (`app/domains/registry.py` keys) means that domain only,
`shared` means every domain. There is no `domains:` frontmatter, so the folder cannot disagree with a tag.
`app/agent/tools/builtin.py::make_skill_tools` enforces it (in `skill_search` results and `use_skill` lookup);
this module only carries the fact.

This is the DISK side only, the source of truth for a skill's full body. `builtin.py::use_skill` reads the
`body` from here by exact `name`, never from Qdrant: the `skills` collection (`app/adapters/outbound/qdrant/qdrant_store.py`,
built by `scripts/tools/index_skills.py`) holds only `{name, description}`, just enough for `skill_search` to find
the right name. Keeping "searchable" (Qdrant) and "authoritative" (this module) as two systems means a skill's
instructions can't drift from what's on disk.

Skills are bundled app capabilities, not tenant data: no `SecurityCtx`, like `builtin.py::calculator`.
"""
from dataclasses import dataclass
from pathlib import Path

from app.agent.tools import frontmatter as fm
from app.core.config import DOMAINS_DIR


class SkillLoadError(ValueError):
    """A SKILL.md file doesn't match the required shape (frontmatter with
    non-empty `name`/`description`, plus a non-empty body). Raised by
    `_parse_skill_file`; `load_skills` skips the offending file rather than
    letting one bad skill take down the whole catalog (GRAPH_PATTERNS.md
    pattern 10's degrade-gracefully posture)."""


@dataclass(frozen=True)
class SkillRecord:
    name: str
    description: str
    body: str
    domains: tuple[str, ...] | None  # None = shared: visible to every domain (Ecorp too)
    path: Path


def _parse_skill_file(path: Path) -> SkillRecord:
    frontmatter, body = fm.split_file(path, SkillLoadError)
    if "domains" in frontmatter:
        raise SkillLoadError(f"{path}: 'domains' is not frontmatter: the owner folder says who may see a skill.")
    owner = fm.owner_of(path)
    return SkillRecord(
        name=fm.required_text(frontmatter, "name", path, SkillLoadError),
        description=fm.required_text(frontmatter, "description", path, SkillLoadError),
        body=fm.required_body(body, path, "skill", SkillLoadError),
        domains=None if owner == fm.SHARED else (owner,),
        path=path,
    )


def load_skills(domains_dir: str | Path | None = None) -> dict[str, SkillRecord]:
    """Scans `domains_dir/*/skills/*/SKILL.md` (default `DOMAINS_DIR`) and returns
    `{name: SkillRecord}`; see `frontmatter.load_catalog` for how a malformed file
    or a duplicate name is handled."""
    root = Path(domains_dir) if domains_dir is not None else Path(DOMAINS_DIR)
    return fm.load_catalog(root, "SKILL.md", _parse_skill_file, SkillLoadError, "skill")


_skills_cache: dict[str, SkillRecord] | None = None


def get_skills() -> dict[str, SkillRecord]:
    """Lazy-loaded, process-wide catalog — same lazy-singleton shape as
    `app/adapters/outbound/embeddings/embeddings.py`'s `_get_sparse_model()`/`_get_reranker()`,
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
