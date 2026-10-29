"""Retention for the usage events: the sweep spec D7 promised and nothing built (specs/010 T030c3).

`usage_events` is append-only and, since the caps sum it (T030b), grows with every model call; nothing trimmed
it, so a year of calls was a year of rows read by the reconciliation and scanned by monthly caps. This deletes
events older than `USAGE_EVENT_RETENTION_DAYS`.

## What it must not delete

  * **An event inside a window something still reads.** A monthly cap reads back a calendar month (31 days)
    and the gateway reconciliation up to `USAGE_RECONCILE_LOOKBACK_DAYS` (35). The floor is 35 days
    (`USAGE_EVENT_MIN_RETENTION_DAYS`), and a smaller `older_than_days` raises BEFORE any row is touched, so
    the floor holds for every caller, not just the script.
  * Anything younger than the cutoff, in any tenant. Retention is a property of the table, so the sweep spans
    tenants on purpose; the job is an operator's, never reachable from a request.

Billing keeps its own record with its own retention (`credit-billing-usage-sweep`); deleting an event here
never touches it, and the wallet's debits are permanent either way.

## How the delete is allowed

The table refuses DELETE unless the transaction says so (`usage_events.allow_delete`, postgres-init/19).
`set_config(..., true)` is LOCAL to the transaction, so it can't be left on, and each batch commits on its own
so a sweep never holds a long lock.
"""
import logging
from dataclasses import dataclass

from app.core.config import USAGE_EVENT_MIN_RETENTION_DAYS
from app.ports import cost as ports

logger = logging.getLogger(__name__)

# Rows go in batches so one sweep never holds a long lock or one huge transaction on a table that has grown for a year.
SWEEP_BATCH_SIZE = 5000
# 1,000 batches is 5 million rows at the default size. The app never writes an event older than the cutoff, so a sweep ends at the
# first short batch; the ceiling is for the day something does (a restore, a backfill), so one run still has a bounded duration.
SWEEP_MAX_BATCHES = 1000

@dataclass(frozen=True)
class SweepResult:
    deleted: int  # events removed
    complete: bool  # False when the per-run ceiling stopped it: run it again


async def sweep_old_events(
    *, older_than_days: int, batch_size: int = SWEEP_BATCH_SIZE, max_batches: int = SWEEP_MAX_BATCHES
) -> SweepResult:
    """Deletes events older than `older_than_days`. Raises `ValueError` for fewer days than the floor, before a row is touched.
    Idempotent: a re-run before more events age out deletes nothing."""
    if older_than_days < USAGE_EVENT_MIN_RETENTION_DAYS:
        raise ValueError(
            f"older_than_days={older_than_days} is below the {USAGE_EVENT_MIN_RETENTION_DAYS}-day floor: "
            "a monthly cap or the reconciliation would stop seeing spend that is still inside its window"
        )
    deleted = 0
    complete = False
    for _ in range(max_batches):
        gone = await _batch(older_than_days, batch_size)
        deleted += gone
        if gone < batch_size:
            complete = True
            break
    if not complete:
        logger.warning("usage_events_sweep_hit_batch_ceiling", extra={"deleted": deleted, "max_batches": max_batches, "batch_size": batch_size})
    return SweepResult(deleted, complete)


async def _batch(days: int, n: int) -> int:
    return await ports.usage_events.get().delete_older_than(days=days, limit=n)
