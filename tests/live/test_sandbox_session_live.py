"""A real opensandbox-mcp round trip for app/domains/sandbox_session.py (GRAPH_PATTERNS.md pattern 50): the
fake-free counterpart to tests/domains/test_sandbox_session.py, which (correctly, for a fast hermetic suite)
mocks the raw tools' `.invoke(...)`. It is the shared logic behind every domain's sandbox trio
(ops/support/sales), so it tests `get_or_create_sandbox_id` directly rather than through one domain's tool
layer, like the hermetic counterpart.

It hard-asserts success for `get_or_create_sandbox_id`. An EARLIER version deliberately didn't, chasing what
looked like a twice-reproduced timing race (sandbox creation succeeding server-side but the client's readiness
poll sometimes hanging). That diagnosis was wrong. Tracing the real request URLs in debug-level httpx logs
showed that the OpenSandbox SDK's `ConnectionConfig.use_server_proxy` defaults to `False` and
`opensandbox-mcp==0.1.1`'s CLI had no flag or env var to override it. So the client (a bare host process,
app/domains/sandbox_tools.py) tried to reach each sandbox directly at its Docker bridge-network IP (e.g.
`172.19.0.13:port`), genuinely UNREACHABLE from the host on Docker Desktop for Mac, not merely slow. Confirmed
directly: a raw `Sandbox.create()` hung 44+ seconds with `use_server_proxy=False` against that address and
completed in under 1.2s with `use_server_proxy=True`.

Fixed via `--use-server-proxy` (release 1.1 of the packaged CLI; until then a wrapper script forced it), and
`app/domains/sandbox_tools.py` now launches `python -m opensandbox_mcp ... --use-server-proxy`. With the real
bug fixed, both tests complete in ~5s end to end, reliably, so hard-asserting success is the correct bar
again, not a flakiness risk.
"""
import importlib.util

import pytest

from app.domains import sandbox_session

pytestmark = pytest.mark.sandbox


@pytest.fixture(autouse=True)
def _require_opensandbox_mcp():
    # The launch is `python -m opensandbox_mcp` under this interpreter — checking importability,
    # not PATH, matches what this app's own code actually depends on.
    if importlib.util.find_spec("opensandbox_mcp") is None:
        pytest.skip("opensandbox_mcp not installed — `pip install opensandbox-mcp` (already in requirements.txt)")


async def test_reaches_the_real_bridge_and_creates_a_real_sandbox():
    raw = await sandbox_session.load_raw_sandbox_tools()
    assert raw, "opensandbox-mcp's catalog should list even with no server reachable (see module docstring)"

    sandbox_id = await sandbox_session.get_or_create_sandbox_id(raw, "live-verify-thread", tenant="ecorp")
    assert isinstance(sandbox_id, str) and sandbox_id


async def test_a_second_call_reuses_the_same_sandbox():
    raw = await sandbox_session.load_raw_sandbox_tools()
    first_id = await sandbox_session.get_or_create_sandbox_id(raw, "live-verify-reuse-thread", tenant="ecorp")
    second_id = await sandbox_session.get_or_create_sandbox_id(raw, "live-verify-reuse-thread", tenant="ecorp")
    assert second_id == first_id
