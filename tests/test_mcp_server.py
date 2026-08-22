"""Tests for app/mcp_server.py — the MCP-exposed counterpart to
app/tools.py's query_employees, reached over FastMCP's in-process
list_tools/call_tool rather than a real stdio subprocess (verified once,
empirically, against a real running Postgres before this file was written;
these stay hermetic like the rest of the suite by mocking
app.sql_store.query_employees, same boundary test_tools.py's
TestQueryEmployees mocks at).

No pytest-asyncio in this project (see tests/test_durable_checkpoint.py) —
`call_tool`/`list_tools` are async, so each test wraps its call in
asyncio.run(), same pattern used there.
"""
import asyncio

from app import mcp_server, sql_store


def _call(name, arguments):
    """FastMCP.call_tool returns (content_blocks, structured_result) in
    this SDK version — verified empirically; tests only care about the
    string result, which lives in structured_result["result"]."""
    _, structured = asyncio.run(mcp_server.mcp.call_tool(name, arguments))
    return structured["result"]


def test_lists_query_employees_tool():
    tools = asyncio.run(mcp_server.mcp.list_tools())
    assert "query_employees" in {t.name for t in tools}


def test_refuses_without_tenant_or_principal():
    result = _call("query_employees", {"tenant": "", "principal": ""})
    assert "Refused" in result


def test_invalid_department_returns_a_friendly_error(monkeypatch):
    called = []
    monkeypatch.setattr(sql_store, "query_employees", lambda **kw: called.append(kw) or [])

    result = _call(
        "query_employees",
        {"tenant": "acme", "principal": "p", "department": "NotADept"},
    )

    assert "Invalid department" in result
    assert "Engineering" in result  # lists the valid values
    assert called == []  # never reached sql_store with a bad filter


def test_valid_department_passes_through_as_the_enum_value(monkeypatch):
    captured = {}

    def fake_query_employees(tenant, department=None, name_contains=None, limit=None):
        captured["tenant"] = tenant
        captured["department"] = department
        return []

    monkeypatch.setattr(sql_store, "query_employees", fake_query_employees)

    _call(
        "query_employees",
        {"tenant": "acme", "principal": "p", "department": "Engineering"},
    )

    assert captured["tenant"] == "acme"
    assert captured["department"] == "Engineering"


def test_two_different_tenants_get_different_tenant_param(monkeypatch):
    seen = []
    monkeypatch.setattr(
        sql_store,
        "query_employees",
        lambda tenant, department=None, name_contains=None, limit=None: seen.append(tenant) or [],
    )

    _call("query_employees", {"tenant": "acme", "principal": "p"})
    _call("query_employees", {"tenant": "other-co", "principal": "p"})

    assert seen[0] != seen[1]
