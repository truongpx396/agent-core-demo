"""Fakes for the ports in `app/ports/cost.py`."""
from collections.abc import Mapping
from datetime import datetime

from app.ports.cost import (
    ALL_PRINCIPALS,
    TENANT_SUBJECT,
    EventsByTenantDay,
    Override,
    UsageEventRow,
    UsageTotals,
    Window,
)


class InMemoryUsageEventStore:
    """Keeps every written event in `rows`, in order. `insert` is idempotent on `event_id`, as the table's PRIMARY KEY makes it."""

    def __init__(self) -> None:
        self.rows: list[UsageEventRow] = []

    async def insert(self, row: UsageEventRow) -> bool:
        if any(existing["event_id"] == row["event_id"] for existing in self.rows):
            return False
        self.rows.append(row)
        return True

    async def totals(self, tenant: str, principal: str | None = None, since: datetime | None = None) -> UsageTotals:
        mine = [r for r in self.rows if r["tenant"] == tenant and (not principal or r["principal"] == principal)]
        return UsageTotals(sum(r["total_tokens"] for r in mine), float(sum(r["cost_usd"] or 0 for r in mine)))

    async def delete_older_than(self, *, days: int, limit: int) -> int:
        return 0  # the fake has no clock: a row is never old

    async def by_tenant_day(self, window: Window) -> EventsByTenantDay:
        return EventsByTenantDay({}, {}, 0)

    async def known_tenants(self) -> set[str]:
        return {r["tenant"] for r in self.rows}


class InMemoryBudgetHoldStore:
    """Holds by (tenant, hold_id). Nothing ages out: the fake has no clock, and staleness is the adapter's SQL."""

    def __init__(self) -> None:
        self.holds: dict[tuple[str, str], float] = {}

    async def reserve(self, tenant: str, hold_id: str, amount_usd: float, *, stale_after_minutes: int) -> None:
        self.holds[(tenant, hold_id)] = amount_usd

    async def release(self, tenant: str, hold_id: str) -> None:
        self.holds.pop((tenant, hold_id), None)

    async def in_flight(self, tenant: str, *, stale_after_minutes: int) -> float:
        return float(sum(amount for (t, _), amount in self.holds.items() if t == tenant))


class InMemoryBudgetPolicyStore:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, str, str], float | None] = {}

    async def for_person(self, tenant: str, principal: str) -> list[Override]:
        wanted = {TENANT_SUBJECT, ALL_PRINCIPALS, principal}
        return [Override(s, p, lim) for (t, s, p), lim in self.rows.items() if t == tenant and s in wanted]

    async def for_tenant(self, tenant: str) -> list[Override]:
        return [Override(s, p, lim) for (t, s, p), lim in sorted(self.rows.items()) if t == tenant]

    async def upsert(self, tenant: str, subject: str, period: str, limit_usd: float | None, updated_by: str) -> None:
        self.rows[(tenant, subject, period)] = limit_usd

    async def delete(self, tenant: str, subject: str, period: str) -> bool:
        return self.rows.pop((tenant, subject, period), ...) is not ...


class NoGatewayModelCatalog:
    """A gateway that is not there: no alias resolves, and the price list cannot be read (which is what the real adapter does with
    nothing listening, so the degrade paths a test sees are the production ones)."""

    async def model_info(self) -> list[Mapping]:
        raise ConnectionError("no gateway in this test")

    async def resolve(self, alias: str) -> str | None:
        return None
