"""The directory the integration tier mounts into the Postgres image must be readable by the image's own user.

`tests/containers.py::write_init_dir` builds it. Its permissions cannot be checked by the integration tier on a developer's Mac, where
Docker Desktop hides file ownership, which is exactly how a `0700` directory (what `tempfile.mkdtemp` makes) passed locally and then
killed the Postgres container on a Linux CI runner (`Permission denied` reading `/docker-entrypoint-initdb.d/`, exit code 2, every
real-Postgres test erroring at setup). This pins the mode bits instead, with no Docker.
"""
import shutil
import stat

import pytest

from credit_billing import schema
from tests.containers import write_init_dir


@pytest.fixture
def init_dir():
    path = write_init_dir()
    yield path
    shutil.rmtree(path, ignore_errors=True)


def test_the_directory_can_be_listed_and_entered_by_another_user(init_dir):
    mode = stat.S_IMODE(init_dir.stat().st_mode)

    assert mode & 0o005 == 0o005, f"mode {mode:04o}: the postgres user in the container is not the owner, so it needs r-x as `other`"


def test_every_script_can_be_read_by_another_user(init_dir):
    for script in init_dir.iterdir():
        mode = stat.S_IMODE(script.stat().st_mode)

        assert mode & 0o004 == 0o004, f"{script.name} mode {mode:04o}: the container's postgres user needs to read it as `other`"


def test_it_holds_the_appdata_line_then_exactly_the_shipped_scripts_in_order(init_dir):
    # The entrypoint runs `*.sql` in filename order, so the database must be created before the scripts that `\connect` to it.
    assert sorted(path.name for path in init_dir.iterdir()) == ["01-appdata.sql", *schema.scripts()]
    assert "CREATE DATABASE appdata" in (init_dir / "01-appdata.sql").read_text()
    for name in schema.scripts():
        assert (init_dir / name).read_text() == schema.read(name), f"{name} must be the shipped script, byte for byte"
