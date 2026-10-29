"""The Trivy gate, the dependencies it is about, and the exception list, which is empty today.

On 2026-10-21/22 CI's `trivy` job went red on every PR with no dependency change: its database learned of
CRITICAL findings in packages the lock already pinned.
- certifi 2026.7.22 (GHSA-6q48-6c7w-9gw2): the bundle trusts emSign intermediates with compromised keys. Fixed
  by pinning 2026.10.19 and by keeping pip out of the shipped image: its vendored copy (pip/_vendor/certifi)
  was what Trivy read as a second certifi the lock can't reach (the two-stage build removes it,
  ENGINEERING_PATTERNS.md pattern 76).
- pillow 12.3.0 (five CVEs, fixed only in 13.0.0) and brotli 1.2.0 (GHSA-9v8h-qqgm-4m35, fixed in 1.2.1).
  fastembed declared `pillow<13` until 0.9.1 (2026-10-24), so `.trivyignore.yaml` carried a dated exception
  for a week; 0.9.1 allows `pillow<14`, the lock moved and the exception went (pattern 72). The gate had been
  red on `main` for five days (16 runs) first.

A security gate is honest only while the fixes stay and any exception stays reasoned. These tests write that
down: the lock keeps the fixed versions, requirements.txt keeps the floor that allows them, every exception
entry (none now) has a reason and an expiry, and only the hard gates read the file. A TEXT test: no Docker or
Trivy needed.
"""
import re
from datetime import date
from pathlib import Path

import yaml
from packaging.version import Version

REPO = Path(__file__).resolve().parents[2]
IGNORE = REPO / ".trivyignore.yaml"


def _lock() -> dict[str, str]:
    pins = {}
    for line in (REPO / "requirements-lock.txt").read_text().splitlines():
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)==(\S+)", line.strip())
        if match:
            pins[match.group(1).lower()] = match.group(2)
    return pins


def _entries() -> list[dict]:
    return (yaml.safe_load(IGNORE.read_text()) or {}).get("vulnerabilities") or []


def test_the_lock_pins_the_fixed_certifi():
    assert Version(_lock()["certifi"]) >= Version("2026.10.19"), "GHSA-6q48-6c7w-9gw2 is fixed in 2026.10.19"


def test_the_shipped_image_has_no_pip():
    """pip vendors its own certifi, which the lock cannot change. The image used to upgrade pip to get a fresh copy; the
    runtime stage now removes pip altogether (the virtualenv uv builds has none), so there is no vendored certifi, and no
    future pip finding, to carry. The stage that ships is the LAST one, and it must not reinstall an installer."""
    dockerfile = (REPO / "Dockerfile").read_text()
    runtime = dockerfile.split("AS runtime", 1)[1]
    instructions = "\n".join(line for line in runtime.splitlines() if not line.lstrip().startswith("#"))

    assert "python -m pip uninstall -y pip" in instructions, "the runtime stage must remove the base image's pip"
    assert "pip install" not in instructions and "uv pip" not in instructions, "the shipped stage must not run an installer"
    assert "astral-sh/uv" not in instructions, "uv belongs to the builder stage; the shipped stage must not copy it"


class TestEveryExceptionIsDatedAndReasoned:
    """There are no entries today, so these hold for the next one: an exception with no reason or no date is how a
    temporary decision becomes permanent."""

    def test_the_file_is_a_list_trivy_can_read(self):
        assert isinstance(yaml.safe_load(IGNORE.read_text())["vulnerabilities"], list)

    def test_an_entry_says_why_and_until_when(self):
        for entry in _entries():
            assert re.fullmatch(r"(CVE-\d{4}-\d+|GHSA(-[a-z0-9]{4}){3})", entry["id"]), entry
            assert len(str(entry.get("statement", "")).strip()) >= 80, f"{entry['id']}: say why it is safe to carry"
            assert isinstance(entry.get("expired_at"), date), f"{entry['id']}: an exception with no expiry is permanent"
            assert "paths" not in entry, "the image scan reports Python packages under the target 'Python': a path would never match"


class TestTheFixesStayFixed:
    """The gate was red on main for five days on versions the lock could have moved off from the day fastembed allowed it.
    These keep the fix from drifting back: a loosened requirement, a re-generated lock from an old resolver cache."""

    def test_the_lock_pins_pillow_13_or_later(self):
        assert Version(_lock()["pillow"]) >= Version("13.0.0"), "CVE-2026-112403, -112393, -112900, -112398 and -96639 are fixed in 13.0.0"

    def test_the_lock_pins_the_fixed_brotli(self):
        assert Version(_lock()["brotli"]) >= Version("1.2.1"), "GHSA-9v8h-qqgm-4m35 is fixed in 1.2.1"

    def test_fastembed_is_a_release_that_allows_pillow_13(self):
        """0.8.x and 0.9.0 declare `pillow<13`; 0.9.1 is the first with `<14`. The floor in requirements.txt is what stops
        a resolver (or a person loosening a range) from picking an older one and, with it, an older Pillow."""
        floor = re.search(r"^fastembed>=([0-9.]+)", (REPO / "requirements.txt").read_text(), re.MULTILINE)

        assert floor and Version(floor.group(1)) >= Version("0.9.1"), "requirements.txt must keep fastembed>=0.9.1"
        assert Version(_lock()["fastembed"]) >= Version("0.9.1")


class TestOnlyTheHardGatesReadTheExceptions:
    """The full-report steps feed the Security tab. If they read the file too, an accepted risk would disappear
    from the one place that lists everything."""

    @staticmethod
    def _steps() -> list[dict]:
        workflow = yaml.safe_load((REPO / ".github" / "workflows" / "ci.yml").read_text())
        return [s for s in workflow["jobs"]["trivy"]["steps"] if "Trivy" in str(s.get("name", ""))]

    def test_both_hard_gates_read_the_file(self):
        gates = [s for s in self._steps() if "hard gate" in s["name"]]
        assert [s["name"].split(" — ")[0] for s in gates] == ["Trivy fs", "Trivy image"]
        for step in gates:
            assert step["with"].get("trivyignores") == ".trivyignore.yaml", step["name"]

    def test_the_reports_do_not(self):
        reports = [s for s in self._steps() if "full report" in s["name"]]
        assert len(reports) == 2
        for step in reports:
            assert "trivyignores" not in step["with"], step["name"]
