"""One-time carry-over of the usage ledger's history into the usage events (specs/010 T030).

The dollar caps and `GET /usage` are about to sum `usage_events` instead of `usage_ledger` (the next change, specs/010 T030b), and that
table only has rows from the day postgres-init/19 was applied. Without this, a monthly cap would then forget everything spent earlier in
the month and `/usage` would report a fraction of the all-time total. This copies each `usage_ledger` row recorded BEFORE the first real
event into `usage_events`, one row each, so the sum is whole from the first day the ledger has. It changes no behaviour by itself: until
that change lands, nothing reads the copies, which is why it can be run, and checked, first.

## What a carried row is

  * `event_id = 'ledger:<ledger id>'`: deterministic, so a re-run inserts nothing twice (`ON CONFLICT DO NOTHING`), and
    unmistakable, so `WHERE event_id LIKE 'ledger:%'` finds exactly the history;
  * `kind = 'chat'`, tokens and cost as the ledger row has them (it never kept an input/output split, so those are 0),
    and `occurred_at = recorded_at` = the ledger row's own time, so it falls in the window it was spent in;
  * **never rated, never charged, never queued for export.** It is history, not a model call: this is plain SQL outside
    the paths that debit a wallet or fill the outbox, so a carried row can neither bill anyone nor reach a provider.

## The cutoff, and why a row is not copied twice

Since the events existed, every turn has been recorded in BOTH tables (the dual write), so copying a ledger row from
that period would count the turn twice. The cutoff is therefore the time of the FIRST REAL event (a row whose id does
not start with `ledger:`): only older ledger rows are carried. With no real event yet the cutoff is "now", and a
re-run after the deployment starts writing events picks up whatever was recorded in the gap, because the cutoff is
recomputed each time and moves BACK to the first real event, never forward.

Run it BEFORE deploying the change that reads from the events, and once more after (it prints what it did, and the
second run normally carries nothing). `--dry-run` prints what it would carry and writes nothing; `--tenant` limits it to
one tenant, to check a single one before the full run (the cutoff stays the same global instant).

## Disclosed limits

  * A turn that straddles the cutoff (its early calls before the events existed, its ledger row after) is counted by
    its events only. The earlier calls are missed: one turn's worth, once.
  * Rows are copied in batches that commit on their own, up to a ceiling per run (`MAX_BATCHES`); hitting it is reported
    and a re-run continues where it stopped (rows already carried are not selected again).
  * If the events were ever switched off for a while after the first one was written, the ledger rows of that stretch
    sit AFTER the cutoff and are not carried. `make credit-reconcile` names such a tenant-day (events below the ledger).
"""
import argparse
import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from psycopg import errors as pg_errors

from app.agent.sql_store import get_connection
from app.core.job_runtime import scheduled_job

logger = logging.getLogger(__name__)

PREFIX = "ledger:"
BATCH_SIZE = 5000
# 1,000 batches is 5 million ledger rows at the default size: far past a year of any deployment this was sized for. The
# ceiling is for the day something is wrong, so one run still has a bounded duration; the job is idempotent, so a re-run continues.
MAX_BATCHES = 1000

_CUTOFF = "SELECT COALESCE(MIN(recorded_at), now()) FROM usage_events WHERE event_id NOT LIKE %(like)s"

# One statement per batch: read the next ids that are NOT YET carried, insert them, and report what happened. Skipping the
# carried ones in the SELECT (and not only in the INSERT) is what lets a run that stopped at its ceiling be continued: a scan
# that started again from id 0 would re-read the same already-carried rows and never advance. The data-modifying CTE runs even
# though only the final SELECT names it, and `ON CONFLICT DO NOTHING` still guards two runs racing each other.
_BATCH = """
WITH batch AS (
    SELECT id, tenant, principal, thread_id, model_alias, resolved_model, total_tokens, cost_usd, recorded_at
    FROM usage_ledger
    WHERE id > %(after)s AND recorded_at < %(cutoff)s AND (%(tenant)s::text IS NULL OR tenant = %(tenant)s)
      AND NOT EXISTS (SELECT 1 FROM usage_events e WHERE e.event_id = %(prefix)s::text || usage_ledger.id::text)
    ORDER BY id
    LIMIT %(n)s
), inserted AS (
    INSERT INTO usage_events (event_id, tenant, principal, thread_id, kind, model_alias, resolved_model,
                              total_tokens, cost_usd, occurred_at, recorded_at)
    SELECT %(prefix)s::text || id::text, tenant, principal, thread_id, 'chat', model_alias, resolved_model,
           total_tokens, cost_usd, recorded_at, recorded_at
    FROM batch
    ON CONFLICT (event_id) DO NOTHING
    RETURNING cost_usd
)
SELECT (SELECT MAX(id) FROM batch), (SELECT COUNT(*) FROM batch),
       (SELECT COUNT(*) FROM inserted), (SELECT COALESCE(SUM(cost_usd), 0) FROM inserted)
"""

