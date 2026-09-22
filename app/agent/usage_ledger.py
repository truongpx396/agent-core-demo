"""Real usage/cost ledger (pattern 26) — closes the "no hollow Meter" gap:
a shipped default should keep an actual ledger, not a no-op that makes a
broken deployment look configured (same principle as moderation.py's real
check).

Persisted in the same `appdata` Postgres database as sql_store.py
(`usage_ledger` table, postgres-init/03-meter.sql). Every row is
tenant+principal scoped.

Cost comes from a small, explicit per-model price table
(`PRICE_PER_1K_TOKENS_USD`) — $0 for any unlisted alias, true for every
Ollama model this demo runs locally. Tokens are recorded unconditionally
regardless of price, so pointing at a real paid provider and adding its
alias is the only change needed for real cost tracking.
"""
import logging
from datetime import datetime

from app.agent.model_resolver import resolve_model
from app.agent.sql_store import get_connection
from app.core.security import SecurityCtx, valid_ctx

logger = logging.getLogger(__name__)

# A reservation older than this is treated as abandoned (a worker that
# died mid-turn without ever reaching release_budget_reservation) rather
# than real in-flight spend — see postgres-init/12-tenant-budget-reservations.sql
# for why this lives here instead of a Redis TTL. Generous relative to
# REQUEST_TIMEOUT_SECONDS (60s default) for the same reason
# app/job_queue/queue.py's THREAD_LOCK_TTL_SECONDS is: a "resume" job's
# own turn isn't wrapped in that same timeout, so a legitimate reservation
# can outlive it.
RESERVATION_STALE_AFTER_MINUTES = 5

# Approximate, illustrative USD/1000-token pricing — update to match your
# provider's rates. An alias absent from this table costs $0 (true for
# every model this app runs locally via Ollama/LiteLLM).
PRICE_PER_1K_TOKENS_USD: dict[str, float] = {
    "gpt-4o": 0.005,
    "gpt-4o-mini": 0.00015,
}


async def record_usage(
    ctx: SecurityCtx | None, thread_id: str, model_alias: str, total_tokens: int
) -> None:
    """Best-effort write-through after a turn completes
    (`runtime.py::_record_turn_metrics`). A failing write must not fail
    the turn it's recording — same degrade-don't-crash posture as
    `semantic_cache.py::set()`. No-ops without a valid ctx or with zero
    tokens (unattributable / nothing to meter).
    """
    if not valid_ctx(ctx) or total_tokens <= 0:
        return
    price_per_1k = PRICE_PER_1K_TOKENS_USD.get(model_alias, 0.0)
    cost_usd = (total_tokens / 1000) * price_per_1k
    # The resolved CONCRETE model behind `model_alias` (GRAPH_PATTERNS.md
    # pattern 38) — None if resolution itself degrades (LiteLLM
    # unreachable, alias unknown); never blocks the write.
    resolved_model = resolve_model(model_alias)
    try:
        async with get_connection() as conn:
            await conn.execute(
                "INSERT INTO usage_ledger "
                "(tenant, principal, thread_id, model_alias, total_tokens, cost_usd, resolved_model) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (
                    ctx["tenant"],
                    ctx["principal"],
                    thread_id,
                    model_alias,
                    total_tokens,
                    cost_usd,
                    resolved_model,
                ),
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "usage ledger write failed; continuing without recording",
            extra={"error_class": type(exc).__name__},
        )


async def usage_summary(
    tenant: str, principal: str | None = None, since: datetime | None = None
) -> dict:
    """Total tokens and cost for `tenant`, optionally narrowed to one
    `principal` and/or to usage on/after `since`. `since` is what
    `_tenant_over_daily_budget` (runtime.py) uses for a ROLLING 24h window
    (`now - 24h`, not calendar-day boundaries, so a tenant's near-limit
    status never resets mid-day). `since=None` (all-time) is the right
    default for `GET /usage`'s "total ever spent" question instead."""
    where = ["tenant = %s"]
    params: list = [tenant]
    if principal:
        where.append("principal = %s")
        params.append(principal)
    if since is not None:
        where.append("recorded_at >= %s")
        params.append(since)

    sql = (
        "SELECT COALESCE(SUM(total_tokens), 0), COALESCE(SUM(cost_usd), 0) "
        f"FROM usage_ledger WHERE {' AND '.join(where)}"
    )
    async with get_connection() as conn:
        cur = await conn.execute(sql, params)
        total_tokens, total_cost = await cur.fetchone()
    return {"total_tokens": int(total_tokens), "total_cost_usd": float(total_cost)}


