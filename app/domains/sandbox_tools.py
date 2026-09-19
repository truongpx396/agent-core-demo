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

## Why a real code-execution tool, unlike app/agent/tools.py::calculator
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
bridge hangs: wrapped in `_arun_with_timeout`, all exceptions caught,
degrading to `([], {})` with a logged warning.

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

from app.agent.tools import _arun_with_timeout
from app.core.config import OPENSANDBOX_API_KEY, OPENSANDBOX_MCP_DOMAIN
from app.mcp import client as mcp_client

logger = logging.getLogger(__name__)

_SANDBOX_LIST_TIMEOUT_SECONDS = 10  # backstop against a hung bridge
# process; catalog listing is normally near-instant (see module docstring).

# scripts/opensandbox_mcp_bridge.py, not the packaged opensandbox-mcp CLI
# directly: that CLI can't set ConnectionConfig(use_server_proxy=True),
# needed for this host process to reach sandboxes on the Docker bridge
# network. Runs under this same interpreter, so the same installed
# opensandbox-mcp/opensandbox packages are guaranteed available.
_BRIDGE_SCRIPT = str(Path(__file__).resolve().parent.parent.parent / "scripts" / "opensandbox_mcp_bridge.py")


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
    try:
        return await _arun_with_timeout(
            mcp_client.load_remote_tools,
            command=sys.executable,
            args=[
                _BRIDGE_SCRIPT,
                "--domain", OPENSANDBOX_MCP_DOMAIN, "--protocol", "http",
                "--api-key", OPENSANDBOX_API_KEY,
            ],
            capability_overrides={},  # explicit: never trust OpenSandbox's
            # own capability annotations (see module docstring).
            _timeout_seconds=_SANDBOX_LIST_TIMEOUT_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001 - a missing/hung bridge must degrade, never crash
        logger.warning(
            "opensandbox_mcp_unavailable",
            extra={"error_class": type(exc).__name__, "domain": OPENSANDBOX_MCP_DOMAIN},
        )
        return [], {}
