"""What has this tenant (or one person in it) spent? The read behind every dollar cap and `GET /usage`.

It sums `usage_events` (postgres-init/19), the per-CALL meter that billing, the wallet and the export outbox
are built on (it replaced the per-TURN `usage_ledger` sum, specs/010 T030). Compared with the ledger:
- A turn is counted as it spends, not when it ends, so a turn that timed out, was cancelled or sat paused no
  longer spends money no cap saw.
- Every call kind is counted by construction (follow-ups, compaction, subagents, cron scripts all write events
  through `metering.metered_invoke`), and the cap and the bill can't disagree because they sum the same rows.
- An unpriced call adds $0 (`cost_usd` is NULL) but still its tokens; it is counted as
  `agent_unpriced_usage_total`, and `UNPRICED_MODEL_POLICY=block` refuses a model with no price.
- The window is `occurred_at` (indexed by `(tenant, occurred_at)`), so a long turn lands in the window it ran
  in.
- A running turn is counted twice, in the safe direction: its finished calls are events while its hold
  (`budget_holds.reserve_budget`) still stands. That over-counts by at most what it has spent and never lets a
  burst through. A person's limit ignores holds.

## What a read costs (measured with 2,000,000 events for one tenant in 30 days)

Tenant rolling 24h (the always-on cap) 9.3 ms; one person 24h 5.8 ms; one person calendar month 98 ms;
**tenant calendar month 426 ms, a sequential scan**. A `(tenant, principal, occurred_at)` index was measured
and NOT added: it saves ~5 ms on an opt-in cap and costs 118 MB (32% of the table) plus a write per model
call. The monthly caps are linear in a month's calls and off by default; the lever, if a tenant makes them
slow, is a per-day rollup written in the event's transaction (not built; in the README's known gaps).

## History and failure

Events begin when postgres-init/19 is applied, so `scripts/usage_events_carry_over.py` copies the older ledger
rows once (ids `ledger:<id>`, never rated, charged or exported). Run it BEFORE a deployment starts reading
from here (README upgrade note).

A missing table or unreachable database raises, and the caller applies `BUDGET_CHECK_FAILURE_POLICY` ("open"
serves the turn and counts `agent_cost_governance_degraded_total{path="ledger_read"}`, alert
`TenantAllowanceUnenforced`; "closed" refuses). Nothing here swallows an error: a cap that silently reads zero
is a cap that is off.
"""
from datetime import datetime

from app.core.sql_store import get_connection


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
