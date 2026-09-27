"""Tests for app/domains/sandbox_tools.py — mocks
`app.mcp.client.load_remote_tools` (the existing, unmodified pattern-28
client this module builds on, same boundary tests/mcp/test_mcp_client.py
already mocks one level deeper at `_list_remote_tools`/`_call_remote_tool`)
so these stay hermetic, no live `opensandbox-mcp`/`opensandbox-server`
needed. The graceful-degrade path this file exists to prove is the one
piece of this module's own logic that isn't already covered by
tests/domains/ops/test_domain.py's live-environment-dependent assertions
(see that file's own comment on why it can't hardcode an exact sandbox
tool set).

`load_sandbox_tools` is `async def` now (awaits `mcp_client.load_remote_tools`
via `_arun_with_timeout` — see that module's own docstring), so every call
below runs through `asyncio.run(...)`, this repo's established pattern for
exercising async code from a plain `def test_...`.

Every test gets a fresh `_OPENSANDBOX_BREAKER` (the `_fresh_breaker`
fixture below) so one test's failures can never carry over — via shared
module-level circuit-breaker state — into another; see
app/core/resilience.py's own docstring for what that breaker does.
`_OPENSANDBOX_RETRY_BASE_DELAY_SECONDS` is also monkeypatched down to keep
the retry tests from actually waiting out real backoff delays.
"""
import sys

import pytest

from app.core.resilience import CircuitBreaker
from app.domains import sandbox_tools
from app.mcp import client as mcp_client


@pytest.fixture(autouse=True)
def _fresh_breaker(monkeypatch):
    monkeypatch.setattr(
        sandbox_tools, "_OPENSANDBOX_BREAKER", CircuitBreaker(name="opensandbox_mcp", failure_threshold=3, cooldown_seconds=30.0)
    )
    monkeypatch.setattr(sandbox_tools, "_OPENSANDBOX_RETRY_BASE_DELAY_SECONDS", 0.001)


async def test_passes_the_configured_domain_protocol_and_api_key_to_the_bridge(monkeypatch):
    captured = {}

    async def fake_load_remote_tools(**kwargs):
        captured.update(kwargs)
        return [], {}

    monkeypatch.setattr(mcp_client, "load_remote_tools", fake_load_remote_tools)

    await sandbox_tools.load_sandbox_tools()

    # sys.executable + scripts/opensandbox_mcp_bridge.py, NOT the packaged
    # `opensandbox-mcp` binary directly — see sandbox_tools.py's own
    # `_BRIDGE_SCRIPT` comment for why (opensandbox-mcp==0.1.1's CLI has no
    # way to set ConnectionConfig(use_server_proxy=True), which this app's
    # containerized opensandbox-server deployment needs).
    assert captured["command"] == sys.executable
    assert captured["args"] == [
        sandbox_tools._BRIDGE_SCRIPT,
        "--domain", sandbox_tools.OPENSANDBOX_MCP_DOMAIN, "--protocol", "http",
        "--api-key", sandbox_tools.OPENSANDBOX_API_KEY,
    ]


async def test_never_supplies_capability_overrides_so_every_tool_defaults_to_outward(monkeypatch):
    """No override means load_remote_tools's own fail-closed default
    applies (app/mcp/client.py: unlisted -> "outward") — this module must
    never narrow that on OpenSandbox's behalf."""
    captured = {}

    async def fake_load_remote_tools(**kwargs):
        captured.update(kwargs)
        return [], {}

    monkeypatch.setattr(mcp_client, "load_remote_tools", fake_load_remote_tools)

    await sandbox_tools.load_sandbox_tools()

    assert captured["capability_overrides"] == {}


async def test_returns_the_real_tools_and_capabilities_on_success(monkeypatch):
    fake_tools = ["sandbox_create", "command_run"]
    fake_caps = {"sandbox_create": "outward", "command_run": "outward"}

    async def fake_load_remote_tools(**kwargs):
        return fake_tools, fake_caps

    monkeypatch.setattr(mcp_client, "load_remote_tools", fake_load_remote_tools)

    tools, caps = await sandbox_tools.load_sandbox_tools()

    assert tools == fake_tools
    assert caps == fake_caps


async def test_degrades_to_empty_when_the_bridge_is_not_installed(monkeypatch):
    async def raise_not_found(**kwargs):
        raise FileNotFoundError("opensandbox-mcp not found on PATH")

    monkeypatch.setattr(mcp_client, "load_remote_tools", raise_not_found)

    tools, caps = await sandbox_tools.load_sandbox_tools()

    assert (tools, caps) == ([], {})


async def test_degrades_to_empty_on_any_other_connection_failure(monkeypatch):
    async def raise_connection_error(**kwargs):
        raise ConnectionRefusedError("could not reach the sandbox server")

    monkeypatch.setattr(mcp_client, "load_remote_tools", raise_connection_error)

    tools, caps = await sandbox_tools.load_sandbox_tools()

    assert (tools, caps) == ([], {})


async def test_degraded_result_never_raises_even_when_logging(monkeypatch, caplog):
    async def raise_boom(**kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(mcp_client, "load_remote_tools", raise_boom)

    tools, caps = await sandbox_tools.load_sandbox_tools()

    assert (tools, caps) == ([], {})


async def test_retries_once_on_a_connection_failure_then_succeeds(monkeypatch):
    """The exact scenario app/core/resilience.py exists for: the bridge
    subprocess started but opensandbox-server hadn't finished booting on
    the FIRST attempt — a second, immediate attempt finds it up."""
    calls = {"count": 0}
    fake_tools, fake_caps = ["command_run"], {"command_run": "outward"}

    async def flaky(**kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise ConnectionRefusedError("opensandbox-server not up yet")
        return fake_tools, fake_caps

    monkeypatch.setattr(mcp_client, "load_remote_tools", flaky)

    tools, caps = await sandbox_tools.load_sandbox_tools()

    assert (tools, caps) == (fake_tools, fake_caps)
    assert calls["count"] == 2


async def test_does_not_retry_a_non_connection_failure(monkeypatch):
    calls = {"count": 0}

    async def raise_not_found(**kwargs):
        calls["count"] += 1
        raise FileNotFoundError("opensandbox-mcp not found on PATH")

    monkeypatch.setattr(mcp_client, "load_remote_tools", raise_not_found)

    tools, caps = await sandbox_tools.load_sandbox_tools()

    assert (tools, caps) == ([], {})
    assert calls["count"] == 1  # a missing bridge script won't fix itself on retry


async def test_circuit_breaker_fails_fast_after_repeated_connection_failures(monkeypatch):
    """Three (_OPENSANDBOX_BREAKER's failure_threshold) calls that each
    exhaust their own retry all fail — the FOURTH must short-circuit
    without even invoking load_remote_tools again."""
    calls = {"count": 0}

    async def always_refuses(**kwargs):
        calls["count"] += 1
        raise ConnectionRefusedError("opensandbox-server unreachable")

    monkeypatch.setattr(mcp_client, "load_remote_tools", always_refuses)

    for _ in range(3):
        assert await sandbox_tools.load_sandbox_tools() == ([], {})

    calls_before_breaker_open = calls["count"]

    tools, caps = await sandbox_tools.load_sandbox_tools()

    assert (tools, caps) == ([], {})  # still degrades the same way from the caller's side
    assert calls["count"] == calls_before_breaker_open  # but never touched the network this time
