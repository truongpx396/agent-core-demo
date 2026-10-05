"""The `test-live` job's e2e step is split into a GATE and an ADVISORY step; keep the two honest.

Why a split exists (GRAPH_PATTERNS.md pattern 48): two e2e tests need the 3B model to chain several calls, and
neither a prompt change nor a tool change made that reliable, so they run in a non-blocking step instead of
turning the whole job red on the model's mood. The danger of any such split is silent drift in either
direction: a test quietly moved out of the gate, or the advisory step quietly becoming blocking again. These
read the workflow, the marker registration and the test file as text, so they need no Docker, browser or model.
"""
import ast
import tomllib
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
GATE = 'pytest -m "e2e and not advisory" -q'
ADVISORY = 'pytest -m "e2e and advisory" -q'
# Decided one by one, from the CI record, not "every multi-step test": adding to this set is a conscious choice.
EXPECTED_ADVISORY = {"test_a_skill_is_found_and_followed", "test_a_subagent_delegates_and_returns_a_real_answer"}


def _test_live_steps() -> list[dict]:
    workflow = yaml.safe_load((REPO / ".github" / "workflows" / "ci.yml").read_text())
    return workflow["jobs"]["test-live"]["steps"]


def _runs(steps: list[dict]) -> dict[str, dict]:
    return {" ".join(step["run"].split()): step for step in steps if "run" in step}


def test_the_advisory_marker_is_registered():
    markers = tomllib.loads((REPO / "pyproject.toml").read_text())["tool"]["pytest"]["ini_options"]["markers"]
    assert any(marker.startswith("advisory:") for marker in markers)


def test_the_gate_step_excludes_advisory_tests_and_is_blocking():
    gate = _runs(_test_live_steps())[GATE]
    assert "continue-on-error" not in gate  # a red here must fail the job


def test_the_advisory_step_runs_even_after_a_failed_gate_and_never_blocks():
    steps = _test_live_steps()
    advisory = _runs(steps)[ADVISORY]
    assert advisory["continue-on-error"] is True
    assert "!cancelled()" in advisory["if"]  # one run shows both, even when the gate is red
    assert steps.index(advisory) > steps.index(_runs(steps)[GATE])


def test_no_step_runs_every_e2e_test_at_once_because_that_would_run_the_advisory_ones_in_the_gate():
    plain = [run for run in _runs(_test_live_steps()) if "-m e2e" in run or '-m "e2e"' in run]
    assert plain == []


def test_exactly_the_documented_tests_carry_the_advisory_marker():
    tree = ast.parse((REPO / "tests" / "live" / "test_chat_ui.py").read_text())
    marked = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and any(ast.unparse(decorator) == "pytest.mark.advisory" for decorator in node.decorator_list)
    }
    assert marked == EXPECTED_ADVISORY
