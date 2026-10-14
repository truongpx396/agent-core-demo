"""Each slow tier of the package's tests lives in its own folder AND carries its own marker, and the two always agree.

The folder is for people (`ls tests/integration` is the real-Postgres tier, as in the repo's own `tests/`), the marker is what pytest
selects on (`pytest -m integration`, and the default `addopts` that keeps both slow tiers out of the plain run). If they drift, a
module that needs Docker or real provider credentials lands in the default run, or a hermetic module hides in a tier CI does not
treat the same way. These tests read the modules as text, so they need neither Docker nor credentials.
"""
import ast
from pathlib import Path

TESTS = Path(__file__).resolve().parent
TIERS = {"integration": TESTS / "integration", "provider_sandbox": TESTS / "provider_sandbox"}


def marks_of(path: Path) -> set[str]:
    """Names of the marks in the module-level `pytestmark` (one mark or a list), e.g. {"integration"}."""
    names: set[str] = set()
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in node.targets):
            for part in ast.walk(node.value):
                if isinstance(part, ast.Attribute) and isinstance(part.value, ast.Attribute) and part.value.attr == "mark":
                    names.add(part.attr)
    return names


def starts_a_postgres(path: Path) -> bool:
    """Whether the module uses `ensure_postgres`, the helper that starts a container (and so needs Docker)."""
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and any(alias.name == "ensure_postgres" for alias in node.names):
            return True
        if isinstance(node, ast.Name) and node.id == "ensure_postgres":
            return True
        if isinstance(node, ast.Attribute) and node.attr == "ensure_postgres":  # `containers.ensure_postgres()`
            return True
    return False


def all_test_modules():
    return sorted(TESTS.rglob("test_*.py"))


def _inside(path: Path, folder: Path) -> bool:
    return folder in path.parents


def test_every_module_in_a_tier_folder_carries_that_tiers_marker():
    for tier, folder in TIERS.items():
        unmarked = [p.name for p in sorted(folder.glob("test_*.py")) if tier not in marks_of(p)]

        assert unmarked == [], f"tests/{folder.name}/ modules without `pytestmark = pytest.mark.{tier}` would run in the default tier: {unmarked}"


def test_no_module_outside_a_tier_folder_carries_a_tier_marker():
    strays = [
        f"{p.relative_to(TESTS).as_posix()} ({sorted(marks_of(p) & TIERS.keys())})"
        for p in all_test_modules()
        if not any(_inside(p, folder) for folder in TIERS.values()) and marks_of(p) & TIERS.keys()
    ]

    assert strays == [], f"a slow-tier module outside its folder: {strays}"


def test_a_module_that_starts_a_postgres_is_an_integration_module():
    offenders = [
        p.relative_to(TESTS).as_posix()
        for p in all_test_modules()
        if starts_a_postgres(p) and not (_inside(p, TIERS["integration"]) and "integration" in marks_of(p))
    ]

    assert offenders == [], f"these start a container but are not in tests/integration/ with the integration marker: {offenders}"


def test_the_scan_sees_each_tier():
    """Guards the guard: an empty or misplaced folder would make the tests above pass vacuously."""
    for folder in TIERS.values():
        assert list(folder.glob("test_*.py")), f"no test modules found in {folder}"
    assert any(not any(_inside(p, f) for f in TIERS.values()) for p in all_test_modules()), "no hermetic modules found"
    assert (TIERS["integration"] / "__init__.py").exists(), "the folder is a package, like tests/provider_sandbox/ (the tests import `tests.integration.*`)"


def test_the_scanner_does_recognise_marks_and_container_use(tmp_path):
    marked = tmp_path / "test_a.py"
    for source, expected in (
        ("import pytest\npytestmark = pytest.mark.integration\n", {"integration"}),
        ("import pytest\npytestmark = [pytest.mark.integration, pytest.mark.xdist_group(name='x')]\n", {"integration", "xdist_group"}),
        ("import pytest\npytestmark = pytest.mark.provider_sandbox\n", {"provider_sandbox"}),
        ("import pytest\n\n@pytest.mark.integration\ndef test_one():\n    pass\n", set()),  # a per-test mark is not a module mark
        ("x = 1\n", set()),
    ):
        marked.write_text(source)

        assert marks_of(marked) == expected, source
    for source, expected in (
        ("from tests.containers import ensure_postgres\n", True),
        ("from tests import containers\ncontainers.ensure_postgres()\n", True),
        ("def f():\n    return ensure_postgres()\n", True),
        ("from tests.containers import write_init_dir\n", False),
    ):
        marked.write_text(source)

        assert starts_a_postgres(marked) is expected, source
