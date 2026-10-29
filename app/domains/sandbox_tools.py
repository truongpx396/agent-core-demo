"""OpenSandbox (github.com/opensandbox-group/OpenSandbox) consumed over MCP (pattern 50) through the unmodified
`app/mcp/client.py::load_remote_tools` (pattern 28). `opensandbox-mcp` is a stdio MCP server bridging to a
containerized `opensandbox-server` (`make sandbox-up`), run as `python -m opensandbox_mcp ...
--use-server-proxy`. Since release 1.1 that flag is what a sandbox call needs: without it the call hangs 40+
seconds against a Docker-internal bridge IP the host can't reach.

Historical gotcha: sandbox-creation calls 405'd because `opensandbox_mcp_domain`'s old port (8080) collided
with `open-webui` in docker-compose.yml, so requests silently landed there. Fixed with port 8090 and a real
`--api-key`.

Why a real code-execution tool, unlike `calculator` (app/agent/tools/builtin.py): calculator is a narrow AST
evaluator because the app had nowhere safe to run arbitrary code. An isolated, disposable container is a real
boundary, so it is wired into the ops domain for investigations needing more than arithmetic (a percentile,
grepping a log dump, diffing configs).

Fail-soft, lazy: the catalog is served from static definitions, not the backend, so `load_sandbox_tools()`
returns near-instantly even with no `opensandbox-server` (only calling a tool needs it). Its caller,
`sandbox_session.py::load_raw_sandbox_tools`, calls it lazily and caches. It must degrade, never crash, if
opensandbox-mcp is missing or the bridge hangs: it runs under `arun_with_timeout`, catches everything, and
returns `([], {})` with a warning.

A connection-shaped failure (`ConnectionError`/`TimeoutError`: a bridge that couldn't yet reach the server, or
hung past `_SANDBOX_LIST_TIMEOUT_SECONDS`) gets one retry via `app/core/resilience.py::CircuitBreaker`, riding
out a server still booting. Any other exception (e.g. `FileNotFoundError`, package not installed) is never
retried. After enough consecutive exhausted retries `_OPENSANDBOX_BREAKER` fails calls fast until its
cooldown.

Disclosed gap: no SecurityCtx or tenant scoping, as for any remote MCP tool (MCP has no `RunnableConfig`
channel). Acceptable because a code sandbox is a shared operational resource, not tenant data; the
mandatory-approval gate (every tool forced to "outward") is the actual boundary.
"""
import logging
import sys

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

# The packaged opensandbox-mcp CLI, run under this same interpreter so the same installed opensandbox-mcp / opensandbox packages
# are guaranteed. `--use-server-proxy` routes sandbox traffic through opensandbox-server instead of straight to a Docker-internal
# bridge IP this host process cannot reach (opensandbox-mcp<=0.1.1 had no such flag, which is why a wrapper script existed).
# `--transport stdio` is the CLI's default, said out loud: the bridge is a child of this process, holds the key in its environment
# and listens on no port, so it is not a network service that holds a code-execution key (GRAPH_PATTERNS.md pattern 78).
_BRIDGE_ARGS = ["-m", "opensandbox_mcp", "--transport", "stdio", "--protocol", "http", "--use-server-proxy"]

# The variable the OpenSandbox SDK falls back to when no `--api-key` is given (opensandbox.config.ConnectionConfig).
OPEN_SANDBOX_API_KEY_ENV = "OPEN_SANDBOX_API_KEY"

# The five of the bridge's 19 tools this app uses (sandbox_session.py), each with the fingerprint of its name, description and
# input schema as `opensandbox-mcp==1.1.1` offers it (app/mcp/client.py::tool_fingerprint). The client keeps exactly these and
# only while they still match: the other fourteen (kill, renew, move, delete, replace, ...) are never offered to the model, and
# a release that rewrites one of these five's description, which is text the model obeys, is left out and counted until a
# person reads the change and updates the hash. tests/domains/test_sandbox_tool_pins.py fails on the same drift in CI, and
# prints the new values, so this is a two-line change with the diff of the bridge's descriptions next to it.
PINNED_SANDBOX_TOOLS = {
    "command_run": "a85c41d3e5e85d5ce01c32baa41708a66c953dc4912bab907227242dce177a5e",
    "file_read": "0fc75b8d353bda9cc1da0ea57163bdc73df48feca09ced886942853e22f80ca9",
    "file_write": "ea0a02e9633235d55615f4e5bfee1a698786f589b5cb514bdf5fd1ab0c67776e",
    "sandbox_create": "a47b3abdc4072394dd95531b9535516003f4ae40ff0d3e3737302fc4d46de807",
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
            args=[*_BRIDGE_ARGS, "--domain", OPENSANDBOX_MCP_DOMAIN],
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
