"""A real `opensandbox-mcp` round trip for app/adapters/mcp/sandbox_tools.py (GRAPH_PATTERNS.md pattern 50): the
fake-free counterpart to tests/adapters/mcp/test_sandbox_tools.py, which (correctly, for a fast hermetic suite)
mocks `app.mcp.client.load_remote_tools`. What that can't catch: the packaged `opensandbox-mcp` CLI (`python
-m opensandbox_mcp --use-server-proxy`, what this module spawns) actually starting and listing its real tool
catalog over stdio, through the also-real `app/mcp/client.py::load_remote_tools` (pattern 28). It needs
`opensandbox_mcp` installed, hence `@pytest.mark.sandbox` (self-skips if it isn't, the same "skip cleanly,
don't fail" contract as every live-service marker here).

Deliberately scoped to the CATALOG round trip, not to CALLING a sandbox tool (`sandbox_create`, etc.).
Verified directly: `opensandbox-mcp`'s tool listing is served from static definitions, not proxied to a
backend, so it succeeds with NO `opensandbox-server` running (which is what makes app/adapters/mcp/sandbox_tools.py
safe to wire in eagerly; see its docstring). This test therefore needs only the bridge installed. Creating a
sandbox needs a real, reachable, authenticated `opensandbox-server` (`make sandbox-up`);
tests/live/test_sandbox_session_live.py covers that full round trip, kept separate from this file's narrower,
always-installable scope.
"""
import importlib.util

import pytest

from app.adapters.mcp import sandbox_tools

pytestmark = pytest.mark.sandbox


@pytest.fixture(autouse=True)
def _require_opensandbox_mcp():
    # The launch is `python -m opensandbox_mcp` under this interpreter — checking importability,
    # not PATH, matches what this app's own code actually depends on.
    if importlib.util.find_spec("opensandbox_mcp") is None:
        pytest.skip("opensandbox_mcp not installed — `pip install opensandbox-mcp` (already in requirements.txt)")


async def test_lists_the_real_opensandbox_tool_catalog():
    tools, _capabilities = await sandbox_tools.load_sandbox_tools()

    names = {t.name for t in tools}
    assert "sandbox_create" in names
    assert "command_run" in names
    assert "file_read" in names
    assert "file_write" in names


async def test_every_real_tool_is_capped_at_outward():
    tools, capabilities = await sandbox_tools.load_sandbox_tools()

    assert tools  # the skip fixture above already ruled out "bridge missing"
    for tool in tools:
        assert capabilities[tool.name] == "outward", tool.name
