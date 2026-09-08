"""A real `opensandbox-mcp` round trip for app/domains/sandbox_tools.py
(GRAPH_PATTERNS.md pattern 50) — the fake-free counterpart to
tests/domains/test_sandbox_tools.py, which (correctly, for a fast/hermetic
suite) mocks `app.mcp.client.load_remote_tools` entirely. What that can't
catch — `scripts/opensandbox_mcp_bridge.py` (the wrapper this module
spawns instead of the packaged `opensandbox-mcp` binary directly, see that
script's own docstring) actually spawning, actually listing its real tool
catalog over stdio, through the ALSO-real
`app/mcp/client.py::load_remote_tools` (pattern 28) underneath it — needs
`opensandbox_mcp` actually installed, hence `@pytest.mark.sandbox`
(self-skips if it isn't, same "skip cleanly, don't fail" contract every
other live-service marker in this suite already has).

Deliberately scoped to the CATALOG round trip only, not to actually
CALLING a sandbox tool (`sandbox_create`, etc.) — verified directly:
`opensandbox-mcp`'s tool listing is served from its own static
definitions, not proxied to a backend at all, so it succeeds even with NO
`opensandbox-server` running (this is exactly what makes
app/domains/sandbox_tools.py safe to wire in eagerly, see that module's
own docstring) — meaning this test needs only the bridge installed,
nothing more. Actually creating a sandbox needs a real, reachable,
authenticated `opensandbox-server` (`make sandbox-up`) — that full round
trip is covered by tests/live/test_ops_sandbox_session_live.py instead,
kept separate from this file's own narrower, always-installable-bridge
scope.
"""
import importlib.util

import pytest

from app.domains import sandbox_tools

pytestmark = pytest.mark.sandbox


@pytest.fixture(autouse=True)
def _require_opensandbox_mcp():
    # scripts/opensandbox_mcp_bridge.py imports `opensandbox_mcp` as a
    # LIBRARY (not the CLI binary) — checking importability, not PATH,
    # matches what this app's own code actually depends on now.
    if importlib.util.find_spec("opensandbox_mcp") is None:
        pytest.skip("opensandbox_mcp not installed — `pip install opensandbox-mcp` (already in requirements.txt)")


def test_lists_the_real_opensandbox_tool_catalog():
    tools, _capabilities = sandbox_tools.load_sandbox_tools()

    names = {t.name for t in tools}
    assert "sandbox_create" in names
    assert "command_run" in names
    assert "file_read" in names
    assert "file_write" in names


def test_every_real_tool_is_capped_at_outward():
    tools, capabilities = sandbox_tools.load_sandbox_tools()

    assert tools  # the skip fixture above already ruled out "bridge missing"
    for tool in tools:
        assert capabilities[tool.name] == "outward", tool.name
