"""The sandbox path with the REAL `opensandbox-mcp` bridge process and no backend: catalog, pins, one shared process.

Everything above the bridge is faked elsewhere and the bridge itself is exercised only by the manual `make test-sandbox` tier, which
needs a running opensandbox-server. But the bridge's tool catalog is static and answers without a backend, and a call to it with the
server unreachable fails fast with an error the client reports, so what this app does around the bridge can be checked for real here:
the five pinned tools load and are all `outward`, the other fourteen are never offered, and listing and calling share ONE process.
"""
from app.core import metrics
from app.domains import sandbox_session, sandbox_tools
from app.mcp.connection import close_all_connections
from tests.conftest import metric_value


async def test_the_real_bridge_yields_exactly_the_five_pinned_tools_all_outward_and_one_process_serves_a_call_too(monkeypatch):
    # A port nothing listens on: a call fails fast instead of reaching a developer's own sandbox server.
    monkeypatch.setattr(sandbox_tools, "OPENSANDBOX_MCP_DOMAIN", "127.0.0.1:1")
    monkeypatch.setattr(sandbox_tools, "OPENSANDBOX_API_KEY", "not-a-real-key")
    monkeypatch.setattr(sandbox_session, "_raw_sandbox_tools_cache", {})
    spawned_before = sum(metric_value(metrics.agent_mcp_connection_events_total, dependency="opensandbox_mcp", event=e)
                         for e in ("started", "restarted"))
    try:
        tools, capabilities = await sandbox_tools.load_sandbox_tools()

        assert {t.name for t in tools} == set(sandbox_tools.PINNED_SANDBOX_TOOLS)
        assert capabilities == dict.fromkeys(sandbox_tools.PINNED_SANDBOX_TOOLS, "outward")

        listing = next(t for t in tools if t.name == "sandbox_list")
        reply = await listing.ainvoke({"filter": {"metadata": {"k": "v"}, "states": ["RUNNING"]}})

        # Unreachable backend: reported as a tool error (a string the session turns into SandboxCallFailed), not raised.
        assert reply.startswith("Remote tool error:")
        assert "not-a-real-key" not in reply  # the key is never echoed back to the model

        # Listing and calling used ONE process: a second spawn would count as another "started"/"restarted".
        spawned = sum(metric_value(metrics.agent_mcp_connection_events_total, dependency="opensandbox_mcp", event=e)
                      for e in ("started", "restarted"))
        assert spawned - spawned_before == 1
    finally:
        await close_all_connections()
