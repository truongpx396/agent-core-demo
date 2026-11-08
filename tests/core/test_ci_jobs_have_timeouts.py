"""Every job in CI declares `timeout-minutes`.

Without one, a stalled job runs to GitHub's 6-hour cap and holds the PR red-or-pending for that long: 28 of 300 runs between 2026-10-07 and
2026-11-08 had a job past 45 minutes, 20 of them `test-integration` against a healthy 10 (the comment above `jobs:` in ci.yml has the numbers
and how each limit was chosen). A job added later with no limit would bring that back unseen.
"""
from pathlib import Path

import yaml

CI = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"
GITHUB_JOB_CAP_MINUTES = 360


def _jobs() -> dict:
    return yaml.safe_load(CI.read_text(encoding="utf-8"))["jobs"]


def test_every_ci_job_has_a_timeout_below_githubs_own_cap():
    jobs = _jobs()
    assert jobs, "ci.yml has no jobs: the path or the file's shape changed"
    missing = sorted(name for name, job in jobs.items() if "timeout-minutes" not in job)
    assert not missing, f"jobs with no `timeout-minutes` (GitHub's default is {GITHUB_JOB_CAP_MINUTES}): {missing}"
    too_long = {name: job["timeout-minutes"] for name, job in jobs.items() if job["timeout-minutes"] >= GITHUB_JOB_CAP_MINUTES}
    assert not too_long, f"a limit at or above GitHub's own cap is no limit: {too_long}"
