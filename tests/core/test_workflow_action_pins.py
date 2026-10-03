"""Every third-party GitHub Action in .github/workflows is pinned to a full commit SHA.

A tag such as `@v4` is a mutable pointer: whoever controls the action's repo (or whoever
compromises it) can move it, and the next CI run executes the new code with this repo's
secrets and write-scoped token. A 40-hex commit SHA cannot be moved. Dependabot
(.github/dependabot.yml) bumps the pins, so pinning costs nothing in freshness.

This is a plain text scan, not a YAML parse, on purpose: it needs no extra dependency and
cannot be fooled by YAML anchors or quoting into skipping a `uses:` line.
"""

import re
from pathlib import Path

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"

# `uses: owner/repo[/subpath]@ref`, optionally a list item, optionally with a trailing comment.
_USES = re.compile(r"^\s*(?:-\s+)?uses:\s*(?P<ref>\S+)(?P<rest>.*)$")
_SHA_PIN = re.compile(r"^[^@\s]+@[0-9a-f]{40}$")
# The human-readable version Dependabot rewrites alongside the SHA, e.g. `# v4.4.0`.
_VERSION_COMMENT = re.compile(r"#\s*v\d+(\.\d+)*")


def _uses_lines() -> list[tuple[str, int, str, str]]:
    found = []
    for path in sorted(WORKFLOWS.glob("*.yml")):
        for number, line in enumerate(path.read_text().splitlines(), start=1):
            match = _USES.match(line)
            if match:
                found.append((path.name, number, match["ref"], match["rest"]))
    return found


def test_the_scan_actually_finds_the_workflows_uses_lines() -> None:
    # Guards the guard: a wrong WORKFLOWS path would make the assertions below vacuously pass.
    assert len(_uses_lines()) > 10


def test_every_remote_action_is_pinned_to_a_full_commit_sha() -> None:
    unpinned = [
        f"{name}:{number}: {ref}"
        for name, number, ref, _ in _uses_lines()
        # Local composite actions (`./path`) live in this repo, so they are already pinned by
        # the commit being built.
        if not ref.startswith("./") and not _SHA_PIN.match(ref)
    ]
    assert not unpinned, "pin these to a commit SHA (see .github/dependabot.yml):\n" + "\n".join(unpinned)


def test_every_pinned_action_keeps_a_version_comment_so_the_sha_stays_reviewable() -> None:
    bare = [
        f"{name}:{number}: {ref}"
        for name, number, ref, rest in _uses_lines()
        if _SHA_PIN.match(ref) and not _VERSION_COMMENT.search(rest)
    ]
    assert not bare, "add a trailing `# vX.Y.Z` comment:\n" + "\n".join(bare)
