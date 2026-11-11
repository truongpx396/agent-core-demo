"""Fail when an OpenAPI document's contract changed against a base and its `info.version` did not go up.

    python -m scripts.tools.openapi_version --base origin/main        # `make openapi-version`; CI's `openapi` job runs it on every PR

The version is the CONTRACT's, bumped by hand, so something has to notice a change that forgot it. Two halves, because oasdiff covers one:

  * a BREAKING change must raise the major version: oasdiff does this itself once its severity is raised from info to error
    (.github/openapi-severity-levels.txt, passed by `make openapi-breaking`);
  * ANY change to the contract must raise the version, which oasdiff does not check and this does. "A change" is what `oasdiff changelog`
    reports: a route, a status, a field, a security rule, an event. A reworded description and the version itself are not changes.

Which of patch, minor or major is the author's call (a fix, an addition, a break); this only requires that it went up. Stdlib only, so the CI
job needs nothing but oasdiff.
"""
import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]

# Each document sits beside the Dockerfile of the process it describes (the same list as make/test.mk's OPENAPI_DOCUMENTS).
DOCUMENTS = ("openapi.json", "services/ml-service/openapi.json", "services/authlimit/openapi.json")

SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
# oasdiff's own remark on the version, which is not a change to the contract (and would make a missed bump look like two problems).
ABOUT_THE_VERSION = {"api-version-not-bumped", "api-major-version-not-bumped"}


def parse(version: str) -> tuple[int, int, int] | None:
    found = SEMVER.match(version)
    return (int(found[1]), int(found[2]), int(found[3])) if found else None


def verdict(base_version: str, head_version: str, changes: list[dict[str, Any]]) -> str | None:
    """Why the version is wrong for these changes, or None when it is fine."""
    real = [change for change in changes if change.get("id") not in ABOUT_THE_VERSION]
    if not real:
        return None
    base, head = parse(base_version), parse(head_version)
    if head is None:
        return f"version {head_version!r} is not MAJOR.MINOR.PATCH"
    if base is not None and head <= base:
        examples = sorted({" ".join(filter(None, [c.get("id"), c.get("operation"), c.get("path")])) for c in real})[:3]
        return f"the contract changed ({len(real)} change(s), e.g. {'; '.join(examples)}) and the version is {head_version}, not above the base's {base_version}"
    return None


def _git_show(repo: Path, ref: str, path: str) -> str | None:
    shown = subprocess.run(["git", "show", f"{ref}:{path}"], cwd=repo, capture_output=True, text=True, encoding="utf-8", check=False)
    return shown.stdout if shown.returncode == 0 else None


def _changelog(oasdiff: str, repo: Path, base: str, path: str) -> list[dict[str, Any]]:
    out = subprocess.run(
        [oasdiff, "changelog", f"{base}:{path}", path, "--format", "json"], cwd=repo, capture_output=True, text=True, encoding="utf-8", check=False
    )
    if out.returncode != 0:
        raise RuntimeError(f"oasdiff failed on {path}: {out.stderr.strip() or out.stdout.strip()}")
    changes: list[dict[str, Any]] = json.loads(out.stdout) or []
    return changes


def run(base: str, *, repo: Path = REPO, documents: tuple[str, ...] = DOCUMENTS, oasdiff: str | None = None) -> int:
    """0 when every document's version fits its changes, 1 when one does not (each is named), 2 when oasdiff is missing."""
    exe = oasdiff or shutil.which("oasdiff")
    if exe is None:
        print("oasdiff is not installed: go install github.com/oasdiff/oasdiff@v1.35.0", file=sys.stderr)
        return 2
    failed = False
    for path in documents:
        before = _git_show(repo, base, path)
        if before is None:
            print(f"{path}: not in {base}, nothing to compare")
            continue
        base_version = json.loads(before)["info"]["version"]
        head_version = json.loads((repo / path).read_text(encoding="utf-8"))["info"]["version"]
        problem = verdict(base_version, head_version, _changelog(exe, repo, base, path))
        if problem:
            failed = True
            print(f"{path}: {problem}. Raise info.version (a fix, an addition or a break: your call; a break needs the major).", file=sys.stderr)
        else:
            print(f"{path}: version {head_version} fits the changes against {base} ({base_version})")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    parser.add_argument("--base", default="origin/main", help="the git ref whose documents are compared with the working tree's")
    return run(parser.parse_args(argv).base)


if __name__ == "__main__":
    sys.exit(main())
