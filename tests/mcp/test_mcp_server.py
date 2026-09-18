"""Tests for app/mcp/server.py — the MCP-exposed counterpart to
app/agent/tools.py's query_employees, reached over FastMCP's in-process
list_tools/call_tool rather than a real stdio subprocess (verified once,
empirically, against a real running Postgres before this file was written;
these stay hermetic like the rest of the suite by mocking
app.agent.sql_store.query_employees, same boundary test_tools.py's
TestQueryEmployees mocks at).

`call_tool`/`list_tools` are async, so every test here is `async def` too
(pytest-asyncio's `asyncio_mode = "auto"`, pyproject.toml — no
`@pytest.mark.asyncio` needed on each one).
"""

from app.agent import sql_store
from app.mcp import server as mcp_server


async def _call(name, arguments):
    """FastMCP.call_tool returns (content_blocks, structured_result) in
    this SDK version — verified empirically; tests only care about the
    string result, which lives in structured_result["result"]."""
    _, structured = await mcp_server.mcp.call_tool(name, arguments)
    return structured["result"]


async def test_lists_query_employees_tool():
    tools = await mcp_server.mcp.list_tools()
    assert "query_employees" in {t.name for t in tools}


async def test_refuses_without_tenant_or_principal():
    result = await _call("query_employees", {"tenant": "", "principal": ""})
    assert "Refused" in result


async def test_invalid_department_returns_a_friendly_error(monkeypatch):
    called = []

    async def fake_query_employees(**kw):
        called.append(kw)
        return []

    monkeypatch.setattr(sql_store, "query_employees", fake_query_employees)

    result = await _call(
        "query_employees",
        {"tenant": "ecorp", "principal": "p", "department": "NotADept"},
    )

    assert "Invalid department" in result
    assert "Engineering" in result  # lists the valid values
    assert called == []  # never reached sql_store with a bad filter


async def test_valid_department_passes_through_as_the_enum_value(monkeypatch):
    captured = {}

    async def fake_query_employees(tenant, department=None, name_contains=None, limit=None):
        captured["tenant"] = tenant
        captured["department"] = department
        return []

    monkeypatch.setattr(sql_store, "query_employees", fake_query_employees)

    await _call(
        "query_employees",
        {"tenant": "ecorp", "principal": "p", "department": "Engineering"},
    )

    assert captured["tenant"] == "ecorp"
    assert captured["department"] == "Engineering"


async def test_two_different_tenants_get_different_tenant_param(monkeypatch):
    seen = []

    async def fake_query_employees(tenant, department=None, name_contains=None, limit=None):
        seen.append(tenant)
        return []

    monkeypatch.setattr(sql_store, "query_employees", fake_query_employees)

    await _call("query_employees", {"tenant": "ecorp", "principal": "p"})
    await _call("query_employees", {"tenant": "other-co", "principal": "p"})

    assert seen[0] != seen[1]
