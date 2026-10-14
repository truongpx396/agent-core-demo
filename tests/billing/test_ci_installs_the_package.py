"""A CI job that runs the app must install the billing package first (specs/011-billing-module-boundary, PR C).

The package is not in `requirements-lock.txt` (that file is a machine-generated freeze of requirements.txt), so a job that installs
only the lock file has no `credit_billing`. The repo's pytest `pythonpath` and mypy `mypy_path` make that invisible to the pytest and
mypy processes themselves, which is exactly why it went unnoticed: the first run of PR C was green locally (where `make` exports
PYTHONPATH and the dev venv installs the package) and red in `test-integration` and `promptfoo`, because those jobs start the app as
a SUBPROCESS (uvicorn, agent workers, `python -m promptfoo.dump_prompts`), and a subprocess inherits the environment, not pytest's
sys.path. It died on `from credit_billing.settings import ...` in `app/core/config.py`.

These tests read the workflows as text, so they need no Docker or runner. They fail on the shape that broke: a job that installs the
lock file, runs repo code, and has no step installing `./packages/billing` before it.
"""
import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
WORKFLOWS = sorted((REPO / ".github" / "workflows").glob("*.yml"))

# A step that executes repo code. `pip install` lines are excluded by the caller; `mkdir`, `docker exec ... ollama pull`, `npm install`
# and the like do not match, so only a step that could import `app` counts.
RUNS_REPO_CODE = re.compile(r"\b(pytest|make|uvicorn|mypy|python3?)\b")
INSTALLS_THE_PACKAGE = "./packages/billing"

# Jobs that install the lock file and deliberately do NOT install the package, each for a reason that is itself checked below.
EXEMPT = {
    ("ci.yml", "test"): "the hermetic suite is the job that proves pytest's `pythonpath` is enough on its own",
    ("ci.yml", "typecheck"): "mypy reads the sources through `mypy_path` and starts nothing",
}


def _run(step: dict) -> str:
    return " ".join(str(step.get("run", "")).split())


def _installs_lock(steps: list[dict]) -> int | None:
    return next((i for i, step in enumerate(steps) if "requirements-lock.txt" in _run(step)), None)


def _installs_package(step: dict) -> bool:
    return "pip install" in _run(step) and INSTALLS_THE_PACKAGE in _run(step)


def jobs_that_run_the_app_without_the_package(workflow: dict) -> list[str]:
    """Jobs that install the lock file, then run repo code with no earlier step installing the package."""
    offenders = []
    for name, job in workflow.get("jobs", {}).items():
        steps = job.get("steps", [])
        lock = _installs_lock(steps)
        if lock is None:
            continue
        first_run = next(
            (i for i, step in enumerate(steps) if i > lock and "pip install" not in _run(step) and RUNS_REPO_CODE.search(_run(step))),
            None,
        )
        if first_run is None:
            continue
        install = next((i for i, step in enumerate(steps) if _installs_package(step)), None)
        if install is None or install > first_run:
            offenders.append(name)
    return offenders


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_every_job_that_runs_the_app_installs_the_billing_package_first():
    offenders = [
        f"{path.name}:{job}"
        for path in WORKFLOWS
        for job in jobs_that_run_the_app_without_the_package(_load(path))
        if (path.name, job) not in EXEMPT
    ]

    assert offenders == [], f"these jobs start the app without `pip install --no-deps ./packages/billing` first: {offenders}"


def test_the_exemptions_are_real():
    """An exemption that no longer holds is a hole: the job must exist, install the lock file, and NOT install the package, because
    the reason it is exempt is that it proves the package is found without one."""
    for (file, job), reason in EXEMPT.items():
        steps = _load(REPO / ".github" / "workflows" / file)["jobs"][job]["steps"]

        assert _installs_lock(steps) is not None, f"{file}:{job} no longer installs the lock file, so the exemption ({reason}) is stale"
        assert not any(_installs_package(step) for step in steps), f"{file}:{job} installs the package, which defeats: {reason}"


def test_the_scan_covers_the_jobs_that_matter():
    """Guards the guard: an empty or misspelt scan would pass vacuously."""
    covered = {
        f"{path.name}:{name}"
        for path in WORKFLOWS
        for name, job in _load(path).get("jobs", {}).items()
        if _installs_lock(job.get("steps", [])) is not None
    }

    for job in ("ci.yml:test-integration", "ci.yml:test-live", "ci.yml:promptfoo", "ci.yml:deepeval", "redteam.yml:promptfoo-redteam"):
        assert job in covered, job


def _workflow(*steps: str) -> dict:
    return {"jobs": {"j": {"steps": [{"run": step} for step in steps]}}}


def test_the_scanner_does_flag_a_job_that_would_have_broken():
    lock = "pip install -r requirements-lock.txt pytest-xdist==3.8.0"
    package = "pip install --no-deps ./packages/billing"

    # What test-integration looked like in the first PR C run: lock file, app-starting pytest, the package only afterwards.
    assert jobs_that_run_the_app_without_the_package(_workflow(lock, "pytest -n auto -m integration -q", package)) == ["j"]
    # What promptfoo looked like: a plain `python -m` that imports the app.
    assert jobs_that_run_the_app_without_the_package(_workflow(lock, "python -m promptfoo.dump_prompts")) == ["j"]
    # make counts too.
    assert jobs_that_run_the_app_without_the_package(_workflow(lock, "make promptfoo-redteam")) == ["j"]


def test_the_scanner_accepts_what_is_correct_and_ignores_what_is_not_a_run_of_the_app():
    lock = "pip install -r requirements-lock.txt"
    package = "pip install --no-deps ./packages/billing"

    assert jobs_that_run_the_app_without_the_package(_workflow(lock, package, "pytest -q")) == []
    assert jobs_that_run_the_app_without_the_package(_workflow(lock, 'pip install "./packages/billing[test]"', "pytest -q")) == []
    # No lock file: not a job that runs the app on this install (the package's own job installs it alone).
    assert jobs_that_run_the_app_without_the_package(_workflow("pytest -q")) == []
    # A lock install followed only by steps that do not execute repo code.
    assert jobs_that_run_the_app_without_the_package(_workflow(lock, "mkdir -p ~/.cache", "docker exec ollama ollama pull qwen")) == []
