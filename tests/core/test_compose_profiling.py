"""The deployment contract of the profiler (specs/022): the Pyroscope server's pin, hardening and flags, who may reach it, and where the app
is told to push. Static, reading the YAML and the Terraform (no Docker); the server was brought up from these files and a hardened container
pushed a profile to it when this was written (specs/022 research.md). The pooler's half is `test_compose_pooler.py`.
"""
import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
COMPOSE = REPO / "deploy" / "compose"
DEV, PROD = COMPOSE / "docker-compose.yml", COMPOSE / "docker-compose.prod.yml"
OBS_DEV, OBS_PROD = COMPOSE / "docker-compose.observability.yml", COMPOSE / "docker-compose.observability.prod.yml"
PINNED = re.compile(r"^[\w./-]+:[\w.-]+@sha256:[0-9a-f]{64}$")


def _doc(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def _pyroscope(path: Path) -> dict:
    return _doc(path)["services"]["pyroscope"]


def test_the_server_is_pinned_by_digest_and_dev_runs_what_production_runs():
    for path in (OBS_DEV, OBS_PROD):
        assert PINNED.match(_pyroscope(path)["image"]), path.name
    assert _pyroscope(OBS_DEV)["image"] == _pyroscope(OBS_PROD)["image"]


def test_the_server_runs_hardened_with_a_volume_and_a_capped_tmpfs_as_its_only_write_paths():
    """Run read-only with no capabilities and no new privileges, its `docker diff` and log read: it writes /data (a declared volume) and
    nothing else (specs/022 research.md). /tmp is a small tmpfs in case a Go temp file is needed."""
    for path in (OBS_DEV, OBS_PROD):
        service = _pyroscope(path)
        assert service.get("init") is True and service.get("read_only") is True, path.name
        assert service.get("security_opt") == ["no-new-privileges:true"] and service.get("cap_drop") == ["ALL"], path.name
        assert "pyroscope:/data" in service["volumes"] and all(":size=" in t for t in service["tmpfs"]), path.name


def test_the_server_runs_in_the_measured_fast_start_mode_and_profiles_expire():
    for path in (OBS_DEV, OBS_PROD):
        command = _pyroscope(path)["command"]
        assert "-architecture.storage=v1" in command and "-write-path=ingester" in command, f"{path.name}: ready in 19 s, not about 3 minutes"
        assert any(c.startswith("-compactor.blocks-retention-period=") for c in command), f"{path.name}: profiles must expire"
        assert _pyroscope(path)["ports"] == ["4040:4040"]


def test_profiling_is_on_in_dev_and_off_in_production_until_tried_on_the_droplet():
    assert _doc(DEV)["x-app-env"]["PYROSCOPE_SERVER_ADDRESS"] == "${PYROSCOPE_SERVER_ADDRESS-http://host.docker.internal:4040}"
    assert _doc(PROD)["x-app-env"]["PYROSCOPE_SERVER_ADDRESS"] == "${PYROSCOPE_SERVER_ADDRESS:-}"
    assert "PYROSCOPE_SERVER_ADDRESS=\n" in (REPO / "deploy/env/prod.env.example").read_text()


def test_the_profile_port_is_open_to_the_vpc_only_in_the_cloud_firewall_and_in_ufw():
    main = (REPO / "infra/terraform/main.tf").read_text()
    rule = re.search(r'inbound_rule \{\s*protocol\s*=\s*"tcp"\s*port_range\s*=\s*"4040"\s*source_addresses\s*=\s*\[([^\]]*)\]', main)
    assert rule and rule.group(1).strip() == "digitalocean_vpc.this.ip_range", "4040 must be scoped to the VPC, like 4318 and 3100"
    assert re.search(r"ufw_tcp_ports\s*=\s*\[[^\]]*\b4040\b", main)


def test_grafana_provisions_the_datasource_at_the_servers_in_network_name():
    sources = {d["uid"]: d for d in _doc(REPO / "observability/grafana/provisioning/datasources/datasources.yml")["datasources"]}
    assert sources["pyroscope"]["type"] == "grafana-pyroscope-datasource" and sources["pyroscope"]["url"] == "http://pyroscope:4040"
