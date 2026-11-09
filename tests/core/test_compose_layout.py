"""References that cross the compose project boundary agree with each other.

The telemetry collectors (exporters, cAdvisor, OTel collector, Promtail) are an overlay on the app's project (`docker-compose.collectors.yml`);
the observability backends are a project of their own. A service moved between them keeps working only if what used to be an in-network name is
rewritten, and compose reports a dangling `depends_on` only when it is run. Nothing else adds these up.
"""
import re
from pathlib import Path

import pytest
import yaml

from app.adapters.inbound.workers.agent_worker import parse_worker_domains
from app.domains.registry import DOMAINS

REPO = Path(__file__).resolve().parents[2]
COMPOSE = REPO / "deploy" / "compose"
DEV, COLLECTORS, OBS = (COMPOSE / f"docker-compose{name}.yml" for name in ("", ".collectors", ".observability"))
PROD, OBS_PROD = COMPOSE / "docker-compose.prod.yml", COMPOSE / "docker-compose.observability.prod.yml"


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _services(*paths: Path) -> dict:
    merged: dict = {}
    for path in paths:
        merged |= _load(path)["services"]
    return merged


def test_the_makefile_starts_and_removes_exactly_the_collectors_file_services():
    listed = re.search(r"^COLLECTORS\s*:=\s*(.+)$", (REPO / "Makefile").read_text(encoding="utf-8"), re.MULTILINE)
    assert listed and set(listed.group(1).split()) == set(_services(COLLECTORS))


@pytest.mark.parametrize(
    "files",
    [(DEV,), (DEV, COLLECTORS), (OBS,), (PROD,), (OBS_PROD,)],
    ids=lambda files: "+".join(path.name for path in files),
)
def test_every_depends_on_names_a_service_in_the_same_project(files):
    services = _services(*files)
    for name, service in services.items():
        depends = service.get("depends_on") or []
        for target in depends if isinstance(depends, list) else depends.keys():
            assert target in services, f"{name} depends_on {target}, which is not in {'+'.join(p.name for p in files)}"


def test_prometheus_scrapes_only_what_its_own_project_or_the_host_publishes():
    """Prometheus is in the observability project: a collector is reached through the host port it publishes, not by service name."""
    obs_services = set(_services(OBS))
    config = yaml.safe_load((REPO / "observability" / "prometheus" / "prometheus.yml").read_text(encoding="utf-8"))
    hosts = {t.split(":")[0] for job in config["scrape_configs"] for sc in job["static_configs"] for t in sc["targets"]}
    hosts |= {t.split(":")[0] for am in config["alerting"]["alertmanagers"] for sc in am["static_configs"] for t in sc["targets"]}
    assert hosts <= obs_services | {"localhost", "host.docker.internal"}, f"scrape targets not reachable from {OBS.name}: {sorted(hosts - obs_services)}"


def test_promtail_pushes_to_loki_through_the_host_and_keeps_only_services_that_exist():
    config = yaml.safe_load((REPO / "observability" / "promtail" / "promtail-config.yaml").read_text(encoding="utf-8"))
    assert "loki" not in config["clients"][0]["url"].split("/")[2].split(":")[0], "Promtail is not in Loki's project"
    keep = next(r for r in config["scrape_configs"][0]["relabel_configs"] if r.get("action") == "keep")
    allowed = set(re.fullmatch(r"\^\((.+)\)\$", keep["regex"]).group(1).split("|"))
    unknown = allowed - set(_services(DEV, COLLECTORS))
    assert not unknown, f"promtail's allowlist names services no compose file defines: {sorted(unknown)}"


@pytest.mark.parametrize("path", [DEV, PROD], ids=lambda p: p.name)
def test_the_domains_a_compose_worker_serves_by_default_are_ones_the_registry_has(path):
    spec = _services(path)["agent-worker"]["environment"]["AGENT_WORKER_DOMAINS"]
    default = re.fullmatch(r"\$\{AGENT_WORKER_DOMAINS:-(.*)\}", spec).group(1)
    assert parse_worker_domains(default, sorted(DOMAINS))
