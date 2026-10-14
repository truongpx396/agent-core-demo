"""Cron-callable retention sweep for the package's usage events (credit_billing/retention.py, sql/24-billing-usage-events.sql).

Deletes events older than `BILLING_USAGE_EVENT_RETENTION_DAYS` (400 by default; the setting enforces a 35-day floor so the wallet
reconciliation or the export can never lose usage that is still inside its window). It never deletes an event whose export to a billing
provider did not finish: those are counted and reported, not removed. The wallet's debits are never touched.

Same "fixed pipeline, not an agent turn" shape as `credit-billing-inbox-sweep`, and like it deliberately NOT run by anything automatically:
wire it to real cron once your retention policy is decided, e.g.

    30 3 * * 0 credit-billing-usage-sweep        # or: python -m credit_billing.cli.usage_sweep

If you need rows older than the window (audit, invoicing), export them first. Idempotent: a re-run before more events age out deletes
nothing. Exits 3 with a one-line `error:` when the database cannot be reached.
"""
import asyncio
import logging
import sys

from credit_billing.cli._runtime import (
    EXIT_DATABASE_UNAVAILABLE,
    DatabaseUnavailable,
    scheduled_job,
    wired_pool,
)
from credit_billing.retention import (
    SWEEP_BATCH_SIZE,
    SWEEP_MAX_BATCHES,
    SweepResult,
    sweep_old_events,
)
from credit_billing.settings import BILLING_USAGE_EVENT_RETENTION_DAYS

logger = logging.getLogger(__name__)


async def run_sweep(older_than_days: int = BILLING_USAGE_EVENT_RETENTION_DAYS) -> SweepResult:
    """Deletes every event older than `older_than_days` that is safe to delete and returns what it did (0 is a normal result on a young or
    quiet deployment). A value below the retention floor raises, from `sweep_old_events`, before anything is deleted."""
    async with wired_pool():
        result = await sweep_old_events(older_than_days=older_than_days)
    logger.info(
        "billing_usage_swept",
        extra={"deleted": result.deleted, "outbox_cleared": result.outbox_cleared, "held_back": result.held_back, "older_than_days": older_than_days},
    )
    return result


def describe(result: SweepResult) -> str:
    """What the operator sees on stdout. A run that deleted its full ceiling probably left rows behind, and events kept because their
    export never finished are something a person should look at, so both are said."""
    lines = ["Nothing to sweep."] if not result.deleted else [f"Deleted {result.deleted} usage event(s)."]
    if result.outbox_cleared:
        lines.append(f"Cleared {result.outbox_cleared} finished export row(s) with them.")
    if not result.complete:
        lines.append(
            f"That is this run's ceiling ({SWEEP_MAX_BATCHES * SWEEP_BATCH_SIZE} events), so older events may remain: run it again."
        )
    if result.held_back:
        lines.append(
            f"KEPT {result.held_back} old event(s) whose export never finished (pending, failed or expired): "
            "resolve them rather than waiting for a sweep to hide them."
        )
    return " ".join(lines)


def main() -> None:
    with scheduled_job("credit-billing-usage-sweep"):
        try:
            print(describe(asyncio.run(run_sweep())))
        except DatabaseUnavailable as exc:
            print(f"error: {exc}", file=sys.stderr)
            raise SystemExit(EXIT_DATABASE_UNAVAILABLE) from exc


if __name__ == "__main__":
    main()
