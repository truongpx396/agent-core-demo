"""The authlimit service is a separate program (specs/018, task T048): the boundary in both directions.

The constitution's rule for self-contained packages (and, in spirit, for a service in another language) is enforced by a test that fails
the build, not by convention. Here the boundary is:

  * the service reads nothing of this repository at run time. Its non-test Go code opens no path outside its own module, and imports
    nothing but its own packages and third-party modules. (Its TESTS read the shared token vectors, which is the one declared coupling.)
  * the Python app and the packages reach it through NOTHING: no import, no subprocess, no path. They talk to it only over HTTP, through
    the front door, and PR 2 adds the app's reading of its public keys and its two JSON registries from the tests, not from app code.
"""
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SERVICE = REPO / "services" / "authlimit"
GO_FILES = [p for p in SERVICE.rglob("*.go")]
NON_TEST_GO = [p for p in GO_FILES if not p.name.endswith("_test.go")]


def _imports(path: Path) -> list[str]:
    text = path.read_text()
    block = re.findall(r"import\s*\((.*?)\)", text, re.DOTALL)
    single = re.findall(r'^import\s+(?:\w+\s+)?"([^"]+)"', text, re.MULTILINE)
    return single + [m for b in block for m in re.findall(r'"([^"]+)"', b)]


def test_the_service_has_go_code_for_this_test_to_be_about():
    """A floor against a vacuous pass (a glob that matches nothing makes every check below true), not a count to keep in step: the service
    arrives in three pull requests, and this must hold for each of them."""
    assert len(NON_TEST_GO) >= 10 and (SERVICE / "go.mod").exists()


def test_the_services_go_code_imports_only_itself_the_standard_library_and_third_party_modules():
    allowed_third_party = ("github.com/", "go.opentelemetry.io/", "golang.org/", "google.golang.org/")
    for path in GO_FILES:
        for imp in _imports(path):
            first = imp.split("/")[0]
            ok = imp == "authlimit" or imp.startswith("authlimit/") or "." not in first or imp.startswith(allowed_third_party)
            assert ok, f"{path.relative_to(REPO)} imports {imp!r}: nothing but the module itself, the standard library and third-party modules"


def _code_lines(path: Path) -> list[str]:
    """The lines of a Go file that are not comments (comments may name anything; code may not open it)."""
    return [line for line in path.read_text().splitlines() if not line.lstrip().startswith("//")]


def test_the_services_runtime_code_opens_no_path_outside_its_own_module():
    for path in NON_TEST_GO:
        for line in _code_lines(path):
            # A string literal that starts with ../ or has /../ in it, or a Join that climbs: all ways to read the repository around the module.
            # (A comparison with ".." is not one: the route table's path cleaner uses it to REFUSE dot segments.)
            assert not re.search(r'"\.\./|/\.\./', line) and not re.search(r'filepath\.Join\([^)]*"\.\."', line), (
                f"{path.relative_to(REPO)}: {line.strip()!r}: runtime code must not reach out of the module"
            )
            assert "auth-vectors" not in line, f"{path.relative_to(REPO)}: the shared vectors are for tests only"


def test_the_shared_vectors_are_opened_by_the_services_tests_and_nothing_else_in_go():
    readers = {p.relative_to(REPO).as_posix() for p in GO_FILES if any("auth-vectors" in line for line in _code_lines(p))}

    assert readers, "the vectors exist to be read by the Go verifier's tests"
    assert all(r.endswith("_test.go") for r in readers), readers


def test_the_python_app_and_packages_do_not_reach_into_the_service():
    offenders = []
    for root in (REPO / "app", REPO / "packages"):
        for path in root.rglob("*"):
            if path.suffix in {".py", ".sql", ".toml", ".md"} and "services/authlimit" in path.read_text(errors="ignore"):
                offenders.append(path.relative_to(REPO).as_posix())

    assert not offenders, f"{offenders}: the app talks to the service over HTTP only (PR 2 reads its JSON registries from tests, not from app code)"


def test_no_private_key_material_is_committed_for_the_service():
    pem = re.compile(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----")
    for root in (SERVICE, REPO / "testdata"):
        for path in root.rglob("*"):
            if path.is_file() and path.suffix not in {".png", ".gif"}:
                assert not pem.search(path.read_text(errors="ignore")), f"{path.relative_to(REPO)} contains a private key"
    assert not list(SERVICE.rglob("*.pem")), "a .pem under the service directory would be one `git add` from a commit"
