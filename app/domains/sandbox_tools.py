"""OpenSandbox (https://github.com/opensandbox-group/OpenSandbox) consumed
over MCP (pattern 50), built on the existing, unmodified
`app/mcp/client.py::load_remote_tools` (pattern 28). `opensandbox-mcp` is a
purpose-built stdio MCP server bridging to a containerized
`opensandbox-server` (`make sandbox-up`). New here: `scripts/
opensandbox_mcp_bridge.py`, spawned instead of the packaged
`opensandbox-mcp` binary directly, because that CLI has no way to set
`ConnectionConfig(use_server_proxy=True)` — without it, a sandbox call
hangs 40+ seconds against a Docker-internal bridge IP unreachable from the
host (see that script's docstring).

Historical gotcha: sandbox-creation calls 405'd, root-caused to
`opensandbox_mcp_domain`'s old port (8080) colliding with
`docker-compose.yml`'s `open-webui` — requests silently landed on
open-webui instead. Fixed via port 8090 + real `--api-key` auth.

## Why a real code-execution tool, unlike app/agent/tools/builtin.py::calculator
`calculator` is a narrow AST evaluator specifically because this app has
nowhere safe to run arbitrary code. OpenSandbox changes that: an isolated,
disposable container is a real security boundary. Wired into the ops
domain for investigations needing computation beyond arithmetic (recompute
a percentile, grep a log dump, diff two JSON configs).

## Fail-soft, lazy loading
`opensandbox-mcp`'s tool catalog is served from static definitions, not
proxied to the backend — `load_sandbox_tools()` returns near-instantly
even with no `opensandbox-server` running (only actually calling a tool
needs the backend reachable). Its one caller,
`sandbox_session.py::load_raw_sandbox_tools`, calls it lazily and caches
it there. Must degrade, never crash, if opensandbox-mcp is missing or the
bridge hangs: wrapped in `arun_with_timeout`, all exceptions caught,
degrading to `([], {})` with a logged warning.

A connection-shaped failure (`ConnectionError`/`TimeoutError` — a bridge
subprocess that started but couldn't yet reach `opensandbox-server`, or one
that hung past `_SANDBOX_LIST_TIMEOUT_SECONDS`) gets one retry via
`app/core/resilience.py::CircuitBreaker` before giving up — enough to ride
out the server still finishing its own boot, the exact scenario
`sandbox_session.py`'s own "never cache an empty result" comment already
exists to tolerate on the CALLER's side. Any other exception (e.g.
`FileNotFoundError` — the bridge script genuinely isn't there) is never
retried: that failure will look identical a second later. Once
`_OPENSANDBOX_BREAKER` has seen enough consecutive exhausted-retry failures,
further calls fail fast (no subprocess spawn at all) until its cooldown
elapses — see that module's own docstring for why.

## Disclosed gap: no SecurityCtx, no tenant scoping
Same limitation as any remote MCP tool (app/mcp/client.py) — no
`RunnableConfig`/`SecurityCtx` channel over MCP. Acceptable here since a
code sandbox is a shared operational resource, not tenant data; the
mandatory-approval gate (every tool forced to "outward") is the actual
safety boundary.
"""
import logging
import sys
from pathlib import Path

from langchain_core.tools import BaseTool

from app.core import metrics
from app.core.config import OPENSANDBOX_API_KEY, OPENSANDBOX_MCP_DOMAIN
from app.core.resilience import CircuitBreaker, CircuitOpenError
from app.core.tool_runtime import arun_with_timeout
from app.mcp import client as mcp_client

logger = logging.getLogger(__name__)

_SANDBOX_LIST_TIMEOUT_SECONDS = 10  # backstop against a hung bridge
# process; catalog listing is normally near-instant (see module docstring).

_OPENSANDBOX_BREAKER = CircuitBreaker(name="opensandbox_mcp", failure_threshold=3, cooldown_seconds=30.0)
_OPENSANDBOX_RETRY_ATTEMPTS = 2  # 1 initial + 1 retry — enough to ride out
# opensandbox-server still finishing its own boot without piling multiple
# _SANDBOX_LIST_TIMEOUT_SECONDS-long waits onto every early call once the
# breaker above is what actually protects a sustained outage.
_OPENSANDBOX_RETRY_BASE_DELAY_SECONDS = 1.0

# scripts/opensandbox_mcp_bridge.py, not the packaged opensandbox-mcp CLI
# directly: that CLI can't set ConnectionConfig(use_server_proxy=True),
# needed for this host process to reach sandboxes on the Docker bridge
# network. Runs under this same interpreter, so the same installed
# opensandbox-mcp/opensandbox packages are guaranteed available.
_BRIDGE_SCRIPT = str(Path(__file__).resolve().parent.parent.parent / "scripts" / "opensandbox_mcp_bridge.py")

