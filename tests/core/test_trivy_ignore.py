"""The Trivy gate, the dependency it is about, and the one exception we carry.

On 2026-10-21/22 CI's `trivy` job went red on every PR without any of them touching a dependency: its
vulnerability database learned about four CRITICAL findings in packages the lock already pinned.

  * certifi 2026.7.22 (GHSA-6q48-6c7w-9gw2): the bundle trusts emSign intermediates whose keys are compromised.
    Fixed by pinning 2026.10.19, and by keeping pip out of the shipped image: its vendored copy
    (pip/_vendor/certifi) is what Trivy's image scan read as a second certifi that the lock cannot reach. (The image
    first upgraded pip for a fresh copy; the two-stage build removes it, ENGINEERING_PATTERNS.md pattern 76.)
  * pillow 12.3.0 (CVE-2026-112403, -112393, -112900): fixed only in 13.0.0, and fastembed, which the app uses
    for sparse retrieval, declares `pillow<13.0` in every release through 0.9.0. They cannot be installed
    together, so `.trivyignore.yaml` carries a dated, reasoned exception instead of a lock that pip would reject.

A security exception is only honest while its reasons hold. These tests are those reasons, written down so a
change that breaks one fails here and not in a quiet drift: no first-party code touches Pillow, the image has no
Ghostscript, the lock still pins what the exception is about, every entry expires, and only the hard gates read it.
A TEXT test: no Docker and no Trivy needed (the exception was verified with the real scanner when it was written).
"""
import ast
import re
from datetime import date
from pathlib import Path

import pytest
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
    def test_the_file_has_entries_or_this_proves_nothing(self):
        assert {e["id"] for e in _entries()} == {"CVE-2026-112403", "CVE-2026-112393", "CVE-2026-112900"}

    @pytest.mark.parametrize("entry", _entries(), ids=lambda e: e["id"])
    def test_an_entry_says_why_and_until_when(self, entry):
        assert re.fullmatch(r"(CVE-\d{4}-\d+|GHSA(-[a-z0-9]{4}){3})", entry["id"])
        assert len(str(entry.get("statement", "")).strip()) >= 80, f"{entry['id']}: say why it is safe to carry"
        assert isinstance(entry.get("expired_at"), date), f"{entry['id']}: an exception with no expiry is permanent"
        assert "paths" not in entry, "the image scan reports Python packages under the target 'Python': a path would never match"


class TestTheReasonsTheExceptionRestsOn:
    def test_the_lock_still_pins_a_pillow_the_exception_is_about(self):
        """12.3.0 is affected, 13.0.0 is the fix. Once the lock moves to 13, the entries are dead weight and
        would hide the next Pillow finding that has the same ids: remove them."""
        assert Version(_lock()["pillow"]) < Version("13.0.0"), "pillow is on 13+ now: delete the entries in .trivyignore.yaml"

    def test_no_first_party_code_touches_pillow(self):
        """All three findings need the app (or a package it calls) to decode, thumbnail, save or open an image with
        Pillow. None of app/ or scripts/ imports it or calls the two methods the JPEG finding needs."""
        offenders = []
        for root in ("app", "scripts"):
            for path in sorted((REPO / root).rglob("*.py")):
                tree = ast.parse(path.read_text(), filename=str(path))
                for node in ast.walk(tree):
                    imported = []
                    if isinstance(node, ast.Import):
                        imported = [alias.name for alias in node.names]
                    elif isinstance(node, ast.ImportFrom):
                        imported = [node.module or ""]
                    if any(name == "PIL" or name.startswith("PIL.") for name in imported):
                        offenders.append(f"{path.relative_to(REPO)}:{node.lineno} imports PIL")
                    if isinstance(node, ast.Attribute) and node.attr in {"thumbnail", "draft"}:
                        offenders.append(f"{path.relative_to(REPO)}:{node.lineno} uses .{node.attr}")
        assert not offenders, (
            "first-party code now reaches Pillow, so CVE-2026-112403/-112393/-112900 are no longer 'not reachable' "
            f"and .trivyignore.yaml's statements are false: {offenders}"
        )

    def test_the_image_has_no_ghostscript(self):
        """CVE-2026-112900 runs Pillow's EPS path, which shells out to `gs`."""
        dockerfile = (REPO / "Dockerfile").read_text().lower()
        assert "ghostscript" not in dockerfile and "libgs" not in dockerfile


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
