"""A real Postgres for the package's integration tests, seeded with ONLY the scripts the package ships (`credit_billing.schema`).

The repo's own `tests/containers.py` seeds a container from every script in `postgres-init/`; the package cannot (it must not know
that directory exists), and it does not need to: the wallet and the webhook inbox are two scripts. They open with `\\connect appdata`,
a `psql` meta-command, so the container's own init entrypoint (which runs `*.sql` files through `psql`, in filename order) is the
simplest way to apply them unmodified, after one line that creates the database they connect to.

The test SKIPS, never fails, when Docker is unreachable (the integration tier is opt-in: `pytest -m integration`).
"""
import atexit
import functools
import shutil
import tempfile
import time
from pathlib import Path

import pytest

from credit_billing import schema


def _require_docker() -> None:
    try:
        import docker

        docker.from_env().ping()
    except Exception as exc:  # noqa: BLE001 - any failure to reach Docker means "skip", whatever the reason
        pytest.skip(f"Docker is not reachable ({type(exc).__name__}): the package's real-Postgres tests need it")


def write_init_dir() -> Path:
    """The directory the container mounts at `/docker-entrypoint-initdb.d`: one line creating `appdata`, then the shipped scripts.

    It is made readable by other users on purpose. `tempfile.mkdtemp` creates a directory `0700`, owned by whoever runs the tests, and the
    image's entrypoint reads the directory as the `postgres` user. On macOS Docker Desktop that goes unnoticed (host ownership is mapped
    away), but on a Linux runner the bind mount keeps the real owner, the entrypoint dies on `ls: cannot open directory
    '/docker-entrypoint-initdb.d/': Permission denied`, the container exits with code 2, and every test errors at setup with Docker's
    `409 container ... is not running`. That is how the first PR C run failed `test-integration`'s package step in CI after passing locally.
    The repo's own helper never hit it because it mounts the checked-out `postgres-init/` directory, which is `0755`.
    """
    init = Path(tempfile.mkdtemp(prefix="credit-billing-init-"))
    (init / "01-appdata.sql").write_text("CREATE DATABASE appdata OWNER langfuse;\n")
    for name in schema.scripts():
        (init / name).write_text(schema.read(name))
    init.chmod(0o755)
    for script in init.iterdir():
        script.chmod(0o644)
    return init


@functools.lru_cache(maxsize=1)
def ensure_postgres() -> dict[str, str]:
    """Real Postgres 16 with the shipped schema applied. One container per test process, stopped at exit."""
    _require_docker()
    import psycopg
    from testcontainers.postgres import PostgresContainer

    init = write_init_dir()
    atexit.register(shutil.rmtree, init, ignore_errors=True)  # registered first, so it runs after the container is stopped (atexit is LIFO)

    container = PostgresContainer(image="postgres:16", username="langfuse", password="langfuse", dbname="langfuse")
    container.with_volume_mapping(str(init), "/docker-entrypoint-initdb.d", mode="ro")
    container.start()
    atexit.register(container.stop)
    url = f"postgresql://langfuse:langfuse@{container.get_container_host_ip()}:{int(container.get_exposed_port(5432))}/appdata"
    for _ in range(30):  # the init scripts run on a temporary server first; wait for the real one to accept the appdata connection
        try:
            with psycopg.connect(url, connect_timeout=2):
                break
        except Exception:  # noqa: BLE001 - retry until the loop ends
            time.sleep(1)
    else:
        raise RuntimeError("the appdata database never came up")
    return {"appdata_database_url": url}