_DRY_RUN = """
SELECT COUNT(*), COALESCE(SUM(cost_usd), 0) FROM usage_ledger l
WHERE l.recorded_at < %(cutoff)s AND (%(tenant)s::text IS NULL OR l.tenant = %(tenant)s)
  AND NOT EXISTS (SELECT 1 FROM usage_events e WHERE e.event_id = %(prefix)s::text || l.id::text)
"""


@dataclass(frozen=True)
class CarryOver:
    cutoff: datetime
    carried: int  # rows written by this run
    carried_usd: Decimal
    complete: bool  # False when the per-run ceiling stopped it: run it again
    dry_run: bool = False


async def cutoff_for(conn) -> datetime:
    """The instant before which a ledger row is history: the time of the first REAL event (an id that does not start with
    `ledger:`), or the database's `now()` when there is none yet. Taken on `conn` so a test can ask it of an empty table."""
    cur = await conn.execute(_CUTOFF, {"like": f"{PREFIX}%"})
    (cutoff,) = await cur.fetchone()
    return cutoff


async def run(
    *, tenant: str | None = None, dry_run: bool = False, batch_size: int = BATCH_SIZE, max_batches: int = MAX_BATCHES
) -> CarryOver:
    """Carries every ledger row older than the first real event (of `tenant` only, when given). In dry-run mode counts
    the rows it would carry and writes nothing."""
    async with get_connection() as conn:
        cutoff = await cutoff_for(conn)
        if dry_run:
            cur = await conn.execute(_DRY_RUN, {"cutoff": cutoff, "prefix": PREFIX, "tenant": tenant})
            count, usd = await cur.fetchone()
            return CarryOver(cutoff, int(count), Decimal(usd), True, dry_run=True)

    after, carried, carried_usd = 0, 0, Decimal(0)
    for _ in range(max_batches):
        async with get_connection() as conn:
            cur = await conn.execute(
                _BATCH, {"after": after, "cutoff": cutoff, "n": batch_size, "prefix": PREFIX, "tenant": tenant}
            )
            last_id, scanned, inserted, usd = await cur.fetchone()
        carried += int(inserted)
        carried_usd += Decimal(usd)
        if scanned < batch_size:
            return CarryOver(cutoff, carried, carried_usd, True)
        after = last_id
    logger.warning("usage_events_carry_over_hit_batch_ceiling", extra={"carried": carried, "max_batches": max_batches})
    return CarryOver(cutoff, carried, carried_usd, False)


def describe(result: CarryOver) -> str:
    """What the operator sees on stdout. A run that stopped at its ceiling must not read as "done"."""
    when = result.cutoff.isoformat()
    if result.dry_run:
        return f"Would carry {result.carried} ledger row(s) worth USD {result.carried_usd}, recorded before {when}."
    if not result.carried:
        return f"Nothing to carry: every ledger row recorded before {when} is already in usage_events."
    message = f"Carried {result.carried} ledger row(s) worth USD {result.carried_usd}, recorded before {when}."
    if not result.complete:
        message += " That is this run's ceiling, so older rows may remain: run it again."
    return message


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--dry-run", action="store_true", help="count what would be carried and write nothing")
    parser.add_argument("--tenant", help="carry only this tenant's ledger rows (the cutoff is still the global one)")
    args = parser.parse_args()
    with scheduled_job("agent-core-usage-events-carry-over"):
        try:
            result = asyncio.run(run(tenant=args.tenant, dry_run=args.dry_run))
        except pg_errors.UndefinedTable as exc:
            raise SystemExit(
                f"a table is missing ({exc}): apply postgres-init/19-usage-events.sql before carrying the ledger over"
            ) from exc
        print(describe(result))


if __name__ == "__main__":
    main()
