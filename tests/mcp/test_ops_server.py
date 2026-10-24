"""Tests for app/mcp/ops_server.py — same hermetic MCPServer in-process
call_tool/list_tools convention as tests/mcp/test_mcp_server.py, including its
reason for binding the identity at launch (`launched_as`) instead of taking it
as an argument.

app/mcp/ops_server.py imports `fetch_metrics_summary_impl`/
`list_recent_incidents_impl` by name (`from app.domains.ops.tools import
...`), so — unlike tests/mcp/test_mcp_server.py's `sql_store.query_employees`
(a module-qualified call one layer deeper inside `query_employees_impl`
itself) — the right patch target here is `ops_server`'s OWN bound name,
not `app.domains.ops.tools`'s: patching the latter would leave
`ops_server`'s already-imported reference to the original function
untouched, a classic `from X import Y` monkeypatching pitfall.
"""
import logging

import pytest

from app.core.config import DEFAULT_TENANT
from app.mcp import ops_server


async def _call(name, arguments=None):
    """See tests/mcp/test_mcp_server.py's own docstring for why the string result lives in
    structured_content["result"]."""
    result = await ops_server.mcp.call_tool(name, arguments or {})
    return result.structured_content["result"]


async def test_lists_both_ops_tools():
    tools = await ops_server.mcp.list_tools()
    names = {t.name for t in tools}
    assert names == {"fetch_metrics_summary", "list_recent_incidents"}


async def test_no_tool_takes_a_principal_or_a_tenant_as_an_argument():
    for tool in await ops_server.mcp.list_tools():
        assert set(tool.input_schema["properties"]) <= {"status"}, tool.name


async def test_every_tool_declares_itself_read_only_and_closed_world():
    for tool in await ops_server.mcp.list_tools():
        assert tool.annotations.read_only_hint is True, tool.name
        assert tool.annotations.open_world_hint is False, tool.name
        assert tool.title, tool.name


async def test_fetch_metrics_summary_refuses_when_started_without_a_principal():
    result = await _call("fetch_metrics_summary")
    assert "Refused" in result


async def test_fetch_metrics_summary_passes_through_when_launched_with_a_principal(launched_as, monkeypatch):
    launched_as(principal="oncall-engineer")

    async def fake_impl():
        return "all clear"

    monkeypatch.setattr(ops_server, "fetch_metrics_summary_impl", fake_impl)
    result = await _call("fetch_metrics_summary")
    assert result == "all clear"


async def test_the_ops_server_needs_a_principal_but_not_a_tenant(launched_as, monkeypatch):
    """Ops data is the deployment's, not a tenant's: MCP_TENANT may be unset and the server still serves."""
    launched_as(tenant="", principal="oncall-engineer")

    async def fake_impl():
        return "all clear"

    monkeypatch.setattr(ops_server, "fetch_metrics_summary_impl", fake_impl)

    assert await _call("fetch_metrics_summary") == "all clear"


async def test_list_recent_incidents_refuses_when_started_without_a_principal():
    result = await _call("list_recent_incidents")
    assert "Refused" in result


async def test_list_recent_incidents_passes_status_filter_through(launched_as, monkeypatch):
    launched_as(principal="oncall-engineer")
    captured = {}

    async def fake_impl(status):
        captured["status"] = status
        return "no incidents"

    monkeypatch.setattr(ops_server, "list_recent_incidents_impl", fake_impl)

    result = await _call("list_recent_incidents", {"status": "open"})

    assert result == "no incidents"
    assert captured["status"] == "open"


async def test_calls_are_audited_under_the_default_tenant(launched_as, monkeypatch, caplog):
    launched_as(tenant="", principal="oncall-engineer")

    async def fake_impl(status):
        return "no incidents"

    monkeypatch.setattr(ops_server, "list_recent_incidents_impl", fake_impl)

    with caplog.at_level(logging.INFO, logger="app.mcp.audit"):
        await _call("list_recent_incidents", {"status": "open"})

    (record,) = [r for r in caplog.records if r.message == "mcp_tool_call"]
    assert (record.server, record.tool, record.outcome) == ("ecorp-ops", "list_recent_incidents", "ok")
    assert (record.tenant, record.principal) == (DEFAULT_TENANT, "oncall-engineer")


def test_main_refuses_to_serve_without_a_principal(monkeypatch):
    served = []
    monkeypatch.setattr(ops_server, "configure_logging", lambda: None)
    monkeypatch.setattr(ops_server.mcp, "run", lambda **kw: served.append(kw))

    with pytest.raises(SystemExit) as exc:
        ops_server.main()

    assert "MCP_PRINCIPAL" in str(exc.value)
    assert served == []
