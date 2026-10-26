"""Tests for app/mcp/server.py — the MCP-exposed counterpart to
app/agent/tools/builtin.py's query_employees, reached over MCPServer's in-process
list_tools/call_tool. The same server over a real stdio subprocess is in
tests/mcp/test_published_servers_over_stdio.py. These stay hermetic by mocking
app.core.sql_store.query_employees, same boundary test_tools.py's
TestQueryEmployees mocks at.

The identity is the LAUNCHER's (`launched_as`, tests/mcp/conftest.py), never a tool
argument: the connected assistant would be the one filling an argument in, so a
tenant there is a tenant a prompt injection can name.

`call_tool`/`list_tools` are async, so every test here is `async def` too
(pytest-asyncio's `asyncio_mode = "auto"`, pyproject.toml — no
`@pytest.mark.asyncio` needed on each one).
"""
import logging

from app.core import sql_store
from app.mcp import server as mcp_server


async def _call(name, arguments=None):
    """MCPServer.call_tool returns a CallToolResult; the tools return a string, which the SDK
    wraps as structured content {"result": <string>} (verified against mcp 2.3.0)."""
    result = await mcp_server.mcp.call_tool(name, arguments or {})
    return result.structured_content["result"]


async def test_lists_query_employees_tool():
    tools = await mcp_server.mcp.list_tools()
    assert "query_employees" in {t.name for t in tools}


async def test_no_tool_takes_a_tenant_or_a_principal_as_an_argument():
    """Regression: these used to be arguments, so the assistant connected to the server chose which tenant's employees it
    read. Identity is bound when the server is launched; nothing the model can fill in names it."""
    for tool in await mcp_server.mcp.list_tools():
        assert set(tool.input_schema["properties"]) <= {"department", "name_contains"}, tool.name


async def test_a_tenant_the_caller_tries_to_name_is_ignored_and_the_launchers_is_queried(launched_as, monkeypatch):
    launched_as(tenant="ecorp")
    seen = []

    async def fake_query_employees(tenant, department=None, name_contains=None, limit=None):
        seen.append(tenant)
        return []

    monkeypatch.setattr(sql_store, "query_employees", fake_query_employees)

    # An older client, or an injected one, still sends the argument the schema no longer has.
    await _call("query_employees", {"tenant": "other-co", "principal": "someone-else"})

    assert seen == ["ecorp"]


async def test_refuses_when_started_without_an_identity():
    result = await _call("query_employees")
    assert "Refused" in result


async def test_refuses_when_only_half_an_identity_was_set(launched_as):
    launched_as(tenant="ecorp", principal="")
    assert "Refused" in await _call("query_employees")


async def test_invalid_department_returns_a_friendly_error(launched_as, monkeypatch):
    launched_as()
    called = []

    async def fake_query_employees(**kw):
        called.append(kw)
        return []

    monkeypatch.setattr(sql_store, "query_employees", fake_query_employees)

    result = await _call("query_employees", {"department": "NotADept"})

    assert "Invalid department" in result
    assert "Engineering" in result  # lists the valid values
    assert called == []  # never reached sql_store with a bad filter


async def test_valid_department_passes_through_as_the_enum_value(launched_as, monkeypatch):
    launched_as(tenant="ecorp")
    captured = {}

    async def fake_query_employees(tenant, department=None, name_contains=None, limit=None):
        captured["tenant"] = tenant
        captured["department"] = department
        return []

    monkeypatch.setattr(sql_store, "query_employees", fake_query_employees)

    await _call("query_employees", {"department": "Engineering"})

    assert captured["tenant"] == "ecorp"
    assert captured["department"] == "Engineering"


async def test_two_servers_launched_for_different_tenants_query_different_tenants(launched_as, monkeypatch):
    seen = []

    async def fake_query_employees(tenant, department=None, name_contains=None, limit=None):
        seen.append(tenant)
        return []

    monkeypatch.setattr(sql_store, "query_employees", fake_query_employees)

    launched_as(tenant="ecorp")
    await _call("query_employees")
    launched_as(tenant="other-co")
    await _call("query_employees")

    assert seen == ["ecorp", "other-co"]


async def test_the_tool_declares_itself_read_only_and_closed_world():
    (tool,) = [t for t in await mcp_server.mcp.list_tools() if t.name == "query_employees"]

    assert tool.annotations.read_only_hint is True
    assert tool.annotations.destructive_hint is False
    assert tool.annotations.open_world_hint is False
    assert tool.title


async def test_every_call_leaves_an_audit_line_naming_who_but_never_what_was_asked(launched_as, monkeypatch, caplog):
    launched_as(tenant="ecorp", principal="alice")

    async def fake_query_employees(**kw):
        return []

    monkeypatch.setattr(sql_store, "query_employees", fake_query_employees)

    with caplog.at_level(logging.INFO, logger="app.mcp.audit"):
        await _call("query_employees", {"name_contains": "Margaret Hamilton"})
        await _call("query_employees", {"department": "NotADept"})

    ok, invalid = [r for r in caplog.records if r.message == "mcp_tool_call"]
    assert (ok.server, ok.tool, ok.outcome, ok.tenant, ok.principal) == (
        "ecorp-structured-data", "query_employees", "ok", "ecorp", "alice",
    )
    assert invalid.outcome == "invalid"
    assert ok.duration_ms >= 0
    logged = " ".join(str(v) for r in caplog.records for v in vars(r).values())
    assert "Margaret Hamilton" not in logged


async def test_a_refusal_is_audited_too(caplog):
    with caplog.at_level(logging.INFO, logger="app.mcp.audit"):
        await _call("query_employees")

    (record,) = [r for r in caplog.records if r.message == "mcp_tool_call"]
    assert record.outcome == "refused"
    assert record.tenant is None


def test_main_refuses_to_serve_without_an_identity(monkeypatch):
    import pytest

    served = []
    monkeypatch.setattr(mcp_server, "configure_logging", lambda: None)  # force=True would drop pytest's own log handler
    monkeypatch.setattr(mcp_server.mcp, "run", lambda **kw: served.append(kw))

    with pytest.raises(SystemExit) as exc:
        mcp_server.main()

    assert "MCP_TENANT" in str(exc.value)
    assert served == []


def test_main_serves_over_stdio_once_launched_with_an_identity(launched_as, monkeypatch):
    served = []
    launched_as()
    monkeypatch.setattr(mcp_server, "configure_logging", lambda: None)
    monkeypatch.setattr(mcp_server.mcp, "run", lambda **kw: served.append(kw))

    mcp_server.main()

    assert served == [{"transport": "stdio"}]
