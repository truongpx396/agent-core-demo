"""Tests for app/mcp/ops_server.py — same hermetic FastMCP in-process
call_tool/list_tools convention as tests/mcp/test_mcp_server.py.

app/mcp/ops_server.py imports `_fetch_metrics_summary_impl`/
`_list_recent_incidents_impl` by name (`from app.domains.ops.tools import
...`), so — unlike tests/mcp/test_mcp_server.py's `sql_store.query_employees`
(a module-qualified call one layer deeper inside `_query_employees_impl`
itself) — the right patch target here is `ops_server`'s OWN bound name,
not `app.domains.ops.tools`'s: patching the latter would leave
`ops_server`'s already-imported reference to the original function
untouched, a classic `from X import Y` monkeypatching pitfall.
"""

from app.mcp import ops_server


async def _call(name, arguments):
    """See tests/mcp/test_mcp_server.py's own docstring for why the string
    result lives in structured_result["result"] for this SDK version."""
    _, structured = await ops_server.mcp.call_tool(name, arguments)
    return structured["result"]


async def test_lists_both_ops_tools():
    tools = await ops_server.mcp.list_tools()
    names = {t.name for t in tools}
    assert names == {"fetch_metrics_summary", "list_recent_incidents"}


async def test_fetch_metrics_summary_refuses_without_principal():
    result = await _call("fetch_metrics_summary", {"principal": ""})
    assert "Refused" in result


async def test_fetch_metrics_summary_passes_through_when_principal_given(monkeypatch):
    async def fake_impl():
        return "all clear"

    monkeypatch.setattr(ops_server, "_fetch_metrics_summary_impl", fake_impl)
    result = await _call("fetch_metrics_summary", {"principal": "oncall-engineer"})
    assert result == "all clear"


async def test_list_recent_incidents_refuses_without_principal():
    result = await _call("list_recent_incidents", {"principal": ""})
    assert "Refused" in result


async def test_list_recent_incidents_passes_status_filter_through(monkeypatch):
    captured = {}

    async def fake_impl(status):
        captured["status"] = status
        return "no incidents"

    monkeypatch.setattr(ops_server, "_list_recent_incidents_impl", fake_impl)

    result = await _call("list_recent_incidents", {"principal": "oncall-engineer", "status": "open"})

    assert result == "no incidents"
    assert captured["status"] == "open"
