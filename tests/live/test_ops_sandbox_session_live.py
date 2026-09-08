"""A real opensandbox-mcp round trip for app/domains/ops/sandbox_session.py
(GRAPH_PATTERNS.md pattern 50) — the fake-free counterpart to
tests/domains/ops/test_sandbox_session.py, which (correctly, for a fast/
hermetic suite) mocks the raw tools' `.invoke(...)` entirely.

Hard-asserts success for `get_or_create_sandbox_id` — an EARLIER version of
this test deliberately didn't, chasing what looked like a real, twice-
reproduced timing race (sandbox creation succeeding server-side but the
client's readiness poll occasionally hanging). That diagnosis was wrong:
the actual cause, found by tracing the real request URLs in debug-level
httpx logs, is that the OpenSandbox SDK's `ConnectionConfig.use_server_proxy`
defaults to `False`, and `opensandbox-mcp==0.1.1`'s own CLI has no flag/env
var to override it — so the client (a bare host process,
app/domains/sandbox_tools.py) tried to reach each sandbox directly at its
Docker bridge-network IP (e.g. `172.19.0.13:port`), an address genuinely
UNREACHABLE from the host on Docker Desktop for Mac, not merely slow.
Confirmed directly: a raw `Sandbox.create()` call hung 44+ seconds with
`use_server_proxy=False` against that unreachable address, then completed
in under 1.2s with `use_server_proxy=True`. Fixed via
`scripts/opensandbox_mcp_bridge.py` (see its own docstring for the full
finding) — `app/domains/sandbox_tools.py` now spawns that wrapper instead
of the packaged `opensandbox-mcp` binary directly. With the real bug
fixed, this test's whole suite (both tests) now completes in ~5s
end-to-end, reliably — hard-asserting success is the correct bar again,
not a flakiness risk.
"""
import importlib.util

import pytest

from app.domains.ops import sandbox_session

pytestmark = pytest.mark.sandbox


@pytest.fixture(autouse=True)
def _require_opensandbox_mcp():
    # scripts/opensandbox_mcp_bridge.py imports `opensandbox_mcp` as a
    # LIBRARY (not the CLI binary) — checking importability, not PATH,
    # matches what this app's own code actually depends on now.
    if importlib.util.find_spec("opensandbox_mcp") is None:
        pytest.skip("opensandbox_mcp not installed — `pip install opensandbox-mcp` (already in requirements.txt)")


def test_reaches_the_real_bridge_and_creates_a_real_sandbox():
    raw = sandbox_session.load_raw_sandbox_tools()
    assert raw, "opensandbox-mcp's catalog should list even with no server reachable (see module docstring)"

    sandbox_id = sandbox_session.get_or_create_sandbox_id(raw, "live-verify-thread")
    assert isinstance(sandbox_id, str) and sandbox_id


def test_a_second_call_reuses_the_same_sandbox():
    raw = sandbox_session.load_raw_sandbox_tools()
    first_id = sandbox_session.get_or_create_sandbox_id(raw, "live-verify-reuse-thread")
    second_id = sandbox_session.get_or_create_sandbox_id(raw, "live-verify-reuse-thread")
    assert second_id == first_id
