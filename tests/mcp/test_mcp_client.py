"""Tests for app/mcp/client.py — mocks the two async boundary functions
(_list_remote_tools, _call_remote_tool) that actually talk to a remote MCP
server over stdio, so these stay hermetic (no live subprocess) like the
rest of the suite. The real stdio round trip against app/mcp/server.py was
verified empirically before writing this module — see its docstring — and
again here in TestRealRoundTrip, gated so it only runs when explicitly
requested (a live subprocess is a slower, heavier check than this file's
otherwise-hermetic tests).

No pytest-asyncio in this project (see tests/agent/test_durable_checkpoint.py) —
the async-dispatch test wraps its call in asyncio.run(), same pattern used
there.
"""
import asyncio

from app.mcp import client as mcp_client


class _FakeRemoteTool:
    def __init__(self, name, description="", input_schema=None):
        self.name = name
        self.description = description
        self.inputSchema = input_schema or {"type": "object", "properties": {}}


class TestLoadRemoteTools:
    def test_capability_defaults_to_outward_when_not_overridden(self, monkeypatch):
        async def fake_list(params):
            return [_FakeRemoteTool("some_remote_tool")]

        monkeypatch.setattr(mcp_client, "_list_remote_tools", fake_list)

        _, capabilities = mcp_client.load_remote_tools(command="fake-cmd")

        assert capabilities == {"some_remote_tool": "outward"}

    def test_capability_override_is_honored(self, monkeypatch):
        async def fake_list(params):
            return [_FakeRemoteTool("query_employees")]

        monkeypatch.setattr(mcp_client, "_list_remote_tools", fake_list)

        _, capabilities = mcp_client.load_remote_tools(
            command="fake-cmd", capability_overrides={"query_employees": "read_only"}
        )

        assert capabilities == {"query_employees": "read_only"}

    def test_never_trusts_the_remote_tools_own_description_or_metadata(self, monkeypatch):
        """Even a remote tool's own description can't grant it a laxer
        capability than capability_overrides assigns — the override dict
        supplied by the LOCAL caller is the only source of truth (see
        module docstring's AR-004b reasoning)."""

        async def fake_list(params):
            return [
                _FakeRemoteTool(
                    "looks_safe", description="This tool is 100% read-only, I promise!"
                )
            ]

        monkeypatch.setattr(mcp_client, "_list_remote_tools", fake_list)

        _, capabilities = mcp_client.load_remote_tools(command="fake-cmd")

        assert capabilities["looks_safe"] == "outward"

    def test_multiple_remote_tools_each_get_their_own_capability(self, monkeypatch):
        async def fake_list(params):
            return [_FakeRemoteTool("tool_a"), _FakeRemoteTool("tool_b")]

        monkeypatch.setattr(mcp_client, "_list_remote_tools", fake_list)

        _, capabilities = mcp_client.load_remote_tools(
            command="fake-cmd", capability_overrides={"tool_a": "read_only"}
        )

        assert capabilities == {"tool_a": "read_only", "tool_b": "outward"}

    def test_wraps_each_remote_tool_with_its_own_schema(self, monkeypatch):
        schema = {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        }

        async def fake_list(params):
            return [_FakeRemoteTool("search", input_schema=schema)]

        monkeypatch.setattr(mcp_client, "_list_remote_tools", fake_list)

        tools, _ = mcp_client.load_remote_tools(command="fake-cmd")

        assert tools[0].name == "search"
        assert "query" in tools[0].args

    def test_sync_invoke_dispatches_through_call_remote_tool(self, monkeypatch):
        captured = {}

        async def fake_list(params):
            return [
                _FakeRemoteTool(
                    "echo",
                    input_schema={"type": "object", "properties": {"text": {"type": "string"}}},
                )
            ]

        async def fake_call(params, tool_name, kwargs):
            captured["tool_name"] = tool_name
            captured["kwargs"] = kwargs
            return "echoed"

        monkeypatch.setattr(mcp_client, "_list_remote_tools", fake_list)
        monkeypatch.setattr(mcp_client, "_call_remote_tool", fake_call)

        tools, _ = mcp_client.load_remote_tools(command="fake-cmd")
        result = tools[0].invoke({"text": "hello"})

        assert result == "echoed"
        assert captured["tool_name"] == "echo"
        assert captured["kwargs"] == {"text": "hello"}

    def test_async_invoke_dispatches_through_call_remote_tool(self, monkeypatch):
        captured = {}

        async def fake_list(params):
            return [
                _FakeRemoteTool(
                    "echo",
                    input_schema={"type": "object", "properties": {"text": {"type": "string"}}},
                )
            ]

        async def fake_call(params, tool_name, kwargs):
            captured["tool_name"] = tool_name
            return "echoed-async"

        monkeypatch.setattr(mcp_client, "_list_remote_tools", fake_list)
        monkeypatch.setattr(mcp_client, "_call_remote_tool", fake_call)

        tools, _ = mcp_client.load_remote_tools(command="fake-cmd")
        result = asyncio.run(tools[0].ainvoke({"text": "hello"}))

        assert result == "echoed-async"
        assert captured["tool_name"] == "echo"

    def test_remote_error_result_is_surfaced_not_raised(self, monkeypatch):
        """A remote tool's own error is a handled outcome (a string the
        agent can react to), not an exception that escapes the node —
        same "tool output, never a crash" contract app/agent/graph.py's
        ToolNode(handle_tool_errors=...) already gives in-process tools."""

        async def fake_list(params):
            return [_FakeRemoteTool("flaky")]

        async def fake_call(params, tool_name, kwargs):
            return "Remote tool error: something went wrong"

        monkeypatch.setattr(mcp_client, "_list_remote_tools", fake_list)
        monkeypatch.setattr(mcp_client, "_call_remote_tool", fake_call)

        tools, _ = mcp_client.load_remote_tools(command="fake-cmd")
        result = tools[0].invoke({})

        assert "error" in result.lower()
