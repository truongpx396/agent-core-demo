"""The published servers as a client actually meets them: a real subprocess, speaking MCP over its standard input and output.

tests/mcp/test_mcp_server.py and test_ops_server.py call the tools in-process, which proves the logic but not three things a
desktop assistant depends on, and which only a real process shows:
  * a server started without an identity exits, says why, and writes NOTHING to stdout (stdout is the protocol channel: one stray
    line corrupts the session);
  * the schema on the wire has no tenant or principal argument;
  * the audit line is JSON on stderr, not on the protocol channel.

No service is needed: the one tool called here refuses an invalid filter before it would touch a database. The child runs in an
empty directory with MCP_* set explicitly (to "" where a test wants none): `.env` is found by walking up from app/core/config.py,
not from the working directory, so a developer's own file would otherwise decide what these tests see.
"""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

REPO = Path(__file__).resolve().parents[2]
_PYTHONPATH = os.pathsep.join(str(REPO / p) for p in (".", "packages/billing/src", "docker/ml-service"))


def _env(**mcp: str) -> dict[str, str]:
    env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", ""), "PYTHONPATH": _PYTHONPATH}
    env.update({"MCP_TENANT": "", "MCP_PRINCIPAL": ""})
    env.update(mcp)
    return env


def test_a_server_started_without_an_identity_exits_and_leaves_stdout_clean(tmp_path):
    done = subprocess.run(
        [sys.executable, "-m", "app.mcp.server"],
        env=_env(), cwd=tmp_path, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=90,
    )

    assert done.returncode != 0
    assert "MCP_TENANT" in done.stderr
    assert done.stdout == ""


def test_the_operations_server_without_a_principal_exits_the_same_way(tmp_path):
    done = subprocess.run(
        [sys.executable, "-m", "app.mcp.ops_server"],
        env=_env(), cwd=tmp_path, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=90,
    )

    assert done.returncode != 0
    assert "MCP_PRINCIPAL" in done.stderr
    assert done.stdout == ""


async def test_over_stdio_the_schema_has_no_identity_argument_and_every_call_is_audited_on_stderr(tmp_path):
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "app.mcp.server"], cwd=str(tmp_path),
        env=_env(MCP_TENANT="ecorp", MCP_PRINCIPAL="alice"),
    )
    with tempfile.TemporaryFile("w+") as stderr:
        async with asyncio.timeout(90):
            async with stdio_client(params, errlog=stderr) as (read, write), ClientSession(read, write) as session:
                await session.initialize()
                tools = (await session.list_tools()).tools
                result = await session.call_tool("query_employees", {"department": "NotADept"})
        stderr.seek(0)
        logged = stderr.read()

    (tool,) = tools
    assert set(tool.input_schema["properties"]) == {"department", "name_contains"}
    assert tool.annotations.read_only_hint is True
    assert result.content[0].text.startswith("Invalid department")

    audit = [json.loads(line) for line in logged.splitlines() if line.startswith("{") and "mcp_tool_call" in line]
    assert len(audit) == 1
    assert (audit[0]["tool"], audit[0]["outcome"], audit[0]["tenant"], audit[0]["principal"]) == (
        "query_employees", "invalid", "ecorp", "alice",
    )
    assert "NotADept" not in logged


@pytest.mark.parametrize("module, tools", [("app.mcp.ops_server", {"fetch_metrics_summary", "list_recent_incidents"})])
async def test_the_operations_server_lists_its_tools_over_stdio_with_only_the_launchers_principal(module, tools, tmp_path):
    params = StdioServerParameters(
        command=sys.executable, args=["-m", module], cwd=str(tmp_path), env=_env(MCP_PRINCIPAL="oncall"),
    )
    with tempfile.TemporaryFile("w+") as stderr:
        async with asyncio.timeout(90):
            async with stdio_client(params, errlog=stderr) as (read, write), ClientSession(read, write) as session:
                await session.initialize()
                listed = (await session.list_tools()).tools

    assert {t.name for t in listed} == tools
    assert all("principal" not in t.input_schema["properties"] for t in listed)

