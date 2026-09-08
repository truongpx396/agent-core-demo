"""OpenSandbox (https://github.com/opensandbox-group/OpenSandbox) consumed
over MCP — GRAPH_PATTERNS.md pattern 50, built entirely on the EXISTING,
unmodified `app/mcp/client.py::load_remote_tools` (pattern 28). No new MCP
CLIENT plumbing: `opensandbox-mcp` is a real, purpose-built stdio MCP
server that bridges to a containerized `opensandbox-server` (`make
sandbox-up`, docker-compose's opt-in `sandbox` profile — `docker/
opensandbox-server.{Dockerfile,toml}`). What IS new: `scripts/
opensandbox_mcp_bridge.py`, a thin wrapper this module spawns INSTEAD of
the packaged `opensandbox-mcp` binary directly — necessary because that
package's own CLI has no way to set `ConnectionConfig(use_server_proxy=
True)`, which containerizing opensandbox-server made a hard requirement
(that script's own docstring has the full, verified finding: without it, a
real sandbox call hangs 40+ seconds trying to reach a Docker-internal
bridge IP unreachable from this host process). This module is otherwise
just the (small) wiring that turns the remote tool catalog into something
a domain's `DomainPlugin` can merge in.

Verified empirically against a real, locally-run `opensandbox-server` +
`opensandbox-mcp` (not written blind): `load_remote_tools` correctly lists
its full 19-tool catalog (`sandbox_create`, `command_run`, `file_read`/
`file_write`, ...) and, with no `capability_overrides` supplied at all,
already defaults every one of them to `"outward"` — this module still
passes an explicit empty override dict below, so that fail-closed default
is visible here rather than an implicit fact a reader has to already know
about `load_remote_tools`. The actual sandbox-creation call itself hit a
`405` against a locally `uvx`-launched server, reproduced identically via
OpenSandbox's own official `osb` CLI too — at first this looked like a
server-side/environment quirk, but was later root-caused to a real bug in
THIS app's own old default: `opensandbox_mcp_domain`'s port (8080)
collided with `docker-compose.yml`'s `open-webui` service, so every
request was silently landing on open-webui's uvicorn instead of
OpenSandbox at all (see app/core/config.py's `opensandbox_mcp_domain`
comment, GRAPH_PATTERNS.md pattern 50). Fixed by moving to port 8090 and,
separately, wiring real `--api-key` auth below (`opensandbox_api_key`) —
the tool-discovery/dispatch round trip this module depends on was never
the broken part.

## Why a real code-execution tool, when app/agent/tools.py::calculator
## deliberately is NOT one

`calculator`'s whole design point (see its own module docstring in
app/agent/tools.py) is a narrow, whitelisted AST evaluator specifically
BECAUSE this app has nowhere safe to run arbitrary code — `eval()` in the
agent's own process would be a real sandbox escape waiting to happen.
OpenSandbox is what changes that calculus: a genuinely isolated, disposable
container is a real security boundary `calculator`'s AST walk never had to
be. That's why this is wired into the ops domain (app/domains/ops/domain.py)
rather than expanding calculator's own scope — real use case: an
investigation needs computation beyond arithmetic (recompute a percentile
from raw metric readings, grep a pasted log dump for an error signature,
diff two JSON configs) and the ops bot writes a short script and runs it
inside a sandbox instead.

## Fail-soft loading, called eagerly like every other domain composition step

Verified empirically: `opensandbox-mcp`'s tool CATALOG is served from its
own static tool definitions, not proxied to the backend sandbox server —
`load_sandbox_tools()` returns the full tool list in about a second even
with NO `opensandbox-server` running at all (only actually CALLING a tool
like `sandbox_create` needs the backend reachable, and that failure surfaces
normally through app/agent/graph.py's handle_tool_errors like any other tool
error). So this is called eagerly from `_OpsDomainPlugin.tools()`
(app/domains/ops/domain.py), the exact same shape every other eager
domain-composition step already uses (e.g. app/agent/tools.py's
`_SUBAGENT_REGISTRY`) — no special-casing needed.

What still MUST degrade rather than crash: `opensandbox-mcp` not being
installed/on PATH at all (a real, legitimate case — not every deployment
of this app wants the sandbox feature), or the bridge process hanging
instead of failing fast. `load_sandbox_tools()` wraps the connection
attempt in `app/agent/tools.py::_run_with_timeout` (a bounded budget, same
tool this module's own sibling domain `tools.py` files already reuse for
their own timeout needs) and catches every exception, degrading to
`([], {})` with a logged warning — mirroring
app/agent/tools.py::make_domain_subagent_tool's "resolves to nothing
usable, not a crash" contract for the analogous case (an AGENT.md
declaring tools this domain doesn't actually have).

## Disclosed gap: no SecurityCtx, no tenant scoping

Same limitation app/mcp/client.py's own docstring already names for any
remote MCP tool: there is no `RunnableConfig`/`SecurityCtx` channel over
MCP, so these tools carry no tenant/principal scoping at all. That's an
acceptable gap here specifically because a code sandbox is a shared
OPERATIONAL resource, not tenant data — the mandatory-approval capability
gate (every tool forced to "outward" below) is the actual safety boundary,
not per-tenant isolation.
"""
import logging
import sys
from pathlib import Path

