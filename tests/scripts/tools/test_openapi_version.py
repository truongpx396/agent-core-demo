"""The version gate: a contract change that forgot to raise `info.version` fails, whatever kind of change it was.

The decision is a pure function (`verdict`); the git and subprocess plumbing around it runs here against a throwaway repository and a stand-in
oasdiff that prints what the test tells it to, and, where the real oasdiff is installed, against the real one (CI's `openapi` job is the
other place that runs it for real, on every pull request).
"""
import json
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.tools.openapi_version import parse, run, verdict

CHANGE = {"id": "endpoint-added", "level": 1, "text": "endpoint added", "operation": "GET", "path": "/new"}
ABOUT_THE_VERSION = {"id": "api-major-version-not-bumped", "level": 1, "text": "a breaking change was detected but ...", "operation": None, "path": None}


@pytest.mark.parametrize("version", ["1.0.0", "0.0.1", "12.34.56"])
def test_a_version_of_three_numbers_is_read(version):
    assert parse(version) == tuple(int(part) for part in version.split("."))


@pytest.mark.parametrize("version", ["1.0", "v1.0.0", "1.0.0-rc.1", "", "1.0.x"])
def test_anything_else_is_not_a_version(version):
    assert parse(version) is None


def test_no_change_needs_no_new_version_whatever_the_versions_are():
    assert verdict("1.0.0", "1.0.0", []) is None
    assert verdict("2.0.0", "1.0.0", []) is None


def test_oasdiffs_own_remark_about_the_version_is_not_a_change_to_the_contract():
    assert verdict("1.0.0", "1.0.0", [ABOUT_THE_VERSION]) is None


@pytest.mark.parametrize("head", ["1.0.1", "1.1.0", "2.0.0"])
def test_a_change_with_the_version_raised_by_any_step_is_fine(head):
    assert verdict("1.0.0", head, [CHANGE]) is None


@pytest.mark.parametrize("head", ["1.0.0", "0.9.9"])
def test_a_change_without_a_higher_version_fails_and_says_what_changed_and_both_versions(head):
    why = verdict("1.0.0", head, [CHANGE, ABOUT_THE_VERSION])

    assert why is not None
    assert "endpoint-added GET /new" in why and head in why and "1.0.0" in why and "1 change(s)" in why


def test_a_head_version_that_is_not_three_numbers_fails_when_something_changed():
    assert "not MAJOR.MINOR.PATCH" in (verdict("1.0.0", "next", [CHANGE]) or "")


# --- the plumbing -------------------------------------------------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.email=t@example.com", "-c", "user.name=t", *args], cwd=repo, check=True, capture_output=True)


def _document(version: str) -> str:
    return json.dumps({"openapi": "3.1.0", "info": {"title": "t", "version": version}, "paths": {}}) + "\n"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repository whose HEAD has `openapi.json` at 1.0.0 (and no other document)."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "openapi.json").write_text(_document("1.0.0"), encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "base")
    return root


@pytest.fixture
def fake_oasdiff(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """An `oasdiff` that prints the file named by FAKE_CHANGES, so a test chooses what the changelog says."""
    script = tmp_path / "oasdiff"
    script.write_text(f"#!{sys.executable}\nimport os, sys\nsys.stdout.write(open(os.environ['FAKE_CHANGES'], encoding='utf-8').read())\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    changes = tmp_path / "changes.json"

    def say(listed: list[dict]) -> str:
        changes.write_text(json.dumps(listed), encoding="utf-8")
        monkeypatch.setenv("FAKE_CHANGES", str(changes))
        return str(script)

    return say


def test_a_changed_contract_with_the_same_version_fails_and_names_the_document(repo, fake_oasdiff, capsys):
    exe = fake_oasdiff([CHANGE])

    assert run("HEAD", repo=repo, documents=("openapi.json",), oasdiff=exe) == 1
    assert "openapi.json" in capsys.readouterr().err


def test_the_same_change_with_the_version_raised_passes(repo, fake_oasdiff):
    (repo / "openapi.json").write_text(_document("1.1.0"), encoding="utf-8")

    assert run("HEAD", repo=repo, documents=("openapi.json",), oasdiff=fake_oasdiff([CHANGE])) == 0


def test_an_unchanged_contract_passes_without_a_new_version(repo, fake_oasdiff):
    assert run("HEAD", repo=repo, documents=("openapi.json",), oasdiff=fake_oasdiff([])) == 0


def test_a_document_the_base_does_not_have_is_skipped_not_failed(repo, fake_oasdiff, capsys):
    assert run("HEAD", repo=repo, documents=("services/x/openapi.json",), oasdiff=fake_oasdiff([CHANGE])) == 0
    assert "nothing to compare" in capsys.readouterr().out


def test_every_document_is_judged_and_one_failure_fails_the_run(repo, fake_oasdiff):
    (repo / "second.json").write_text(_document("1.0.0"), encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "second")
    (repo / "openapi.json").write_text(_document("1.0.1"), encoding="utf-8")  # raised; second.json is not

    assert run("HEAD", repo=repo, documents=("openapi.json", "second.json"), oasdiff=fake_oasdiff([CHANGE])) == 1


def test_a_missing_oasdiff_is_exit_2_with_the_install_line(repo, monkeypatch, capsys):
    monkeypatch.setenv("PATH", "")

    assert run("HEAD", repo=repo, documents=("openapi.json",)) == 2
    assert "go install github.com/oasdiff/oasdiff" in capsys.readouterr().err


@pytest.mark.skipif(shutil.which("oasdiff") is None, reason="needs the real oasdiff (go install github.com/oasdiff/oasdiff@v1.35.0)")
def test_the_real_oasdiff_reports_a_change_in_the_shape_the_gate_reads(repo):
    """The stand-in above is only as good as this: oasdiff's own JSON has an `id` per change, and a bare description edit is not one."""
    document = json.loads((repo / "openapi.json").read_text(encoding="utf-8"))
    document["paths"] = {"/new": {"get": {"responses": {"200": {"description": "ok"}}}}}
    (repo / "openapi.json").write_text(json.dumps(document), encoding="utf-8")
    assert run("HEAD", repo=repo, documents=("openapi.json",)) == 1  # a route was added and the version stayed

    document["info"]["version"] = "1.0.1"
    (repo / "openapi.json").write_text(json.dumps(document), encoding="utf-8")
    assert run("HEAD", repo=repo, documents=("openapi.json",)) == 0

    document["info"]["description"] = "reworded"
    document["info"]["version"] = "1.0.0"
    document["paths"] = {}
    (repo / "openapi.json").write_text(json.dumps(document), encoding="utf-8")
    assert run("HEAD", repo=repo, documents=("openapi.json",)) == 0  # only the description differs: not a contract change
