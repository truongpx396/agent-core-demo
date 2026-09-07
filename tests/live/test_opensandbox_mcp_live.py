"""A real `opensandbox-mcp` round trip for app/domains/sandbox_tools.py
(GRAPH_PATTERNS.md pattern 50) — the fake-free counterpart to
tests/domains/test_sandbox_tools.py, which (correctly, for a fast/hermetic
suite) mocks `app.mcp.client.load_remote_tools` entirely. What that can't
catch — a real `opensandbox-mcp` binary actually spawning, actually
listing its real tool catalog over stdio, through the ALSO-real
`app/mcp/client.py::load_remote_tools` (pattern 28) underneath it — needs
the real bridge on PATH, hence `@pytest.mark.sandbox` (self-skips if
`opensandbox-mcp` isn't installed, same "skip cleanly, don't fail"
contract every other live-service marker in this suite already has).

Deliberately scoped to the CATALOG round trip only, not to actually
CALLING a sandbox tool (`sandbox_create`, etc.) — verified directly, twice,
during this pattern's own development: (1) `opensandbox-mcp`'s tool
listing is served from its own static definitions, not proxied to a
backend at all, so it succeeds even with NO `opensandbox-server` running
(this is exactly what makes app/domains/sandbox_tools.py safe to wire in
eagerly, see that module's own docstring) — meaning this test needs only
the bridge installed, nothing more; (2) actually creating a sandbox hit a
real `405 Method Not Allowed` against a locally `uvx`-launched
`opensandbox-server` in this environment, reproduced even via OpenSandbox's
own official `osb` CLI — an environment/server-version quirk in that one
local setup, not a defect in this app's own code (see GRAPH_PATTERNS.md
pattern 50's own bullet on this finding). A test asserting a full
sandbox_create round trip would therefore be asserting behavior this
session could not reliably reproduce even with the vendor's own tooling —
dishonest to encode as if it were a solid, always-green check. If you have
a real `opensandbox-server` running (`make sandbox-serve`) and want to
verify further by hand: `sandbox_tools.load_sandbox_tools()[0]` returns
real, invocable LangChain tools — call `.func(image="python:3.12-slim",
timeout_seconds=120)` on the `sandbox_create` one directly.
"""
import shutil

import pytest

from app.domains import sandbox_tools

pytestmark = pytest.mark.sandbox


@pytest.fixture(autouse=True)
def _require_opensandbox_mcp():
    if shutil.which("opensandbox-mcp") is None:
        pytest.skip("opensandbox-mcp not on PATH — `pip install opensandbox-mcp` (already in requirements.txt)")


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
