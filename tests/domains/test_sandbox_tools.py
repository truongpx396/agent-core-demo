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
via `arun_with_timeout` — see that module's own docstring), so every call
below runs through `asyncio.run(...)`, this repo's established pattern for
exercising async code from a plain `def test_...`.

Every test gets a fresh `_OPENSANDBOX_BREAKER` (the `_fresh_breaker`
fixture below) so one test's failures can never carry over — via shared
module-level circuit-breaker state — into another; see
app/core/resilience.py's own docstring for what that breaker does.
`_OPENSANDBOX_RETRY_BASE_DELAY_SECONDS` is also monkeypatched down to keep
the retry tests from actually waiting out real backoff delays.
"""
import subprocess
import sys

import pytest

from app.core import metrics
from app.core.resilience import CircuitBreaker
from app.domains import sandbox_tools
from app.mcp import client as mcp_client
from tests.conftest import metric_value


@pytest.fixture(autouse=True)
def _fresh_breaker(monkeypatch):
    monkeypatch.setattr(
        sandbox_tools, "_OPENSANDBOX_BREAKER", CircuitBreaker(name="opensandbox_mcp", failure_threshold=3, cooldown_seconds=30.0)
    )
    monkeypatch.setattr(sandbox_tools, "_OPENSANDBOX_RETRY_BASE_DELAY_SECONDS", 0.001)


async def test_passes_the_configured_domain_and_protocol_to_the_bridge(monkeypatch):
    captured = {}

    async def fake_load_remote_tools(**kwargs):
        captured.update(kwargs)
        return [], {}

    monkeypatch.setattr(mcp_client, "load_remote_tools", fake_load_remote_tools)

    await sandbox_tools.load_sandbox_tools()

    # The packaged CLI under this same interpreter, with `--use-server-proxy`: a bare host process cannot reach a sandbox on the
    # Docker bridge network any other way (see sandbox_tools.py's `_BRIDGE_ARGS` comment). `--transport stdio` is said out loud.
    assert captured["command"] == sys.executable
    assert captured["args"] == [
        "-m", "opensandbox_mcp", "--transport", "stdio", "--protocol", "http", "--use-server-proxy",
        "--domain", sandbox_tools.OPENSANDBOX_MCP_DOMAIN,
    ]


def test_every_flag_the_launch_passes_is_one_the_installed_bridge_accepts():
    """Launching the packaged CLI directly (no wrapper in between) means a release that renames or drops a flag breaks the
    launch, and `load_sandbox_tools()` degrades quietly to no sandbox tools. Ask the installed CLI what it accepts."""
    help_text = subprocess.run(
        [sys.executable, "-m", "opensandbox_mcp", "--help"], capture_output=True, text=True, timeout=60, check=False
    ).stdout

    flags = {a for a in [*sandbox_tools._BRIDGE_ARGS, "--domain"] if a.startswith("--")}
    assert flags >= {"--transport", "--protocol", "--use-server-proxy", "--domain"}
    missing = {flag for flag in flags if flag not in help_text}
    assert not missing, f"the installed opensandbox-mcp no longer accepts {sorted(missing)}:\n{help_text}"


async def test_the_api_key_goes_to_the_bridge_in_its_environment_never_on_its_command_line(monkeypatch):
    """Regression (specs/009 A5): the key used to be `--api-key <value>`, readable by every user on the host in a process
    listing. The bridge falls back to OPEN_SANDBOX_API_KEY when no flag is given, so the environment carries it."""
    secret = "sk-test-not-a-real-key-4f3a"
    monkeypatch.setattr(sandbox_tools, "OPENSANDBOX_API_KEY", secret)
    captured = {}

    async def fake_load_remote_tools(**kwargs):
        captured.update(kwargs)
        return [], {}

    monkeypatch.setattr(mcp_client, "load_remote_tools", fake_load_remote_tools)

    await sandbox_tools.load_sandbox_tools()

    assert captured["env"] == {"OPEN_SANDBOX_API_KEY": secret}
    assert "--api-key" not in captured["args"]
    assert secret not in " ".join(captured["args"])


async def test_with_no_key_configured_the_bridge_gets_no_empty_variable(monkeypatch):
    """An empty OPEN_SANDBOX_API_KEY would shadow nothing but would read as 'configured, and blank' to anyone inspecting the
    child; with no key the child gets the SDK's own default environment."""
    monkeypatch.setattr(sandbox_tools, "OPENSANDBOX_API_KEY", "")
    captured = {}

    async def fake_load_remote_tools(**kwargs):
        captured.update(kwargs)
        return [], {}

    monkeypatch.setattr(mcp_client, "load_remote_tools", fake_load_remote_tools)

    await sandbox_tools.load_sandbox_tools()

    assert captured["env"] is None


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


