"""The MCP gateway stack in docker-compose.yml (profile `gateway`, specs/017): Casdoor, its Postgres, the gateway and a Caddy.

Static, reading the YAML and the Caddyfile (no Docker). Each test pins something that either went wrong when this was built, or that the
design rests on and an edit could silently undo: the first one is a mistake that would have broken `make up` for everyone.
"""
import json
import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
COMPOSE = REPO / "deploy" / "compose" / "docker-compose.yml"
CADDYFILE = REPO / "deploy" / "caddy" / "Caddyfile.gateway"
GATEWAY_SERVICES = ["casdoor-db", "casdoor", "mcp-gateway", "caddy-gateway"]
DIGEST = re.compile(r"@sha256:[0-9a-f]{64}$")


def _document() -> dict:
    return yaml.safe_load(COMPOSE.read_text())


def _services() -> dict:
    return _document()["services"]


def test_the_dev_compose_file_requires_no_variable_to_be_set():
    """Found by running the stack: the first version wrote `${CASDOOR_DB_PASSWORD:?run make mcp-gateway-up}`. Compose interpolates the WHOLE
    file on every command, profile or not, so that made `docker compose config` (and `make up`, and every other target) fail with 'required
    variable is missing a value' for everyone who never starts this stack. A password defaults to empty instead, which fails loudly at start:
    Postgres will not initialise without one. (Comments are not values; this reads the parsed YAML.)"""
    text = json.dumps(_document())

    assert not re.search(r"\$\{[A-Za-z_][A-Za-z0-9_]*:\?", text), "a ${VAR:?...} anywhere in this file breaks every compose command"


def test_the_gateway_stack_is_opt_in_so_make_up_never_starts_it():
    services = _services()

    for name in GATEWAY_SERVICES:
        assert services[name]["profiles"] == ["gateway"], name


def test_the_third_party_images_are_pinned_by_digest_with_the_tag_kept_for_the_reader():
    services = _services()

    for name in ("casdoor-db", "casdoor", "caddy-gateway"):
        image = services[name]["image"]
        assert DIGEST.search(image), f"{name}: {image!r} must be pinned by digest (Casdoor in particular ships a release almost every day)"
        assert ":" in image.split("@")[0].rsplit("/", 1)[-1], f"{name}: keep the tag beside the digest so a reader can see the version"


def test_only_the_proxy_is_reachable_from_the_host_and_only_on_loopback():
    services = _services()

    for name in ("casdoor-db", "casdoor", "mcp-gateway"):
        assert not services[name].get("ports"), f"{name} publishes a port: it must be reached through caddy-gateway or the network only"
    ports = services["caddy-gateway"]["ports"]
    assert ports and all(str(p).startswith("127.0.0.1:") for p in ports), f"the proxy must bind loopback only (got {ports})"


def test_the_gateway_is_the_hardened_image_this_repo_builds_and_stateless_replicas_are_possible():
    gateway = _services()["mcp-gateway"]

    assert gateway["build"]["target"] == "runtime"  # the stage that ships, not the builder that has uv in it
    assert gateway.get("init") is True and gateway.get("read_only") is True
    assert gateway.get("cap_drop") == ["ALL"] and gateway.get("security_opt") == ["no-new-privileges:true"]
    assert "container_name" not in gateway  # a fixed name would make `--scale mcp-gateway=N` impossible
    assert gateway["command"][:3] == ["python", "-m", "app.mcp.gateway"]


def test_the_gateway_container_holds_only_what_the_two_published_tools_read():
    """It is reachable by anyone holding a token: it gets the app database, Prometheus, its own settings and the telemetry endpoint, and no
    model key, no Redis, no sandbox key, none of the rest of the app's environment."""
    environment = _services()["mcp-gateway"]["environment"]

    allowed = {"APPDATA_DATABASE_URL", "PROMETHEUS_URL", "OTEL_EXPORTER_OTLP_ENDPOINT"}
    unexpected = {k for k in environment if k not in allowed and not k.startswith("MCP_GATEWAY_")}
    assert not unexpected, f"mcp-gateway was given {sorted(unexpected)}"


def test_the_gateway_never_mounts_the_container_runtime_socket():
    for name in GATEWAY_SERVICES:
        volumes = " ".join(str(v) for v in _services()[name].get("volumes", []))
        assert "docker.sock" not in volumes, name


def test_no_secret_is_written_into_the_file():
    casdoor = _services()["casdoor"]["environment"]
    db = _services()["casdoor-db"]["environment"]

    for value in (casdoor["initAdminPassword"], db["POSTGRES_PASSWORD"], casdoor["dataSourceName"]):
        assert re.search(r"\$\{CASDOOR_(ADMIN|DB)_PASSWORD", value), f"a password must be an interpolation from the generated secrets: {value!r}"


def test_casdoors_origin_is_exactly_the_issuer_the_gateway_expects():
    """Measured: with `origin` unset Casdoor derived the issuer from the request's Host header, so a token's `iss` depended on how the
    caller reached it. The gateway compares `iss` for equality, so the two settings must be the same value, default included."""
    casdoor_origin = _services()["casdoor"]["environment"]["origin"]
    gateway_issuer = _services()["mcp-gateway"]["environment"]["MCP_GATEWAY_ISSUER"]

    assert casdoor_origin == gateway_issuer == "${MCP_GATEWAY_ISSUER:-https://auth.localhost:8443}"


def test_the_signing_keys_are_fetched_by_the_in_network_name_not_through_the_proxy():
    jwks = _services()["mcp-gateway"]["environment"]["MCP_GATEWAY_JWKS_URL"]

    assert jwks.startswith("http://casdoor:8000/")


def test_the_gateway_waits_for_what_it_needs_and_the_proxy_waits_for_the_gateway():
    services = _services()

    assert services["mcp-gateway"]["depends_on"]["casdoor"]["condition"] == "service_healthy"
    assert services["mcp-gateway"]["depends_on"]["postgres"]["condition"] == "service_healthy"
    assert services["caddy-gateway"]["depends_on"]["mcp-gateway"]["condition"] == "service_healthy"
    assert services["casdoor"]["depends_on"]["casdoor-db"]["condition"] == "service_healthy"


def test_the_caddyfile_routes_only_the_mcp_paths_and_keeps_health_internal():
    caddyfile = "\n".join(line for line in CADDYFILE.read_text().splitlines() if not line.lstrip().startswith("#"))  # directives only
    # specs/018 split the matcher in two: the MCP routes ask the authlimit service first, the discovery documents (a client with no token
    # yet must be able to find where to get one) do not. Together they are still exactly the four paths of spec 017.
    mcp = re.search(r"@mcp path (.+)", caddyfile).group(1)
    metadata = re.search(r"@mcp_metadata path (.+)", caddyfile).group(1)

    assert set(mcp.split()) == {"/ecorp/mcp", "/ops/mcp"}
    assert set(metadata.split()) == {"/.well-known/oauth-protected-resource/ecorp/mcp", "/.well-known/oauth-protected-resource/ops/mcp"}
    assert "/healthz" not in mcp + metadata and "/readyz" not in mcp + metadata
    assert "dynamic a" in caddyfile and "name mcp-gateway" in caddyfile  # replicas are found by DNS, so `--scale` load-balances
    assert caddyfile.count("tls internal") == 3  # the MCP, API and authorization-server sites; a deployment removes these lines and nothing else
    assert "health_uri /readyz" in caddyfile
    # Measured: stopping a replica under steady traffic failed 2 of 621 calls (the window before Caddy drops the dead upstream); with
    # connect retries on, killing one outright failed 0 of 615.
    assert "lb_try_duration" in caddyfile
