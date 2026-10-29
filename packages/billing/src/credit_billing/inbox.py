"""Retention for the webhook inbox (postgres-init/22-billing.sql).

The inbox is the audit trail of what a payment provider told the app, so it is kept long (400 days by default)
but must not grow forever: this deletes `applied` and `ignored` rows older than
`BILLING_INBOX_RETENTION_DAYS`. It NEVER deletes a row that is still an open question (`received`, `failed`,
`quarantined`), however old.

Deleting an applied row cannot make a late redelivery grant twice: the grant's own key
`"{provider}:{event_id}"` in `credit_transactions` is part of the wallet's permanent record, so the redelivery
inserts a fresh inbox row and finds its grant already made (proved against real Postgres in
tests/integration/test_webhooks_real_postgres.py). The floor below is about not racing a provider's retries,
not correctness.

An operator job (`cli/inbox_sweep.py`), never reachable from a request. Like the usage events' sweep
(`scripts/usage_events_sweep.py`) it spans tenants, because retention is a property of the table. Bounded: a
run deletes at most `SWEEP_MAX_BATCHES * SWEEP_BATCH_SIZE` rows and is idempotent, so the next run continues.
"""
import logging

from credit_billing.db import get_connection
from credit_billing.settings import BILLING_INBOX_MIN_RETENTION_DAYS

logger = logging.getLogger(__name__)

SWEEP_BATCH_SIZE = 5000
SWEEP_MAX_BATCHES = 1000


async def sweep_old_rows(
    *, older_than_days: int, batch_size: int = SWEEP_BATCH_SIZE, max_batches: int = SWEEP_MAX_BATCHES
) -> int:
    """Deletes finished (`applied`/`ignored`) inbox rows received more than `older_than_days` ago and
    returns how many. A value below the floor raises before a row is touched, so the floor holds for every
    caller and not only the script."""
    if older_than_days < BILLING_INBOX_MIN_RETENTION_DAYS:
        raise ValueError(
            f"older_than_days={older_than_days} is below the {BILLING_INBOX_MIN_RETENTION_DAYS}-day floor: "
            "a sweep must not race a payment provider that is still redelivering"
        )
    total = 0
    for _ in range(max_batches):
        async with get_connection() as conn:
            cur = await conn.execute(
                "DELETE FROM billing_webhook_events WHERE (provider, event_id) IN ("
                "SELECT provider, event_id FROM billing_webhook_events "
                "WHERE status IN ('applied', 'ignored') AND received_at < now() - make_interval(days => %s) LIMIT %s)",
                (older_than_days, batch_size),
            )
            deleted = cur.rowcount
        total += deleted
        if deleted < batch_size:
            return total
    logger.warning(
        "billing_inbox_sweep_hit_batch_ceiling", extra={"deleted": total, "max_batches": max_batches, "batch_size": batch_size}
    )
    return total
