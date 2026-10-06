"""Cron-callable retention sweep for `usage_ledger`
(app/agent/usage_ledger.py, postgres-init/03-meter.sql).

Nothing ever trimmed that table (spec 008 A3), and the tenant allowance reads it
before every turn. This deletes rows older than `USAGE_LEDGER_RETENTION_DAYS`
(400 by default; the setting enforces a floor of 35 days so a calendar-month budget
window can never lose spend that is still inside it). Same "fixed pipeline, not an
agent turn" shape as scripts/tool_call_dedup_sweep.py — a plain batched DELETE.

Unlike that table, this one is a financial record, so this is deliberately NOT run
by anything automatically: wire it to real cron once your retention policy is
decided, e.g.

    30 3 * * 0 cd /path/to/agent-core-demo && python -m scripts.usage_ledger_sweep

If you need rows older than the window (audit, invoicing), export them first.
Idempotent: a re-run before more rows age out deletes nothing.
"""
import asyncio
import logging

from app.agent.usage_ledger import SWEEP_BATCH_SIZE, SWEEP_MAX_BATCHES, sweep_old_rows
from app.core.config import USAGE_LEDGER_RETENTION_DAYS
from app.core.job_runtime import scheduled_job

logger = logging.getLogger(__name__)


async def run_sweep(older_than_days: int = USAGE_LEDGER_RETENTION_DAYS) -> int:
    """Deletes every usage_ledger row older than `older_than_days` and returns the
    count (0 is a normal result on a young or quiet deployment). A value below the
    retention floor raises, from `sweep_old_rows`, before anything is deleted."""
    deleted = await sweep_old_rows(older_than_days=older_than_days)
    logger.info("usage_ledger_swept", extra={"deleted": deleted, "older_than_days": older_than_days})
    return deleted


def describe(count: int) -> str:
    """What the operator sees on stdout. A run that deleted its full ceiling probably left
    rows behind (`sweep_old_rows` stops there on purpose), and "Deleted 5000000" alone would
    read as "done"."""
    if not count:
        return "Nothing to sweep."
    message = f"Deleted {count} usage_ledger row(s)."
    if count >= SWEEP_MAX_BATCHES * SWEEP_BATCH_SIZE:
        message += " That is this run's ceiling, so older rows may remain: run it again."
    return message


def main() -> None:
    with scheduled_job("agent-core-usage-ledger-sweep"):
        print(describe(asyncio.run(run_sweep())))


if __name__ == "__main__":
    main()
