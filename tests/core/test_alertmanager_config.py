"""The production Alertmanager must be able to reach someone (spec 008 A7).

`observability/alertmanager/alertmanager.yml` is the local/demo file: its one receiver has
no notification block, so alerts show in the UI and go nowhere. The production compose used
to mount that same file, and nothing in it said it was unfinished — every alert rule in
this repo was, in production, a rule nobody would ever hear about.

These checks are structural and need no Docker or Alertmanager (CI's `alertmanager-config`
job runs `amtool check-config` on the real image):
  * the production compose mounts the PRODUCTION config, never the demo one;
  * every receiver the prod routing tree can pick actually notifies somewhere;
  * every secret FILE that config reads is one the compose command writes, and the compose
    refuses to start without the variable that feeds it — an observability stack that cannot
    reach anyone must fail the deploy, not come up looking healthy;
  * critical alerts are re-sent at least as often as the default.
"""
import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
PROD_COMPOSE = REPO / "deploy/compose/docker-compose.observability.prod.yml"
PROD_CONFIG = REPO / "observability/alertmanager/alertmanager.prod.yml"
DEMO_CONFIG = "observability/alertmanager/alertmanager.yml"
VARIABLE = "ALERTMANAGER_SLACK_WEBHOOK_URL"


def _service() -> dict:
    return yaml.safe_load(PROD_COMPOSE.read_text())["services"]["alertmanager"]


def _config() -> dict:
    return yaml.safe_load(PROD_CONFIG.read_text())


def _routes(route: dict):
    yield route
    for child in route.get("routes", []):
        yield from _routes(child)


def _seconds(duration: str) -> int:
    units = {"s": 1, "m": 60, "h": 3600}
    return sum(int(n) * units[u] for n, u in re.findall(r"(\d+)([smh])", duration))


def test_the_production_compose_mounts_the_production_config_not_the_demo_one():
    mounts = [v for v in _service()["volumes"] if v.endswith(":/etc/alertmanager/alertmanager.yml:ro")]

    assert len(mounts) == 1
    assert mounts[0].startswith("./observability/alertmanager/alertmanager.prod.yml:")
    assert DEMO_CONFIG not in PROD_COMPOSE.read_text().replace("alertmanager.prod.yml", "")


def test_every_receiver_the_production_routing_tree_can_pick_actually_notifies_somewhere():
    config = _config()
    receivers = {r["name"]: r for r in config["receivers"]}

    for route in _routes(config["route"]):
        name = route["receiver"]
        assert name in receivers, f"route points at an undefined receiver {name!r}"
        notifying = [key for key in receivers[name] if key.endswith("_configs") and receivers[name][key]]
        assert notifying, f"receiver {name!r} has no notification block: its alerts are delivered to no one"


def test_every_secret_file_the_config_reads_is_written_by_the_compose_command():
    text = PROD_CONFIG.read_text()
    files = re.findall(r"\b\w+_file:\s*(\S+)", text)
    command = " ".join(_service()["command"])

    assert files, "test setup: the production config reads no secret file, so the check below proved nothing"
    for path in files:
        assert f"> {path}" in command, f"the config reads {path} but the compose command never writes it"


def test_the_production_compose_refuses_to_start_without_the_receiver_secret():
    environment = _service()["environment"]

    assert VARIABLE in environment
    assert re.search(rf"\$\{{{VARIABLE}:\?", PROD_COMPOSE.read_text()), (
        "the variable must use ${VAR:?message}: an unset webhook has to fail `docker compose up`, "
        "not start an Alertmanager that delivers nothing"
    )


def test_the_variable_is_documented_in_the_observability_env_template():
    template = (REPO / "deploy/env/observability.prod.env.example").read_text()

    assert re.search(rf"^{VARIABLE}=", template, re.MULTILINE)


def test_critical_alerts_are_resent_at_least_as_often_as_the_default():
    route = _config()["route"]
    critical = [r for r in route["routes"] if any("critical" in m for m in r.get("matchers", []))]

    assert critical, "no route for severity=critical"
    assert _seconds(critical[0]["repeat_interval"]) <= _seconds(route["repeat_interval"])
