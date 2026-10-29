"""What every tool needs to run safely: a bounded wait, a scrubbed result, and a context that fails closed.

Every tool in this app, the built-ins in `app/agent/tools/builtin.py` and each domain's in `app/domains/*/tools.py`, funnels through these. They used to live in
`builtin.py` and the domains imported them by their private names; none of them depends on the agent, so they are here, public.

  * `arun_with_timeout` / `run_with_timeout`: stop waiting after `TOOL_TIMEOUT_SECONDS` and scrub the result (app/core/scrubbing.py, GRAPH_PATTERNS.md pattern 32), the one
    chokepoint a tool's output passes through on its way to the model or a trace;
  * `ctx_or_refuse` / `ctx_from_config`: the fail-closed check every ctx-aware tool makes first (constitution Principle I), and `NO_CTX_REFUSAL`, the text it returns.
"""
import asyncio
import concurrent.futures

from langchain_core.runnables import RunnableConfig

from app.core.scrubbing import scrub
from app.core.security import DEFAULT_POLICY, SecurityCtx, valid_ctx

# Safety budget: bounds how long one tool call can block the graph (a hung
# Qdrant call, or a pathological expression like `2**99999999999`, could
# otherwise stall — or exhaust memory — the turn indefinitely).
TOOL_TIMEOUT_SECONDS = 15

_TOOL_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=8, thread_name_prefix="tool-timeout"
)


async def arun_with_timeout(func, *args, _timeout_seconds: float | None = None, **kwargs):
    """Await `func(*args, **kwargs)` and stop waiting after `_timeout_seconds` (default TOOL_TIMEOUT_SECONDS).
    The raised TimeoutError is caught by ToolNode's `handle_tool_errors=friendly_tool_error`
    (app/agent/graph/core.py) and surfaced to the agent like any tool exception.

    Every tool uses this (not `run_with_timeout` below), wrapping a real coroutine with `asyncio.wait_for`,
    since every tool impl awaits an async client. `run_with_timeout` remains for the one caller with no
    running event loop (see its docstring).

    `_timeout_seconds` is keyword-only and resolved per call rather than a plain default: a `=
    TOOL_TIMEOUT_SECONDS` default binds at definition time and would stop honoring `monkeypatch.setattr(tools,
    "TOOL_TIMEOUT_SECONDS", ...)` (caught by tests/agent/graph/test_safety_budgets.py). `run_subagent` is the
    one caller that overrides it, to SUBAGENT_TIMEOUT_SECONDS.

    Soft timeout: `asyncio.wait_for` cancels the awaiting task, not necessarily whatever `func` awaits
    underneath. Cancellation only takes effect at `func`'s next `await`, so a call stuck in a non-cancellable
    operation can outlive this timeout in the background.

    The result is scrubbed (app/core/scrubbing.py, GRAPH_PATTERNS.md pattern 32) before returning: the one
    chokepoint every tool impl funnels through, so secrets in a raw result never reach the model or a trace.
    `run_subagent` returns a state dict here (scrubbing skips non-str results) and scrubs its extracted answer
    text separately.
    """
    timeout = _timeout_seconds if _timeout_seconds is not None else TOOL_TIMEOUT_SECONDS
    try:
        result = await asyncio.wait_for(func(*args, **kwargs), timeout=timeout)
    except TimeoutError as exc:
        raise TimeoutError(f"Tool call exceeded the {timeout}s timeout.") from exc
    return scrub(result) if isinstance(result, str) else result



def run_with_timeout(func, *args, _timeout_seconds: float | None = None, **kwargs):
    """The sync survivor of `arun_with_timeout`'s predecessor: still runs
    `func` in a worker thread via `concurrent.futures`, not `asyncio.wait_for`
    — its one caller, `app/adapters/mcp/sandbox_tools.py::load_sandbox_tools`, runs
    at plain Python import time (eager domain composition), before any event
    loop exists. `mcp_client.load_remote_tools` (what it wraps) opens its own
    throwaway loop via `asyncio.run(...)` internally; this dispatches that
    whole call to a worker thread bounded by `future.result(timeout=...)`.
    Every other caller has moved to `arun_with_timeout` — keep this one only
    for callers with no running loop, not as a general alternative.

    Same soft-timeout caveat (Python can't forcibly kill the worker thread)
    and same scrub() chokepoint as `arun_with_timeout`.
    """
    timeout = _timeout_seconds if _timeout_seconds is not None else TOOL_TIMEOUT_SECONDS
    future = _TOOL_EXECUTOR.submit(func, *args, **kwargs)
    try:
        result = future.result(timeout=timeout)
    except concurrent.futures.TimeoutError as exc:
        raise TimeoutError(f"Tool call exceeded the {timeout}s timeout.") from exc
    return scrub(result) if isinstance(result, str) else result



NO_CTX_REFUSAL = (
    "Refused: no valid tenant/principal context for this request. "
    "This isn't something you can work around — it means the request "
    "never got a security context stamped on it upstream."
)


def ctx_from_config(config: RunnableConfig | None) -> SecurityCtx | None:
    if not config:
        return None
    return config.get("configurable", {}).get("ctx")


def ctx_or_refuse(config: RunnableConfig | None, action: str) -> SecurityCtx | None:
    """Fail-closed check every ctx-aware tool makes first: missing/malformed
    ctx, or a ctx Policy doesn't `permit` for `action`, returns None (caller
    returns NO_CTX_REFUSAL) instead of running an unscoped query or
    untenanted write. Never raises — a refusal is a normal ToolMessage, not
    an exception path (handle_tool_errors is for actual failures)."""
    ctx = ctx_from_config(config)
    if not valid_ctx(ctx) or not DEFAULT_POLICY.permit(action, ctx):
        return None
    return ctx

