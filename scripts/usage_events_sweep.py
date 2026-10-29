"""Cron-callable retention sweep for the usage events (app/agent/cost/usage_events_retention.py,
postgres-init/19-usage-events.sql). Nothing trimmed that table (spec D7 promised a job), and since specs/010
T030b the dollar caps and reconciliation read it. This deletes events older than `USAGE_EVENT_RETENTION_DAYS`
(400 by default; the setting enforces a floor of 35 days so a calendar-month cap or reconciliation window
never loses spend still inside it). Billing keeps its own record with its own retention
(`credit-billing-usage-sweep`); nothing here touches it.

Like the sweeps before it (`python -m credit_billing.cli.inbox_sweep`), a fixed pipeline, deliberately NOT run
automatically: spend is read from this table, so wire it to cron once your retention policy is decided, e.g.
`30 3 * * 0 cd /path/to/agent-core-demo && python -m scripts.usage_events_sweep`. Export older rows first if
you need them (audit, invoicing). A re-run before more events age out deletes nothing.
"""
import asyncio
import logging

from app.agent.cost.usage_events_retention import (
    SWEEP_BATCH_SIZE,
    SWEEP_MAX_BATCHES,
    SweepResult,
    sweep_old_events,
)
from app.core.config import USAGE_EVENT_RETENTION_DAYS
from app.core.job_runtime import scheduled_job

logger = logging.getLogger(__name__)


async def run_sweep(older_than_days: int = USAGE_EVENT_RETENTION_DAYS) -> SweepResult:
    """Deletes every event older than `older_than_days` that is safe to delete and returns what it did (0 is a normal result on a
    young or quiet deployment). A value below the retention floor raises, from `sweep_old_events`, before anything is deleted."""
    result = await sweep_old_events(older_than_days=older_than_days)
    logger.info(
        "usage_events_swept",
        extra={"deleted": result.deleted, "older_than_days": older_than_days},
    )
    return result


def describe(result: SweepResult) -> str:
    """What the operator sees on stdout. A run that deleted its full ceiling probably left rows behind, so that is said."""
    lines = ["Nothing to sweep."] if not result.deleted else [f"Deleted {result.deleted} usage event(s)."]
    if not result.complete:
        lines.append(
            f"That is this run's ceiling ({SWEEP_MAX_BATCHES * SWEEP_BATCH_SIZE} events), so older events may remain: run it again."
        )
    return " ".join(lines)


def main() -> None:
    with scheduled_job("agent-core-usage-events-sweep"):
        print(describe(asyncio.run(run_sweep())))


if __name__ == "__main__":
    main()
