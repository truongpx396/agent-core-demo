"""One audit line per call to a published MCP tool.

A stdio server is a separate process that nothing scrapes, so it cannot export a Prometheus metric the way the API and the
workers do (`app/core/metrics.py`); the signal it can give is a structured log line. `configure_logging()` writes JSON to
STDERR, which matters here: stdout is the protocol channel, and one stray line on it corrupts the session. The gateway
(`app/mcp/gateway.py`) writes the same line from a process that also exports metrics, so the line stays the one place a call is
attributed to a person.

What is logged is who (the tenant and principal the call acted under, from the server's `IdentitySource`), which tool, how it
ended and how long it took. Never an argument or a result: a name filter or an employee row is the kind of value a log must not
carry (constitution Principle VI), and never the access token that identified the caller.
"""
import functools
import logging
import time
from collections.abc import Awaitable, Callable

from app.mcp.identity import IdentityMissing, IdentitySource

logger = logging.getLogger(__name__)


def _outcome(result: object) -> str:
    """How a call that returned ended. The published tools answer in prose, so a refusal and a rejected filter are told
    apart by the prefix every one of them already uses."""
    text = result if isinstance(result, str) else ""
    if text.startswith("Refused"):
        return "refused"
    if text.startswith("Invalid"):
        return "invalid"
    return "ok"


def audited(server: str, identity: IdentitySource):
    """Decorate a published tool: time it and log `mcp_tool_call` however it ends, including by raising.

    `identity` is the same source the tool body asks, so the line names exactly who the call acted for (or `None` when it had no
    identity to act under and was refused).
    """

    def decorate(fn: Callable[..., Awaitable[str]]) -> Callable[..., Awaitable[str]]:
        @functools.wraps(fn)  # keeps the signature MCPServer builds the tool's input schema from
        async def wrapper(*args, **kwargs):
            started = time.perf_counter()
            outcome = "error"
            try:
                result = await fn(*args, **kwargs)
                outcome = _outcome(result)
                return result
            finally:
                try:
                    ctx = identity()
                    who = {"tenant": ctx["tenant"], "principal": ctx["principal"]}
                except IdentityMissing:
                    who = {"tenant": None, "principal": None}
                logger.info(
                    "mcp_tool_call",
                    extra={
                        "server": server,
                        "tool": fn.__name__,
                        "outcome": outcome,
                        "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                        **who,
                    },
                )

        return wrapper

    return decorate
