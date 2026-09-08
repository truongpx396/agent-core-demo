"""Tests for app/domains/sandbox_tools.py — mocks
`app.mcp.client.load_remote_tools` (the existing, unmodified pattern-28
client this module builds on, same boundary tests/mcp/test_mcp_client.py
already mocks one level deeper at `_list_remote_tools`/`_call_remote_tool`)
so these stay hermetic, no live `opensandbox-mcp`/`opensandbox-server`
needed. The graceful-degrade path this file exists to prove is the one
piece of this module's own logic that isn't already covered by
tests/domains/ops/test_domain.py's live-environment-dependent assertions
(see that file's own comment on why it can't hardcode an exact sandbox
tool set).
"""
import sys

from app.domains import sandbox_tools
from app.mcp import client as mcp_client


def test_passes_the_configured_domain_protocol_and_api_key_to_the_bridge(monkeypatch):
    captured = {}

    def fake_load_remote_tools(**kwargs):
        captured.update(kwargs)
        return [], {}

    monkeypatch.setattr(mcp_client, "load_remote_tools", fake_load_remote_tools)

    sandbox_tools.load_sandbox_tools()

    # sys.executable + scripts/opensandbox_mcp_bridge.py, NOT the packaged
    # `opensandbox-mcp` binary directly — see sandbox_tools.py's own
    # `_BRIDGE_SCRIPT` comment for why (opensandbox-mcp==0.1.1's CLI has no
    # way to set ConnectionConfig(use_server_proxy=True), which this app's
    # containerized opensandbox-server deployment needs).
    assert captured["command"] == sys.executable
    assert captured["args"] == [
        sandbox_tools._BRIDGE_SCRIPT,
        "--domain", sandbox_tools.OPENSANDBOX_MCP_DOMAIN, "--protocol", "http",
        "--api-key", sandbox_tools.OPENSANDBOX_API_KEY,
    ]


def test_never_supplies_capability_overrides_so_every_tool_defaults_to_outward(monkeypatch):
    """No override means load_remote_tools's own fail-closed default
    applies (app/mcp/client.py: unlisted -> "outward") — this module must
    never narrow that on OpenSandbox's behalf."""
    captured = {}

    def fake_load_remote_tools(**kwargs):
        captured.update(kwargs)
        return [], {}

    monkeypatch.setattr(mcp_client, "load_remote_tools", fake_load_remote_tools)

    sandbox_tools.load_sandbox_tools()

    assert captured["capability_overrides"] == {}


def test_returns_the_real_tools_and_capabilities_on_success(monkeypatch):
    fake_tools = ["sandbox_create", "command_run"]
    fake_caps = {"sandbox_create": "outward", "command_run": "outward"}
    monkeypatch.setattr(
        mcp_client, "load_remote_tools", lambda **kwargs: (fake_tools, fake_caps)
    )

    tools, caps = sandbox_tools.load_sandbox_tools()

    assert tools == fake_tools
    assert caps == fake_caps


def test_degrades_to_empty_when_the_bridge_is_not_installed(monkeypatch):
    def raise_not_found(**kwargs):
        raise FileNotFoundError("opensandbox-mcp not found on PATH")

    monkeypatch.setattr(mcp_client, "load_remote_tools", raise_not_found)

    tools, caps = sandbox_tools.load_sandbox_tools()

    assert (tools, caps) == ([], {})


def test_degrades_to_empty_on_any_other_connection_failure(monkeypatch):
    def raise_connection_error(**kwargs):
        raise ConnectionRefusedError("could not reach the sandbox server")

    monkeypatch.setattr(mcp_client, "load_remote_tools", raise_connection_error)

    tools, caps = sandbox_tools.load_sandbox_tools()

    assert (tools, caps) == ([], {})


def test_degraded_result_never_raises_even_when_logging(monkeypatch, caplog):
    monkeypatch.setattr(
        mcp_client, "load_remote_tools", lambda **kwargs: (_ for _ in ()).throw(RuntimeError("boom"))
    )

    tools, caps = sandbox_tools.load_sandbox_tools()

    assert (tools, caps) == ([], {})
