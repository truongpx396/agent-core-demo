"""The on-disk format `SKILL.md` and `AGENT.md` share: a YAML frontmatter block followed by a
markdown body, one file per directory, scanned into a `{name: record}` catalog. A catalog lives at
`<root>/<owner>/skills/<name>/SKILL.md` (or `.../subagents/<name>/AGENT.md`); the OWNER folder is the
domain the entry is for, or `shared` for a skill every domain may see.

`skill_catalog.py` and `subagent_catalog.py` each define their own record type and their own
required/optional fields; everything else (splitting the file, the validation messages, scanning a
directory, skipping a bad file without losing the rest, keeping the first of two duplicate names) is
here, once. Both catalogs keep their own error class, passed in as `error`, so a caller that
catches `SkillLoadError` or `SubagentLoadError` is unaffected.

Domain-agnostic on purpose, like both catalogs: it knows nothing about tools or capabilities.
"""
import logging
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

import yaml

logger = logging.getLogger(__name__)

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?\n)---\s*\n?", re.DOTALL)

R = TypeVar("R")

SHARED = "shared"  # the owner folder of what every domain may use


def owner_of(path: Path) -> str:
    """The owner folder of `<owner>/<kind>s/<name>/<file>`: a domain name, or `SHARED`."""
    return path.parents[2].name


def split_file(path: Path, error: type[ValueError]) -> tuple[dict[str, Any], str]:
    """`(frontmatter mapping, stripped body)` of one file. Raises `error` with the path in the
    message when the frontmatter is missing, not valid YAML, or not a mapping."""
    text = path.read_text(encoding="utf-8")
    match = _FRONTMATTER_RE.match(text)
    if not match:
        raise error(f"{path}: missing YAML frontmatter (expected a leading '---' block).")
    try:
        frontmatter = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as exc:
        raise error(f"{path}: invalid YAML frontmatter: {exc}") from exc
    if not isinstance(frontmatter, dict):
        raise error(f"{path}: frontmatter must be a YAML mapping.")
    return frontmatter, text[match.end() :].strip()


def required_text(frontmatter: dict[str, Any], key: str, path: Path, error: type[ValueError]) -> str:
    value = frontmatter.get(key)
    if not isinstance(value, str) or not value.strip():
        raise error(f"{path}: frontmatter must set a non-empty '{key}'.")
    return value.strip()


def optional_text(
    frontmatter: dict[str, Any], key: str, path: Path, error: type[ValueError]
) -> str | None:
    value = frontmatter.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise error(f"{path}: '{key}', if set, must be a non-empty string.")
    return value.strip()


def optional_string_list(
    frontmatter: dict[str, Any], key: str, path: Path, error: type[ValueError]
) -> tuple[str, ...] | None:
    value = frontmatter.get(key)
    if value is None:
        return None
    if isinstance(value, list) and all(isinstance(v, str) and v.strip() for v in value):
        return tuple(value)
    raise error(f"{path}: '{key}', if set, must be a list of non-empty strings.")


def required_body(body: str, path: Path, label: str, error: type[ValueError]) -> str:
    if not body:
        raise error(f"{path}: {label} body (markdown after frontmatter) must not be empty.")
    return body


def load_catalog(
    root: Path,
    filename: str,
    parse: Callable[[Path], R],
    error: type[ValueError],
    kind: str,
) -> dict[str, R]:
    """Scan `root/*/<kind>s/*/<filename>` into `{name: record}`. A malformed file is logged and skipped,
    never raised past this function, so one bad file can't take the whole catalog down
    (GRAPH_PATTERNS.md pattern 10's degrade-gracefully posture). A duplicate `name` across two
    directories keeps whichever was parsed first (directory order) and logs a warning; it's not an
    error, since a name collision is a content-authoring mistake, not a reason to refuse serving
    every OTHER entry. `kind` ("skill" / "subagent") only words the log lines."""
    if not root.is_dir():
        return {}

    catalog: dict[str, R] = {}
    for file in sorted(root.glob(f"*/{kind}s/*/{filename}")):
        try:
            record = parse(file)
        except error as exc:
            logger.warning("skipping invalid %s file", kind, extra={"error": str(exc)})
            continue
        name: str = record.name  # type: ignore[attr-defined]  # every catalog record has a name
        if name in catalog:
            logger.warning(
                "duplicate %s name; keeping the first one found", kind,
                extra={f"{kind}_name": name, "path": str(file)},
            )
            continue
        catalog[name] = record
    return catalog
