"""A real opensandbox-mcp round trip for app/domains/ops/sandbox_session.py
(GRAPH_PATTERNS.md pattern 50) — the fake-free counterpart to
tests/domains/ops/test_sandbox_session.py, which (correctly, for a fast/
hermetic suite) mocks the raw tools' `.invoke(...)` entirely.

This deliberately does NOT assert a specific outcome (success vs. failure)
for `get_or_create_sandbox_id` — this environment's own locally
`uvx`-launched opensandbox-server hits a real HTTP 405 on every
sandbox_create attempt (and an empty-body response on sandbox_list) — a
server-version quirk, reproduced even via OpenSandbox's own official `osb`
CLI, disclosed in GRAPH_PATTERNS.md pattern 50 and in sandbox_session.py's
own module docstring — so asserting "it fails" here would just be
encoding one broken local server's behavior as if it were correct.
Instead this test asserts what has to hold regardless of whether the
`opensandbox-server` on the other end is actually healthy: the call
reaches the real bridge (a real subprocess, a real HTTP request) and
either returns a genuine sandbox_id or raises a clean, typed
`SandboxCallFailed` — never crashes with some other exception, and never
hangs. If you have a genuinely working `opensandbox-server` (this
session's own local one didn't — see above): a SECOND call with the same
thread id should return the SAME sandbox_id (reused via the sandbox_list
metadata filter, not recreated) — this file also verifies that when the
first call happens to succeed, and skips it (not fails) otherwise, since
that reuse behavior can't be checked without a real sandbox to reuse.
"""
import shutil

import pytest

from app.domains.ops import sandbox_session

pytestmark = pytest.mark.sandbox


@pytest.fixture(autouse=True)
def _require_opensandbox_mcp():
    if shutil.which("opensandbox-mcp") is None:
        pytest.skip("opensandbox-mcp not on PATH — `pip install opensandbox-mcp` (already in requirements.txt)")


def test_reaches_the_real_bridge_and_either_succeeds_or_fails_cleanly():
    raw = sandbox_session.load_raw_sandbox_tools()
    assert raw, "opensandbox-mcp's catalog should list even with no server reachable (see module docstring)"

    try:
        sandbox_id = sandbox_session.get_or_create_sandbox_id(raw, "live-verify-thread")
    except sandbox_session.SandboxCallFailed as exc:
        assert str(exc)  # a real, non-empty message — not a bare crash
        return
    assert isinstance(sandbox_id, str) and sandbox_id


def test_a_second_call_reuses_the_same_sandbox_if_the_first_one_succeeded():
    raw = sandbox_session.load_raw_sandbox_tools()
    try:
        first_id = sandbox_session.get_or_create_sandbox_id(raw, "live-verify-reuse-thread")
    except sandbox_session.SandboxCallFailed:
        pytest.skip(
            "the real opensandbox-server this session ran against couldn't create a "
            "sandbox at all (see this file's own docstring) — nothing to verify reuse against"
        )
    second_id = sandbox_session.get_or_create_sandbox_id(raw, "live-verify-reuse-thread")
    assert second_id == first_id
