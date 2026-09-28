"""Cron-callable retention sweep for `tool_call_dedup`
(app/agent/tool_idempotency.py, postgres-init/13-tool-call-dedup.sql).

That table has no retention of its own: it exists purely as a short-lived
crash-recovery claim (see its own docstring — a genuine replay only ever
lags the original attempt by `AGENT_WORKER_RECLAIM_IDLE_SECONDS`, a few
minutes), but nothing ever deletes from it. Left alone, every
mutating/outward tool call this app ever makes accumulates here forever,
full result text included. This script is the fix, same "fixed pipeline,
not an agent turn" shape as scripts/followup_sweep.py/ops_digest.py — a
plain DELETE, no LLM involved.

Meant to be wired to real cron, e.g.:

    0 3 * * * cd /path/to/agent-core-demo && python -m scripts.tool_call_dedup_sweep

Idempotent and safe to run as often as you like — a re-run before the next
row goes stale just deletes nothing.
"""
import asyncio
import logging

from app.agent.tool_idempotency import sweep_stale_rows
from app.core.logging_config import configure_logging

logger = logging.getLogger(__name__)

# Comfortably past AGENT_WORKER_RECLAIM_IDLE_SECONDS (240s default) and
# MAX_AUTO_RECLAIM_RETRIES's own retry window — this only has to survive
# every LEGITIMATE crash-recovery replay, not double as an audit log. 24h
# also leaves a full day's worth of dedup hits inspectable by an operator
# between sweeps.
DEFAULT_RETENTION_HOURS = 24


async def run_sweep(older_than_hours: int = DEFAULT_RETENTION_HOURS) -> int:
    """Deletes every tool_call_dedup row older than `older_than_hours`.
    Returns the number of rows deleted (0 is a normal, healthy result on a
    quiet deployment — never treated as an error)."""
    deleted = await sweep_stale_rows(older_than_hours=older_than_hours)
    logger.info("tool_call_dedup_swept", extra={"deleted": deleted, "older_than_hours": older_than_hours})
    return deleted


if __name__ == "__main__":
    configure_logging()
    count = asyncio.run(run_sweep())
    print(f"Deleted {count} stale tool_call_dedup row(s)." if count else "Nothing to sweep.")
