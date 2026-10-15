"""What has this tenant (or one person in it) spent? The read behind every dollar cap and `GET /usage`.

Until specs/010 T030 this was `usage_ledger.usage_summary`, a SUM over a per-TURN table. It is now a SUM over
`usage_events` (postgres-init/19), the per-CALL meter that billing, the credit wallet and the export outbox are
already built on. Having the caps read the same table those read means one record of what was spent instead of
two that a test has to keep agreeing, and it closes three gaps the ledger could not:

  * **A turn is counted as it spends, not when it ends.** The ledger row is written after the last step, so a turn
    that timed out, was cancelled or was paused for approval and never resumed spent money no cap could see
    (spec 008 B17 patched the timeout case by re-reading the checkpoint; a paused turn stayed uncounted). An
    event is written the moment its call returns.
  * **Every kind of call is counted by construction.** Follow-ups, compaction, subagents and the cron scripts
    write events through `metering.metered_invoke`, so a new call site cannot spend off the books.
  * **The cap and the bill cannot disagree**, because they sum the same rows.

## What is deliberately different from the ledger sum

  * **An unpriced call adds $0 to the dollar figure** (`cost_usd` is NULL on the row: unknown, not free) but still
    adds its tokens. `SUM` skips NULLs, which is the same $0 the ledger recorded for it, and the call is already
    counted once as `agent_unpriced_usage_total`. A cap cannot enforce a price nobody knows; `UNPRICED_MODEL_POLICY=block`
    is what refuses a model with none.
  * **The window is `occurred_at`**, the column `(tenant, occurred_at)` indexes. The old ledger window was
    `recorded_at`, which for a turn is the END of the turn; an event's is the moment of the call, so a long turn
    is counted in the window it ran in.
  * **A running turn is counted twice, in the safe direction.** Its finished calls are already events while its hold
    (`budget_holds.reserve_budget`, `MAX_COST_USD_PER_TURN`) still stands, so a sibling's tenant-level check sees
    "spent so far + the whole hold". That over-counts by at most what the running turn has spent so far, and
    never lets a burst through, which is the direction the hold exists to protect. (A person's limit ignores
    holds, as before.)

## What a read costs (measured, 2026-10-08, a throwaway Postgres, one tenant with 2,000,000 events in 30 days)

An event is one MODEL CALL and the ledger row was one TURN, so the same window now holds several times the rows. Every read
below is served by `usage_events (tenant, occurred_at)`; a person's read filters `principal` after that range:

  * tenant, rolling 24h (the cap that is always on): 9.3 ms, 1,805 buffers;
  * one person, 24h: 5.8 ms (0.46 ms with a `(tenant, principal, occurred_at)` index);
  * one person, calendar month: 98 ms (79 ms with that index: the person's own 40,000 rows must still be summed);
  * **tenant, calendar month: 426 ms, a sequential scan of the 2,000,000 rows** (194 ms with a covering index that `INCLUDE`s
    `total_tokens, cost_usd`, which was measured and not adopted).

A `(tenant, principal, occurred_at)` index was measured and NOT added: unlike the ledger (spec 008 A3, whose only index made
the read walk every one of a tenant's rows), the existing index already gives a good plan, and the new one would save about 5 ms
on an opt-in cap while costing 118 MB (32% of the table) and one more write on every model call. **The monthly caps
(`MAX_COST_USD_PER_TENANT_PER_MONTH` and the person's) are linear in a month's calls and off by default**; that is the same
complexity as the ledger read had, times the number of calls per turn. The lever, if a tenant makes them slow, is a per-day
rollup written in the event's transaction, which is not built. Disclosed in the README's known gaps.

## History from before the events existed

Events begin when postgres-init/19 was applied, so a straight switch would make a monthly cap forget everything
spent earlier in the month. `scripts/usage_events_carry_over.py` copies the older ledger rows into `usage_events`
once (event ids `ledger:<id>`; they are never rated, charged or queued for export, because they are history and not calls).
It must run BEFORE a deployment starts reading from here; the README's upgrade note says so.

## When this cannot answer

A missing table (`usage_events` was never applied) or an unreachable database raises, and
`budgets._spend`'s caller applies `BUDGET_CHECK_FAILURE_POLICY` exactly as it did for the ledger read:
"open" serves the turn and counts it (`agent_cost_governance_degraded_total{path="ledger_read"}`, alert
`TenantAllowanceUnenforced`; the label keeps its old name so the alert and dashboards still match), "closed"
refuses it. Nothing here swallows an error: a cap that silently reads zero is a cap that is off.
"""
from datetime import datetime

from app.agent.sql_store import get_connection


async def usage_summary(tenant: str, principal: str | None = None, since: datetime | None = None) -> dict:
    """Total tokens and cost for `tenant`, optionally narrowed to one `principal` and/or to usage on/after `since`.

    `since` is what the budgets use for a ROLLING 24h window (`now - 24h`, not a calendar day, so a tenant's
    near-limit status never resets mid-day) and for the calendar month; `since=None` (all-time) is `GET /usage`'s
    "total ever spent", bounded by however long events are kept. Tenant is a parameter of every statement."""
    where = ["tenant = %s"]
    params: list = [tenant]
    if principal:
        where.append("principal = %s")
        params.append(principal)
    if since is not None:
        where.append("occurred_at >= %s")
        params.append(since)

    sql = (
        "SELECT COALESCE(SUM(total_tokens), 0), COALESCE(SUM(cost_usd), 0) "
        f"FROM usage_events WHERE {' AND '.join(where)}"
    )
    async with get_connection() as conn:
        cur = await conn.execute(sql, params)
        total_tokens, total_cost = await cur.fetchone()
    return {"total_tokens": int(total_tokens), "total_cost_usd": float(total_cost)}
