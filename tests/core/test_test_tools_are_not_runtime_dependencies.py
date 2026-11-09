"""The test runner is a dev dependency: the runtime requirements and the lock file (what the app image installs) name no test tool.

`pytest` and `pytest-asyncio` sat in `requirements.txt`, so the lock file carried them and the production image shipped a test runner (and its
`pluggy`, `iniconfig`) it never runs. CI's pytest jobs install the lock, so each now names both pins itself; they must equal
`requirements-dev.txt`, or a local run and CI test against different runners.
"""
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
CI = ROOT / ".github" / "workflows" / "ci.yml"
# Installs only `./packages/billing[test]`, whose own pyproject pins its test tools (the job exists to prove the package needs nothing else).
OWN_TEST_EXTRA = {"billing-package"}
RUNS_PYTEST = re.compile(r"(?m)^\s*(?:[A-Z_]+=\S+\s+)*pytest\s")


def _requirement_names(filename: str) -> set[str]:
    names = set()
    for line in (ROOT / filename).read_text(encoding="utf-8").splitlines():
        match = re.match(r"([A-Za-z0-9][A-Za-z0-9._-]*)", line)
        if match and not line.startswith("#"):
            names.add(re.sub(r"[-_.]+", "-", match.group(1)).lower())
    return names


def _dev_pins() -> dict[str, str]:
    text = (ROOT / "requirements-dev.txt").read_text(encoding="utf-8")
    pins = {name: re.search(rf"(?m)^{name}==(\S+)$", text) for name in ("pytest", "pytest-asyncio")}
    assert all(pins.values()), f"requirements-dev.txt must pin pytest and pytest-asyncio exactly: {pins}"
    return {name: match.group(1) for name, match in pins.items()}


def test_the_runtime_requirements_and_the_lock_file_name_no_pytest_package():
    for filename in ("requirements.txt", "requirements-lock.txt"):
        found = sorted(n for n in _requirement_names(filename) if n == "pytest" or n.startswith("pytest-"))
        assert not found, f"{filename} is what the app image installs; a test tool belongs in requirements-dev.txt: {found}"


def test_each_ci_job_that_runs_pytest_pins_what_requirements_dev_pins():
    pins = _dev_pins()
    jobs = yaml.safe_load(CI.read_text(encoding="utf-8"))["jobs"]
    checked = []
    for name, job in jobs.items():
        runs = "\n".join(step["run"] for step in job.get("steps", []) if "run" in step)
        if name in OWN_TEST_EXTRA or not RUNS_PYTEST.search(runs):
            continue
        checked.append(name)
        for tool, version in pins.items():
            assert re.search(rf"(?<![\w-]){re.escape(tool)}=={re.escape(version)}(?![\w.])", runs), (
                f"ci.yml job `{name}` runs pytest but does not install {tool}=={version} (the lock file no longer carries it)"
            )
    assert {"test", "test-integration", "test-live", "deepeval"} <= set(checked), f"pytest jobs found: {checked}"
