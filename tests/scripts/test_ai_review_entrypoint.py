"""`python3 -m scripts.ai_review` is how ai-review.yml runs the reviewer, so the package must keep a
`__main__` that reaches `review.run`, and the workflow's skip guard must point at a file that exists.

Both fail quietly if they drift. The job is advisory and inert until a variable is set, so a renamed
entry point shows up as "the review never appeared", not as a red check. And `hashFiles('<path>') != ''`
is the job's `if`: a path that stopped existing turns every review into a clean skip.

The job runs the BASE commit's script (see the workflow), so the guard deliberately accepts both the
package and the pre-package single file for a while; these tests only require that at least one of the
paths it lists is real.
"""
import importlib.util
import os
import re
import runpy
from pathlib import Path

import pytest

from scripts.ai_review import review

REPO = Path(__file__).resolve().parents[2]
WORKFLOW = (REPO / ".github" / "workflows" / "ai-review.yml").read_text()


def test_the_module_the_workflow_runs_has_a_main_entry_point():
    match = re.search(r"^\s*run: python3 -m (\S+)\s*$", WORKFLOW, re.M)
    assert match, "ai-review.yml no longer runs `python3 -m <module>`; update this test with it"
    assert importlib.util.find_spec(f"{match.group(1)}.__main__") is not None, (
        f"`python3 -m {match.group(1)}` needs a {match.group(1)}/__main__.py"
    )


def test_main_hands_the_environment_to_run_and_exits_with_its_code(monkeypatch: pytest.MonkeyPatch):
    seen = []
    monkeypatch.setattr(review, "run", lambda env: seen.append(env) or 7)
    with pytest.raises(SystemExit) as exit_:
        runpy.run_module("scripts.ai_review", run_name="__main__")
    assert exit_.value.code == 7
    assert len(seen) == 1 and seen[0] is os.environ


def test_the_workflow_skip_guard_lists_at_least_one_file_that_exists():
    guard = re.search(r"hashFiles\(([^)]*)\)", WORKFLOW)
    assert guard, "ai-review.yml has no hashFiles(...) guard; update this test with it"
    paths = re.findall(r"'([^']+)'", guard.group(1))
    assert paths and any((REPO / p).is_file() for p in paths), (
        f"none of the guard's paths exist ({paths}); every review would be skipped"
    )
