import pytest

from app.core.config import settings


@pytest.fixture
def launched_as(monkeypatch):
    """Start a published MCP server "as" a tenant and principal: what the `env` block of a client's server entry
    (or `.env` for `make mcp-serve`) sets.
    """

    def launch(tenant: str = "ecorp", principal: str = "launcher") -> None:
        monkeypatch.setattr(settings, "mcp_tenant", tenant)
        monkeypatch.setattr(settings, "mcp_principal", principal)

    return launch


@pytest.fixture(autouse=True)
def _no_identity_unless_a_test_launches_one(monkeypatch):
    """A developer's own `.env` may define MCP_TENANT/MCP_PRINCIPAL; no test here may depend on it."""
    monkeypatch.setattr(settings, "mcp_tenant", "")
    monkeypatch.setattr(settings, "mcp_principal", "")