async def test_asks_the_client_for_exactly_the_pinned_tools_and_hands_it_the_whole_json_reply(monkeypatch):
    """Regression (specs/009 A5/A3): the app uses 5 of the bridge's 19 tools and offered all 19 as described; and a client-side
    cap on the reply would cut the JSON sandbox_session parses, so the client is told not to cap (the session bounds the text)."""
    captured = {}

    async def fake_load_remote_tools(**kwargs):
        captured.update(kwargs)
        return [], {}

    monkeypatch.setattr(mcp_client, "load_remote_tools", fake_load_remote_tools)

    await sandbox_tools.load_sandbox_tools()

    assert captured["pinned"] == sandbox_tools.PINNED_SANDBOX_TOOLS
    assert set(captured["pinned"]) == {"sandbox_list", "sandbox_create", "command_run", "file_read", "file_write"}
    assert captured["cap_output"] is False
    assert captured["dependency"] == "opensandbox_mcp"


@pytest.mark.parametrize(
    "error, reason",
    [
        (ConnectionRefusedError("not up"), "connection"),
        (TimeoutError("hung"), "timeout"),
        (FileNotFoundError("bridge script missing"), "other"),
        (RuntimeError("boom"), "other"),
    ],
)
async def test_every_degrade_is_counted_by_why(monkeypatch, error, reason):
    """Regression (specs/009 A9): a bridge that could not be reached used to leave a log line and nothing else, so no alert could
    ever fire on 'the sandbox has been off for an hour'."""
    async def raises(**kwargs):
        raise error

    monkeypatch.setattr(mcp_client, "load_remote_tools", raises)
    before = metric_value(metrics.agent_sandbox_unavailable_total, reason=reason)

    assert await sandbox_tools.load_sandbox_tools() == ([], {})

    assert metric_value(metrics.agent_sandbox_unavailable_total, reason=reason) == before + 1


async def test_a_breaker_that_is_refusing_to_try_is_counted_as_circuit_open(monkeypatch):
    async def always_refuses(**kwargs):
        raise ConnectionRefusedError("unreachable")

    monkeypatch.setattr(mcp_client, "load_remote_tools", always_refuses)
    for _ in range(3):
        await sandbox_tools.load_sandbox_tools()
    before = metric_value(metrics.agent_sandbox_unavailable_total, reason="circuit_open")

    await sandbox_tools.load_sandbox_tools()

    assert metric_value(metrics.agent_sandbox_unavailable_total, reason="circuit_open") == before + 1


async def test_a_successful_load_counts_nothing_as_unavailable(monkeypatch):
    async def fine(**kwargs):
        return ["command_run"], {"command_run": "outward"}

    monkeypatch.setattr(mcp_client, "load_remote_tools", fine)
    before = {r: metric_value(metrics.agent_sandbox_unavailable_total, reason=r) for r in ("connection", "timeout", "circuit_open", "other")}

    await sandbox_tools.load_sandbox_tools()

    assert before == {r: metric_value(metrics.agent_sandbox_unavailable_total, reason=r) for r in before}