from langchain_core.tools import BaseTool

from app.agent.tools import _run_with_timeout
from app.core.config import OPENSANDBOX_API_KEY, OPENSANDBOX_MCP_DOMAIN
from app.mcp import client as mcp_client

logger = logging.getLogger(__name__)

_SANDBOX_LIST_TIMEOUT_SECONDS = 10  # bounds the one-time catalog-listing
# connection attempt this module makes — normally near-instant (see module
# docstring), this is a backstop against a hung bridge process, not the
# expected path.

# scripts/opensandbox_mcp_bridge.py, NOT the packaged `opensandbox-mcp` CLI
# directly — verified directly (a real Sandbox.create() call hung 44+
# seconds against the containerized opensandbox-server, see that script's
# own docstring for the full finding) that opensandbox-mcp==0.1.1's CLI has
# no way to set ConnectionConfig(use_server_proxy=True), which THIS
# process's own deployment topology (a host process talking to sandboxes
# that live on a Docker bridge network) genuinely needs. Same interpreter
# this process is already running under, so the same installed
# opensandbox-mcp/opensandbox packages are guaranteed available.
_BRIDGE_SCRIPT = str(Path(__file__).resolve().parent.parent.parent / "scripts" / "opensandbox_mcp_bridge.py")


def load_sandbox_tools() -> tuple[list[BaseTool], dict[str, str]]:
    """Connects to `opensandbox-mcp` (the stdio bridge; the containerized
    `opensandbox-server`, `make sandbox-up`, is only needed once a tool is
    actually CALLED, not for this listing step — see module docstring) and
    returns its tool catalog as LangChain tools, every one of them capped at
    `"outward"`. Degrades to `([], {})` with a logged warning — never raises
    — if the bridge isn't installed/reachable or hangs past
    `_SANDBOX_LIST_TIMEOUT_SECONDS`, so a domain merging this in still
    builds and runs with every OTHER tool intact.
    """
    try:
        return _run_with_timeout(
            mcp_client.load_remote_tools,
            command=sys.executable,
            args=[
                _BRIDGE_SCRIPT,
                "--domain", OPENSANDBOX_MCP_DOMAIN, "--protocol", "http",
                "--api-key", OPENSANDBOX_API_KEY,
            ],
            capability_overrides={},  # explicit: every tool defaults to
            # "outward" (see module docstring) — never trust OpenSandbox's
            # own annotations, same reasoning app/mcp/client.py always applies.
            _timeout_seconds=_SANDBOX_LIST_TIMEOUT_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001 - a missing/hung bridge must
        # degrade this domain's tool list, never crash its import or its
        # build_graph() call (see module docstring).
        logger.warning(
            "opensandbox_mcp_unavailable",
            extra={"error_class": type(exc).__name__, "domain": OPENSANDBOX_MCP_DOMAIN},
        )
        return [], {}