async def reserve_budget(ctx: SecurityCtx | None, amount_usd: float) -> bool:
    """Atomically adds `amount_usd` to `ctx`'s tenant's in-flight
    reservation (an UPSERT — no separate seed row needed), called right
    before a turn that passed `_tenant_over_daily_budget`'s check actually
    starts spending. Closes the gap between "checked" and "recorded": this
    turn's own cost isn't in usage_ledger yet (record_usage only runs
    after it completes), so without a reservation a sibling turn racing
    the same tenant would see the SAME stale "spent so far" and pass the
    check too. Always paired with `release_budget_reservation` once the
    turn ends (success, failure, or timeout) — callers use a `finally` for
    that, same shape as `app/job_queue/queue.py::acquire_thread_lock`'s
    own release contract.

    Returns whether the reservation itself succeeded — best-effort, same
    fail-open posture as every other write in this module: a reservation
    failure must not block a turn, it just means this one turn's spend
    goes unprotected against a concurrent sibling, exactly the pre-fix
    behavior."""
    if not valid_ctx(ctx) or amount_usd <= 0:
        return False
    try:
        async with get_connection() as conn:
            await conn.execute(
                "INSERT INTO tenant_budget_reservations (tenant, reserved_usd, updated_at) "
                "VALUES (%s, %s, now()) "
                "ON CONFLICT (tenant) DO UPDATE SET "
                "reserved_usd = tenant_budget_reservations.reserved_usd + EXCLUDED.reserved_usd, "
                "updated_at = now()",
                (ctx["tenant"], amount_usd),
            )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "tenant_budget_reservation_failed", extra={"error_class": type(exc).__name__}
        )
        return False


async def release_budget_reservation(ctx: SecurityCtx | None, amount_usd: float) -> None:
    """Reverses a prior `reserve_budget` call for the same amount.
    `GREATEST(..., 0)` floors at zero rather than going negative — a
    defensive clamp, not a correctness requirement (reserve/release calls
    are always paired 1:1 by `astream_events_turn`), so a bug elsewhere
    can't leave a tenant's reservation permanently negative and quietly
    masking real in-flight spend forever."""
    if not valid_ctx(ctx) or amount_usd <= 0:
        return
    try:
        async with get_connection() as conn:
            await conn.execute(
                "UPDATE tenant_budget_reservations "
                "SET reserved_usd = GREATEST(reserved_usd - %s, 0), updated_at = now() "
                "WHERE tenant = %s",
                (amount_usd, ctx["tenant"]),
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("tenant_budget_release_failed", extra={"error_class": type(exc).__name__})


async def in_flight_reservation(tenant: str) -> float:
    """Current reserved-but-not-yet-recorded spend for `tenant`, ignoring
    a reservation stale beyond RESERVATION_STALE_AFTER_MINUTES (a worker
    that died mid-turn without ever releasing it) rather than letting it
    permanently inflate this tenant's apparent spend — self-heals without
    needing a background cleanup job. Fails open to 0.0 on any error, same
    posture as every other read this module/runtime.py's own budget check
    already has: a reservation-table outage must not ALSO take down the
    ledger check that's still healthy."""
    try:
        async with get_connection() as conn:
            cur = await conn.execute(
                "SELECT reserved_usd FROM tenant_budget_reservations "
                "WHERE tenant = %s AND updated_at > now() - make_interval(mins => %s)",
                (tenant, RESERVATION_STALE_AFTER_MINUTES),
            )
            row = await cur.fetchone()
        return float(row[0]) if row else 0.0
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "tenant_budget_in_flight_read_failed", extra={"error_class": type(exc).__name__}
        )
        return 0.0
