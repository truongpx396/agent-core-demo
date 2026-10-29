"""Per-tenant and per-person overrides of the spend limits (postgres-init/18-budget-policies.sql).

`app/agent/cost/budgets.py` decides; this module only stores and fetches what an operator has changed. A
row overrides ONE of the Settings defaults — for the tenant (`subject=''`), for every person in
the tenant (`'*'`), or for one person — and `budgets.resolve_limits` applies them.

Reads happen before every turn, so they go through a short per-process cache
(`BUDGET_POLICY_REFRESH_SECONDS`, default 30; 0 turns it off): an override takes up to that long
to reach a running worker, which is the price of not adding a database round trip to every turn.
That includes a SUSPEND (limit 0), so it is bounded rather than instant.

Writes are an operator action (scripts/budget_policy.py) and are the one place in the app that
deliberately spans tenants; nothing on a request path can reach them.
"""
import logging
import time

from app.core.config import BUDGET_POLICY_REFRESH_SECONDS
from app.ports import cost as ports
from app.ports.cost import PERIODS, Override, PolicyTableMissing

logger = logging.getLogger(__name__)

_CACHE_MAX = 10_000


# (tenant, principal) -> (fetched_at, rows). A person's rows are the tenant's own, the tenant's
# '*' rows and theirs, so the key has to include the person.
_cache: dict[tuple[str, str], tuple[float, list[Override]]] = {}
_warned_missing_table = False


def reset_cache() -> None:
    """Forgets every cached read and the missing-table warning. For tests, and for an operator
    process that wants to see its own write immediately: this state is process-wide."""
    global _warned_missing_table
    _cache.clear()
    _warned_missing_table = False


async def overrides_for(tenant: str, principal: str) -> list[Override]:
    """Every override that applies to `principal` in `tenant`: the tenant's own, the tenant-wide
    personal default, and this person's. Raises on a database error (the caller decides what a
    failed read means — `budgets.check` applies the failure policy); a missing TABLE is not an
    error, it means the migration has not been applied, and reads as "no overrides" with one
    warning per process."""
    global _warned_missing_table
    now = time.monotonic()
    cached = _cache.get((tenant, principal))
    if cached is not None and now - cached[0] < BUDGET_POLICY_REFRESH_SECONDS:
        return cached[1]
    try:
        overrides = await ports.budget_policies.get().for_person(tenant, principal)
    except PolicyTableMissing:
        if not _warned_missing_table:
            _warned_missing_table = True
            logger.warning(
                "budget_policies table is missing; per-tenant and per-person limit overrides are "
                "ignored until postgres-init/18-budget-policies.sql is applied"
            )
        return []
    if len(_cache) >= _CACHE_MAX:
        _cache.clear()
    _cache[(tenant, principal)] = (now, overrides)
    return overrides


async def list_overrides(tenant: str) -> list[Override]:
    """Every override of `tenant`, for the operator CLI (uncached)."""
    return await ports.budget_policies.get().for_tenant(tenant)


async def set_override(
    tenant: str, subject: str, period: str, limit_usd: float | None, updated_by: str
) -> None:
    """Upserts one override. `limit_usd=None` is "no cap", 0 is "suspend". Operator action."""
    if period not in PERIODS:
        raise ValueError(f"period must be one of {PERIODS}, not {period!r}")
    if limit_usd is not None and limit_usd < 0:
        raise ValueError("limit_usd must be >= 0, or None for no cap")
    if not tenant or not updated_by:
        raise ValueError("tenant and updated_by are required")
    await ports.budget_policies.get().upsert(tenant, subject, period, limit_usd, updated_by)
    reset_cache()


async def clear_override(tenant: str, subject: str, period: str) -> bool:
    """Deletes one override, restoring the Settings default for it. True if a row existed."""
    deleted = await ports.budget_policies.get().delete(tenant, subject, period)
    reset_cache()
    return deleted
