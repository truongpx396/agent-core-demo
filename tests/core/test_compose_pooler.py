"""The connection budget of the pooler (specs/022): the pooled processes plus the direct clients must fit inside Postgres's `max_connections`.

Arithmetic over two compose files that no scanner does. `tests/integration/test_pgbouncer.py` runs the shipped pooler settings against a
real PgBouncer.
"""
import re
from pathlib import Path

import yaml

COMPOSE = Path(__file__).resolve().parents[2] / "deploy" / "compose"
DEV, PROD = COMPOSE / "docker-compose.yml", COMPOSE / "docker-compose.prod.yml"
# Postgres's own default, which the dev file leaves alone and the production file writes down.
DEFAULT_MAX_CONNECTIONS = 100
# What LiteLLM (and Langfuse in dev), the exporters and an operator's psql may hold beside the pooler.
HEADROOM_FOR_DIRECT_CLIENTS = 30


def _services(path: Path) -> dict:
    return yaml.safe_load(path.read_text())["services"]


def _env(service: dict) -> dict:
    env = service.get("environment") or {}
    return env if isinstance(env, dict) else dict(item.split("=", 1) for item in env)


def test_the_connection_budget_fits_inside_postgres_and_the_pool_cap_includes_its_reserve():
    for path in (DEV, PROD):
        env = _env(_services(path)["pgbouncer"])
        cap = int(env["MAX_DB_CONNECTIONS"])
        assert cap == int(env["DEFAULT_POOL_SIZE"]) + int(env["RESERVE_POOL_SIZE"]), f"{path.name}: the hard cap is the pool plus its reserve"
        command = " ".join(_services(path)["postgres"]["command"])
        match = re.search(r"max_connections=(\d+)", command)
        max_connections = int(match.group(1)) if match else DEFAULT_MAX_CONNECTIONS
        databases = len(env["DATABASE_URLS"].split(","))
        assert databases * cap + HEADROOM_FOR_DIRECT_CLIENTS <= max_connections, (
            f"{path.name}: {databases} x {cap} pooled + {HEADROOM_FOR_DIRECT_CLIENTS} direct > max_connections={max_connections}"
        )
    assert re.search(r"max_connections=\d+", " ".join(_services(PROD)["postgres"]["command"])), "production writes the number down"