# The variable the OpenSandbox SDK falls back to when no `--api-key` is given (opensandbox.config.ConnectionConfig).
OPEN_SANDBOX_API_KEY_ENV = "OPEN_SANDBOX_API_KEY"

# The five of the bridge's 19 tools this app uses (sandbox_session.py), each with the fingerprint of its name, description and
# input schema as `opensandbox-mcp==0.1.1` offers it (app/mcp/client.py::tool_fingerprint). The client keeps exactly these and
# only while they still match: the other fourteen (kill, renew, move, delete, replace, ...) are never offered to the model, and
# a release that rewrites one of these five's description, which is text the model obeys, is left out and counted until a
# person reads the change and updates the hash. tests/domains/test_sandbox_tool_pins.py fails on the same drift in CI, and
# prints the new values, so this is a two-line change with the diff of the bridge's descriptions next to it.
PINNED_SANDBOX_TOOLS = {
    "command_run": "bb1d2d52cbbfce43e20fdf1972d15d7f8ac1fd3078d0add74bd2fe8a6e8c8697",
    "file_read": "0fc75b8d353bda9cc1da0ea57163bdc73df48feca09ced886942853e22f80ca9",
    "file_write": "ea0a02e9633235d55615f4e5bfee1a698786f589b5cb514bdf5fd1ab0c67776e",
    "sandbox_create": "60cdc374fde8d4c6e9687368a1a2e0cd4f7729ae7cccd9e3c809ac50e6cc65d2",
    "sandbox_list": "5232c31c63f8b822830defaa2e998cacbd64c4d4e49bc14094cb0a371a707888",
}

SANDBOX_DEPENDENCY = "opensandbox_mcp"  # the label on every metric about this bridge, same as its circuit breaker's


def _unavailable_reason(exc: BaseException) -> str:
    """A coarse, bounded label for why the catalog could not be loaded (a metric label must not be a free-form message)."""
    if isinstance(exc, CircuitOpenError):
        return "circuit_open"
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, ConnectionError):
        return "connection"
    return "other"


async def load_sandbox_tools() -> tuple[list[BaseTool], dict[str, str]]:
    """Connects to `opensandbox-mcp` and returns its tool catalog as
    LangChain tools, every one capped at `"outward"`. Degrades to
    `([], {})` with a logged warning — never raises — if the bridge isn't
    installed/reachable or hangs past `_SANDBOX_LIST_TIMEOUT_SECONDS`, so a
    domain merging this in still builds with every other tool intact.

    `async def` so this can await `mcp_client.load_remote_tools` directly
    on the caller's event loop (its caller,
    `sandbox_session.py::load_raw_sandbox_tools`, is async too) instead of
    dispatching to a worker thread and blocking that loop.
    """
    async def _attempt():
        return await arun_with_timeout(
            mcp_client.load_remote_tools,
            command=sys.executable,
            args=[_BRIDGE_SCRIPT, "--domain", OPENSANDBOX_MCP_DOMAIN, "--protocol", "http"],
            # The key travels in the child's ENVIRONMENT, not its argument list: argv is readable by every user on the
            # host through the process table (`ps`), the environment of a process is not. The bridge asks for it with
            # no flag given (`ConnectionConfig.get_api_key`: the argument, else OPEN_SANDBOX_API_KEY), and the SDK adds
            # this to its small default child environment (PATH, HOME, ...) rather than inheriting the app's own.
            env={OPEN_SANDBOX_API_KEY_ENV: OPENSANDBOX_API_KEY} if OPENSANDBOX_API_KEY else None,
            pinned=PINNED_SANDBOX_TOOLS,
            dependency=SANDBOX_DEPENDENCY,
            # sandbox_session.py parses every reply as JSON and bounds the text that goes on to the model; cutting the JSON
            # here, in the middle, would make a large result unparseable instead of short.
            cap_output=False,
            capability_overrides={},  # explicit: never trust OpenSandbox's
            # own capability annotations (see module docstring).
            _timeout_seconds=_SANDBOX_LIST_TIMEOUT_SECONDS,
        )

    try:
        return await _OPENSANDBOX_BREAKER.call(
            _attempt,
            retry_on=(ConnectionError, TimeoutError),
            attempts=_OPENSANDBOX_RETRY_ATTEMPTS,
            base_delay=_OPENSANDBOX_RETRY_BASE_DELAY_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001 - a missing/hung bridge must degrade, never crash
        metrics.agent_sandbox_unavailable_total.labels(reason=_unavailable_reason(exc)).inc()
        logger.warning(
            "opensandbox_mcp_unavailable",
            extra={"error_class": type(exc).__name__, "domain": OPENSANDBOX_MCP_DOMAIN},
        )
        return [], {}
