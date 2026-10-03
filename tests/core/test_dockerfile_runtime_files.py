"""The container image must contain every file the app reads at runtime.

The Dockerfile used to `COPY app/` and nothing else. Skills (`skills/*/SKILL.md`),
subagents (`subagents/*/AGENT.md`) and the sandbox bridge
(`scripts/opensandbox_mcp_bridge.py`) all live OUTSIDE `app/`, and every one of
them degrades quietly when missing: `load_skills()` / `load_subagents()` return
`{}` when the directory is absent, and `load_sandbox_tools()` returns no tools
when the bridge process can't start. So the API and worker containers booted
healthy and simply had no skills, no delegation and no sandbox — measured
against the built image, not inferred (spec 007 B16, 009 B23, 006).

This is a TEXT test of the build inputs: the Dockerfile's `COPY` lines and the
`.dockerignore` patterns. It cannot prove the files really land in the image —
that is what CI's `docker-build` job does by running the built image. What it
adds is a failure in the default suite, without Docker, the moment someone moves
a catalog, renames the bridge, or adds an ignore pattern that would drop them.
The set of paths is DERIVED from the code's own settings rather than listed
here, so the test follows the app instead of duplicating it.
"""
import re
from pathlib import Path

import pytest

from app.core.config import Settings
from app.domains import sandbox_tools

REPO = Path(__file__).resolve().parents[2]


def _copied_sources() -> set[str]:
    """Source arguments of every `COPY` in the Dockerfile, normalised to a
    repo-relative path without a trailing slash."""
    sources: set[str] = set()
    for line in (REPO / "Dockerfile").read_text().splitlines():
        match = re.match(r"\s*COPY\s+(?:--\S+\s+)*(.+)", line)
        if not match:
            continue
        *srcs, _dest = match.group(1).split()
        sources.update(src.strip("/").removeprefix("./") for src in srcs)
    return sources


def _runtime_files() -> dict[str, str]:
    """What the app opens at runtime outside `app/`, as {description: repo-relative file}."""
    skills_dir = Settings.model_fields["skills_dir"].default
    subagents_dir = Settings.model_fields["subagents_dir"].default
    return {
        "the skill catalog": f"{skills_dir}/onboarding-brief/SKILL.md",
        "the subagent catalog": f"{subagents_dir}/researcher/AGENT.md",
        "the sandbox MCP bridge": str(Path(sandbox_tools._BRIDGE_SCRIPT).relative_to(REPO)),
        "the scripts package (index_skills, seed, cron jobs)": "scripts/__init__.py",
    }


def _is_covered(path: str, sources: set[str]) -> bool:
    return any(path == src or path.startswith(src + "/") for src in sources)


@pytest.mark.parametrize("what", list(_runtime_files()))
def test_the_dockerfile_copies_what_the_app_reads_at_runtime(what):
    path = _runtime_files()[what]
    assert (REPO / path).is_file(), f"test setup: {path} no longer exists, update _runtime_files()"

    assert _is_covered(path, _copied_sources()), (
        f"the Dockerfile never COPYs {path} ({what}); the image would boot healthy without it"
    )


def _dockerignore_patterns() -> list[str]:
    lines = (REPO / ".dockerignore").read_text().splitlines()
    return [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")]


def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Docker's `.dockerignore` matching, the part this repo can hit: `**`
    spans directories, `*` and `?` do not cross a `/`, and a pattern with no
    leading `**` is anchored at the context root."""
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out += "(?:.*/)?"
            i += 3
        elif pattern.startswith("**", i):
            out += ".*"
            i += 2
        elif pattern[i] == "*":
            out += "[^/]*"
            i += 1
        elif pattern[i] == "?":
            out += "[^/]"
            i += 1
        else:
            out += re.escape(pattern[i])
            i += 1
    return re.compile(out + r"\Z")


def _excluded(path: str, patterns: list[str]) -> bool:
    """Last matching pattern wins (`!pattern` re-includes); a pattern that
    matches a parent directory excludes everything under it."""
    excluded = False
    for raw in patterns:
        negated = raw.startswith("!")
        regex = _glob_to_regex(raw.removeprefix("!").strip("/"))
        parts = path.split("/")
        parents = ["/".join(parts[: n + 1]) for n in range(len(parts))]
        if any(regex.match(candidate) for candidate in parents):
            excluded = not negated
    return excluded


@pytest.mark.parametrize("what", list(_runtime_files()))
def test_dockerignore_does_not_drop_what_the_app_reads_at_runtime(what):
    path = _runtime_files()[what]

    assert not _excluded(path, _dockerignore_patterns()), (
        f".dockerignore excludes {path} ({what}) from the build context, so even a COPY of it would be empty"
    )


def test_the_dockerignore_matcher_agrees_with_the_documented_behavior():
    """Pins the helper above to Docker's documented rules, so a green result
    above means something: `*.md` is root-only, `**/*.md` is not."""
    assert _excluded("README.md", ["*.md"])
    assert not _excluded("skills/onboarding-brief/SKILL.md", ["*.md"])
    assert _excluded("skills/onboarding-brief/SKILL.md", ["**/*.md"])
    assert _excluded("skills/onboarding-brief/SKILL.md", ["skills"])
    assert not _excluded("skills/onboarding-brief/SKILL.md", ["**/*.md", "!skills/**/SKILL.md"])
