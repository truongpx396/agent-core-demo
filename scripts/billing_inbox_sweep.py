"""Cron-callable retention sweep for the billing webhook inbox
(app/billing/inbox.py, postgres-init/22-billing.sql).

Deletes `applied` and `ignored` rows older than `BILLING_INBOX_RETENTION_DAYS` (400 by default; the setting
enforces a 30-day floor so a sweep never races a provider's retries). It never touches a row that is still
open (`received`, `failed`, `quarantined`). Same "fixed pipeline, not an agent turn" shape as
scripts/usage_events_sweep.py, and like that one it is deliberately NOT run by anything automatically: wire it to
real cron once your retention policy is decided, e.g.

    45 3 * * 0 cd /path/to/agent-core-demo && python -m scripts.billing_inbox_sweep

Idempotent: a re-run before more rows age out deletes nothing.
"""
import asyncio
import logging

from app.billing.inbox import SWEEP_BATCH_SIZE, SWEEP_MAX_BATCHES, sweep_old_rows
from app.core.config import BILLING_INBOX_RETENTION_DAYS
from app.core.job_runtime import scheduled_job

logger = logging.getLogger(__name__)


async def run_sweep(older_than_days: int = BILLING_INBOX_RETENTION_DAYS) -> int:
    deleted = await sweep_old_rows(older_than_days=older_than_days)
    logger.info("billing_inbox_swept", extra={"deleted": deleted, "older_than_days": older_than_days})
    return deleted


def describe(count: int) -> str:
    """What the operator sees on stdout. A run that deleted its full ceiling probably left rows behind."""
    if not count:
        return "Nothing to sweep."
    message = f"Deleted {count} webhook inbox row(s)."
    if count >= SWEEP_MAX_BATCHES * SWEEP_BATCH_SIZE:
        message += " That is this run's ceiling, so older rows may remain: run it again."
    return message


def main() -> None:
    with scheduled_job("agent-core-billing-inbox-sweep"):
        print(describe(asyncio.run(run_sweep())))


if __name__ == "__main__":
    main()
