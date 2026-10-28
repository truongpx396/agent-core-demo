"""The gateway refuses to start unless it can safely serve someone (app/mcp/gateway.py::GatewayConfig).

A network service that treats a missing issuer as "no check" would be an open door; that is the failure these pin. Plain http is
accepted only where it cannot cross a network: a loopback public URL or issuer, and an in-network (dotless) name for the key fetch.
"""
import re
from pathlib import Path

import pytest

from app.core.config import settings
from app.mcp import gateway
from app.mcp.gateway import GatewayConfig, GatewayConfigError

GOOD = {
    "mcp_gateway_public_url": "https://mcp.example.test",
    "mcp_gateway_issuer": "https://auth.example.test",
    "mcp_gateway_jwks_url": "http://casdoor:8000/.well-known/jwks",
    "mcp_gateway_tenant_claim": "owner",
    "mcp_gateway_principal_claim": "sub",
    "mcp_gateway_ops_tenants": "ops-team",
    "mcp_gateway_jwks_cache_seconds": 300,
}


def _config(**overrides) -> GatewayConfig:
    return GatewayConfig.from_settings(settings.model_copy(update={**GOOD, **overrides}))


def test_a_complete_configuration_is_accepted_and_trailing_slashes_are_dropped():
    cfg = _config(mcp_gateway_public_url="https://mcp.example.test/", mcp_gateway_issuer="https://auth.example.test/")

    assert (cfg.public_url, cfg.issuer) == ("https://mcp.example.test", "https://auth.example.test")
    assert (cfg.tenant_claim, cfg.principal_claim) == ("owner", "sub")


@pytest.mark.parametrize(
    "overrides, mentions",
    [
        ({"mcp_gateway_issuer": ""}, "MCP_GATEWAY_ISSUER"),
        ({"mcp_gateway_issuer": "  "}, "MCP_GATEWAY_ISSUER"),
        ({"mcp_gateway_jwks_url": ""}, "MCP_GATEWAY_JWKS_URL"),
        ({"mcp_gateway_public_url": ""}, "MCP_GATEWAY_PUBLIC_URL"),
        ({"mcp_gateway_public_url": "mcp.example.test"}, "MCP_GATEWAY_PUBLIC_URL"),  # no scheme
        ({"mcp_gateway_public_url": "https://mcp.example.test/ecorp"}, "MCP_GATEWAY_PUBLIC_URL"),  # a path: the endpoints add theirs
        ({"mcp_gateway_public_url": "https://mcp.example.test?x=1"}, "MCP_GATEWAY_PUBLIC_URL"),
        ({"mcp_gateway_public_url": "ftp://mcp.example.test"}, "MCP_GATEWAY_PUBLIC_URL"),
        ({"mcp_gateway_public_url": "http://mcp.example.test"}, "MCP_GATEWAY_PUBLIC_URL"),  # plain http on a real name
        ({"mcp_gateway_issuer": "http://auth.example.test"}, "MCP_GATEWAY_ISSUER"),
        ({"mcp_gateway_jwks_url": "http://auth.example.test/jwks"}, "MCP_GATEWAY_JWKS_URL"),  # plain http across a network
        ({"mcp_gateway_tenant_claim": ""}, "TENANT_CLAIM"),
        ({"mcp_gateway_principal_claim": " "}, "PRINCIPAL_CLAIM"),
    ],
)
def test_a_configuration_that_could_not_serve_safely_is_refused_and_names_the_setting(overrides, mentions):
    with pytest.raises(GatewayConfigError, match=mentions):
        _config(**overrides)


@pytest.mark.parametrize(
    "overrides",
    [
        {"mcp_gateway_public_url": "http://127.0.0.1:9000"},
        {"mcp_gateway_public_url": "http://localhost:9000"},
        {"mcp_gateway_public_url": "http://mcp.localhost:8443"},
        {"mcp_gateway_issuer": "http://localhost:18000"},
        {"mcp_gateway_jwks_url": "http://127.0.0.1:18000/.well-known/jwks"},
        {"mcp_gateway_jwks_url": "https://auth.example.test/.well-known/jwks"},
    ],
)
def test_plain_http_is_accepted_only_where_it_cannot_cross_a_network(overrides):
    _config(**overrides)


def test_operator_tenants_are_a_trimmed_comma_list_and_empty_means_nobody():
    assert _config(mcp_gateway_ops_tenants=" ops-team , platform,, ").ops_tenants == frozenset({"ops-team", "platform"})
    assert _config(mcp_gateway_ops_tenants="").ops_tenants == frozenset()


def test_starting_without_an_issuer_exits_with_the_reason(monkeypatch):
    monkeypatch.setattr(gateway, "configure_logging", lambda: None)  # force=True would drop pytest's own log handler
    monkeypatch.setattr(gateway, "configure_telemetry", lambda _name: None)
    monkeypatch.setattr(gateway.app_settings, "mcp_gateway_issuer", "")

    with pytest.raises(SystemExit) as stopped:
        gateway.main(["--port", "0"])

    assert "MCP_GATEWAY_ISSUER" in str(stopped.value)


def test_the_gateway_never_reads_the_launchers_identity():
    """A text check on purpose: the failure to guard is a future edit that falls back to MCP_TENANT/MCP_PRINCIPAL 'when there is no
    token', which would turn a network service into one that acts for whoever started it."""
    root = Path(__file__).resolve().parents[2] / "app" / "mcp"

    for module in ("gateway.py", "gateway_auth.py"):
        code = re.sub(r'""".*?"""', "", (root / module).read_text(), flags=re.DOTALL)  # prose may name them; code may not
        code = "\n".join(line.split("#", 1)[0] for line in code.splitlines())
        for forbidden in ("mcp_tenant", "mcp_principal", "launcher_ctx", "launcher_source"):
            assert forbidden not in code, f"{module} must not use {forbidden}: the gateway's identity is the token's"
