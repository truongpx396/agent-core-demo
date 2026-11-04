"""The deployment contract of the connection pooler (specs/022): what runs, how it is pinned and hardened, what reaches it, and what the numbers
add up to. Static, reading the YAML (no Docker); `tests/integration/test_pgbouncer.py` runs the shipped pooler settings against a real
PgBouncer, and the stack was brought up from these files when this was written (specs/022 research.md). The profiler's half is
`test_compose_profiling.py`.
"""
import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
COMPOSE = REPO / "deploy" / "compose"
DEV, PROD = COMPOSE / "docker-compose.yml", COMPOSE / "docker-compose.prod.yml"
PINNED = re.compile(r"^[\w./-]+:[\w.-]+@sha256:[0-9a-f]{64}$")
# Postgres's own default, which the dev file leaves alone and the production file writes down.
DEFAULT_MAX_CONNECTIONS = 100
# What LiteLLM (and Langfuse in dev), the exporters and an operator's psql may hold beside the pooler.
HEADROOM_FOR_DIRECT_CLIENTS = 30
# The series the exporter emits, read from a running pgbouncer-exporter v0.12.1; an alert may name only these.
EXPORTER_METRICS = {
    "pgbouncer_up",
    "pgbouncer_pools_client_waiting_connections",
    "pgbouncer_pools_client_maxwait_seconds",
}


def _doc(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def _services(path: Path) -> dict:
    return _doc(path)["services"]


def _env(service: dict) -> dict:
    env = service.get("environment") or {}
    return env if isinstance(env, dict) else dict(item.split("=", 1) for item in env)


def test_the_pooler_and_the_exporter_are_pinned_by_digest_and_dev_runs_what_production_runs():
    for name in ("pgbouncer", "pgbouncer-exporter"):
        assert PINNED.match(_services(DEV)[name]["image"]), f"{name}: {_services(DEV)[name]['image']}"
        assert _services(DEV)[name]["image"] == _services(PROD)[name]["image"], f"{name}: dev and production differ"


def test_what_was_run_hardened_is_hardened_and_its_one_write_path_is_the_only_one():
    """Each of these was run read-only with no capabilities and no new privileges, and the container's diff and log read for a write that
    failed (specs/022 research.md). The pooler writes the config its entrypoint generates, to one tmpfs; the exporter writes nothing."""
    for path in (DEV, PROD):
        for name in ("pgbouncer", "pgbouncer-exporter"):
            service = _services(path)[name]
            where = f"{path.name} {name}"
            assert service.get("init") is True and service.get("read_only") is True, where
            assert service.get("security_opt") == ["no-new-privileges:true"] and service.get("cap_drop") == ["ALL"], where
        assert _services(path)["pgbouncer"]["tmpfs"] == ["/etc/pgbouncer:size=1m,uid=70,gid=70,mode=0700"], path.name


def test_the_app_reaches_its_two_databases_through_the_pooler_and_nothing_else_does():
    for path in (DEV, PROD):
        env = _doc(path)["x-app-env"]
        for database in ("appdata", "checkpointer"):
            url = env[f"{'APPDATA' if database == 'appdata' else 'CHECKPOINTER'}_DATABASE_URL"]
            assert re.search(rf"@pgbouncer:6432/{database}$", url), f"{path.name}: {url}"
        # The gateway and the exporters keep a direct connection: Prisma and the stats collectors are not made for a transaction pooler.
        assert "@postgres:5432/litellm" in _env(_services(path)["litellm"])["DATABASE_URL"], path.name
    assert "@pgbouncer:6432/appdata" in _env(_services(DEV)["mcp-gateway"])["APPDATA_DATABASE_URL"]
    assert "@postgres:5432/langfuse" in _env(_services(DEV)["langfuse-web"])["DATABASE_URL"]


def test_every_service_that_uses_the_pooled_address_waits_for_a_healthy_pooler():
    for path in (DEV, PROD):
        waiting = []
        for name, service in _services(path).items():
            if any("pgbouncer:6432" in str(value) for value in _env(service).values()) and name not in ("pgbouncer-exporter",):
                waiting.append(name)
                assert service["depends_on"]["pgbouncer"] == {"condition": "service_healthy"}, f"{path.name} {name}"
        assert len(waiting) >= 3, f"{path.name}: the scan found only {waiting}"
    assert {"api", "agent-worker", "ingest-worker"} <= set(waiting)


def test_the_pooler_serves_the_two_app_databases_in_transaction_mode_with_scram():
    for path in (DEV, PROD):
        env = _env(_services(path)["pgbouncer"])
        databases = {url.rsplit("/", 1)[1] for url in env["DATABASE_URLS"].split(",")}
        assert databases == {"appdata", "checkpointer"}, path.name
        assert env["POOL_MODE"] == "transaction" and env["AUTH_TYPE"] == "scram-sha-256", path.name
        assert "MAX_PREPARED_STATEMENTS" not in env, "the app does not prepare statements; the pooler's own default is a backstop, not a setting here"


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


def test_production_publishes_no_port_for_the_pooler_or_its_exporter_and_dev_keeps_the_pooler_on_loopback():
    for name in ("pgbouncer", "pgbouncer-exporter"):
        assert not _services(PROD)[name].get("ports"), f"production {name} must not publish a port"
    assert _services(DEV)["pgbouncer"]["ports"] == ["127.0.0.1:6432:6432"]


def test_no_database_password_is_written_into_the_production_pooler_or_exporter():
    for name, key in (("pgbouncer", "DATABASE_URLS"), ("pgbouncer-exporter", "PGBOUNCER_EXPORTER_CONNECTION_STRING")):
        value = _env(_services(PROD)[name])[key]
        assert "${POSTGRES_PASSWORD}" in value and "langfuse:langfuse@" not in value, name


def test_the_exporter_is_scraped_in_dev_and_in_production_under_one_job_name():
    dev = {j["job_name"]: j for j in _doc(REPO / "observability/prometheus/prometheus.yml")["scrape_configs"]}
    assert dev["pgbouncer"]["static_configs"][0]["targets"] == ["host.docker.internal:9127"]
    agent = _doc(REPO / "observability/otel-collector/config.agent.yaml")["receivers"]["prometheus"]["config"]["scrape_configs"]
    assert {"job_name": "pgbouncer", "scrape_interval": "15s", "static_configs": [{"targets": ["pgbouncer-exporter:9127"]}]} in agent


def test_the_pgbouncer_alerts_exist_and_name_only_series_the_exporter_emits():
    rules = {r["alert"]: r for g in _doc(REPO / "observability/prometheus/alerts.yml")["groups"] for r in g["rules"]}
    assert {"PgBouncerDown", "PgBouncerClientsWaiting", "PgBouncerMaxWait"} <= set(rules)
    for name in ("PgBouncerDown", "PgBouncerClientsWaiting", "PgBouncerMaxWait"):
        named = set(re.findall(r"\bpgbouncer_[a-z_]+", rules[name]["expr"]))
        assert named and named <= EXPORTER_METRICS, f"{name} names {named - EXPORTER_METRICS}, which the exporter does not emit"
    assert rules["PgBouncerMaxWait"]["labels"]["severity"] == "critical" and rules["PgBouncerClientsWaiting"]["labels"]["severity"] == "warning"
