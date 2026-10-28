"""A test that starts the real API as a process must say which `AUTH_MODE` it wants (spec 018).

The API's default is `identity` mode, and its lifespan refuses to start without `AUTHLIMIT_KEYS_URL` (app/api/main.py, app/core/internal_identity.py's
`check_settings`). The hermetic suite says header mode once, in `tests/conftest.py`'s autouse `header_auth_mode`, which patches settings inside the
pytest process. A subprocess does not inherit a patch: it reads its own environment, finds no AUTH_MODE, runs in identity mode, and exits with code 3
before it is ready. That is how #208 went red in CI on `test_worker_scaling.py` (`uvicorn ... exited early (code 3) before becoming ready`) and
would have broken `tests/live/conftest.py` the same way. Neither tier runs by default, so a developer's `pytest` never saw it.

This test reads the sources, so it runs in the default tier. It is a guard on the SHAPE of the fixtures (the line is there); the real proof that the
subprocess starts is the integration and live tiers themselves.
"""
import re
from pathlib import Path

TESTS = Path(__file__).resolve().parents[1]

# `"uvicorn", "app.api.main:app"` however the list is laid out; the module path alone also appears in comments and docstrings.
LAUNCHES_THE_API = re.compile(r'"uvicorn",\s*"app\.api\.main:app"')


def _files_that_launch_the_api() -> list[Path]:
    return sorted(
        path for path in TESTS.rglob("*.py")
        if path != Path(__file__).resolve() and LAUNCHES_THE_API.search(path.read_text())
    )


def test_the_known_fixtures_that_start_the_api_are_found():
    # A guard that matches nothing passes forever (ENGINEERING_PATTERNS.md 88). If a fixture is renamed or the launch is written another way,
    # this fails and the pattern above needs updating, rather than the check going quiet.
    names = {path.relative_to(TESTS).as_posix() for path in _files_that_launch_the_api()}
    assert {"integration/test_worker_scaling.py", "live/conftest.py"} <= names


def test_every_test_fixture_that_starts_the_api_says_which_auth_mode():
    silent = [
        path.relative_to(TESTS).as_posix()
        for path in _files_that_launch_the_api()
        if '"AUTH_MODE"' not in path.read_text()
    ]
    assert not silent, (
        f"{silent} start `uvicorn app.api.main:app` without setting AUTH_MODE in the child's environment. The API defaults to identity mode and "
        "refuses to start without AUTHLIMIT_KEYS_URL, so the process exits with code 3. Add `\"AUTH_MODE\": \"headers\"` to the env dict "
        "(these fixtures call the API directly and have no front door)."
    )
