"""Retention for the package's usage events (sql/24-billing-usage-events.sql; the sibling of the webhook inbox's, `inbox.py`).

`billing_usage_events` is append-only and grows with every recorded unit of usage. This deletes events older than
`BILLING_USAGE_EVENT_RETENTION_DAYS` (400 by default).

## What it must not delete

  * **An event inside a window something still reads.** The wallet reconciliation reads back up to `CREDIT_RECONCILE_LOOKBACK_DAYS` (35 at
    most) and the export outbox gives up on an unsent event at 30. The floor is 35 days (`BILLING_USAGE_EVENT_MIN_RETENTION_DAYS`) and a smaller
    `older_than_days` raises BEFORE any row is touched, so the floor holds for every caller and not just the command.
  * **An event whose export never finished** (`billing_export_outbox.status` of `pending`, `failed` or `expired`). Those are usage that
    was meant to reach a provider and did not: lost revenue with an alert on it. Deleting the evidence because it is old would turn a thing
    someone has not resolved into a thing nobody can see, so they are kept and counted (`held_back`). `sent` is the only finished state
    that is done: those events are deleted together with their outbox rows, because the foreign key would refuse the delete otherwise (that is
    the guard the script describes) and both go in ONE statement, since the constraint is checked at the end of it.
  * Anything younger than the cutoff, in any tenant. Like the inbox sweep this deliberately spans tenants: retention is a property of the
    table, and the job is an operator's, never reachable from a request.

## What deleting an event does NOT touch

The wallet. A `credit_transactions` row keeps the `usage_event_id` it was charged for as lineage, but there is no foreign key from it and
the wallet is a permanent financial record: the debit stays, and so does the balance. What goes with the event is the ability to recompute
that debit's credits from the event row, so keep events at least as long as you may need to explain a charge.

## How the delete is allowed

The table refuses DELETE unless the transaction says so (`billing_usage_events.allow_delete`). `set_config(..., true)` is LOCAL to the
transaction, so it cannot be left switched on, and each batch commits on its own so one sweep never holds a long lock.

Imports nothing from `app` (enforced by tests/test_boundary.py).
"""
import logging
from dataclasses import dataclass

from credit_billing.db import get_connection
from credit_billing.settings import BILLING_USAGE_EVENT_MIN_RETENTION_DAYS

logger = logging.getLogger(__name__)

# Rows go in batches so one sweep never holds a long lock or one huge transaction on a table that has grown for a year.
SWEEP_BATCH_SIZE = 5000
# 1,000 batches is 5 million rows at the default size. Nothing writes an event older than the cutoff, so a sweep ends at the first short
# batch; the ceiling is for the day something does (a restore, a backfill), so one run still has a bounded duration.
SWEEP_MAX_BATCHES = 1000

_ALLOW_DELETE = "SELECT set_config('billing_usage_events.allow_delete', 'on', true)"

# No ORDER BY, on purpose: events are inserted in time order, so the oldest sit first in the heap and the scan finds a full batch quickly,
# whereas sorting a year of rows to delete the oldest few thousand would cost more than the delete.
_BATCH = """
WITH doomed AS (
    SELECT e.tenant, e.event_id FROM billing_usage_events e
    WHERE e.occurred_at < now() - make_interval(days => %(days)s)
      AND NOT EXISTS (SELECT 1 FROM billing_export_outbox o WHERE o.tenant = e.tenant AND o.event_id = e.event_id AND o.status <> 'sent')
    LIMIT %(n)s
), cleared AS (
    DELETE FROM billing_export_outbox o USING doomed d WHERE o.tenant = d.tenant AND o.event_id = d.event_id RETURNING 1
), gone AS (
    DELETE FROM billing_usage_events e USING doomed d WHERE e.tenant = d.tenant AND e.event_id = d.event_id RETURNING 1
)
SELECT (SELECT count(*) FROM gone), (SELECT count(*) FROM cleared)
"""

_HELD_BACK = """
SELECT count(*) FROM billing_usage_events e
WHERE e.occurred_at < now() - make_interval(days => %(days)s)
  AND EXISTS (SELECT 1 FROM billing_export_outbox o WHERE o.tenant = e.tenant AND o.event_id = e.event_id AND o.status <> 'sent')
"""


@dataclass(frozen=True)
class SweepResult:
    deleted: int  # events removed
    outbox_cleared: int  # finished (`sent`) export rows removed with them
    held_back: int  # events past the cutoff that were KEPT because their export never finished
    complete: bool  # False when the per-run ceiling stopped it: run it again


async def sweep_old_events(
    *, older_than_days: int, batch_size: int = SWEEP_BATCH_SIZE, max_batches: int = SWEEP_MAX_BATCHES
) -> SweepResult:
    """Deletes events older than `older_than_days`, never one whose export is unfinished. Raises `ValueError` for fewer days than the floor,
    before a row is touched. Idempotent: a re-run before more events age out deletes nothing."""
    if older_than_days < BILLING_USAGE_EVENT_MIN_RETENTION_DAYS:
        raise ValueError(
            f"older_than_days={older_than_days} is below the {BILLING_USAGE_EVENT_MIN_RETENTION_DAYS}-day floor: "
            "the wallet reconciliation or the export would stop seeing usage that is still inside its window"
        )
    deleted = cleared = 0
    complete = False
    for _ in range(max_batches):
        gone, outbox = await _batch(older_than_days, batch_size)
        deleted += gone
        cleared += outbox
        if gone < batch_size:
            complete = True
            break
    if not complete:
        logger.warning("billing_usage_sweep_hit_batch_ceiling", extra={"deleted": deleted, "max_batches": max_batches, "batch_size": batch_size})
    return SweepResult(deleted, cleared, await _held_back(older_than_days), complete)


async def _batch(days: int, n: int) -> tuple[int, int]:
    async with get_connection() as conn:
        await conn.execute(_ALLOW_DELETE)
        cur = await conn.execute(_BATCH, {"days": days, "n": n})
        row = await cur.fetchone()
    assert row is not None  # an aggregate always returns a row
    return int(row[0]), int(row[1])


async def _held_back(days: int) -> int:
    async with get_connection() as conn:
        cur = await conn.execute(_HELD_BACK, {"days": days})
        row = await cur.fetchone()
    assert row is not None
    return int(row[0])
