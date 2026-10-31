"""The agent's own money, as the application sees it: usage events, budget holds, budget overrides and the gateway's spend log.
(The gateway's price list is `app/ports/models.py`.)

The policy (what a window is, which limit applies, what a failed read means, what is counted when a write fails) stays in
`app/agent/cost/`. What these ports hide is where the numbers live: Postgres for the events, the holds and the overrides
(`app/adapters/postgres/`), LiteLLM for the spend log (`app/adapters/litellm/`).

Two conventions every adapter keeps, because the application's fail-open and fail-closed choices are written against them:
- a read or write that cannot reach its system RAISES. The application decides what that means (`budgets.check` applies the failure
  policy; `record_call` counts and carries on); an adapter never swallows it.
- every method takes the tenant as an argument and scopes the statement by it. The tenant is never inferred.

`UsageTableMissing` and `PolicyTableMissing` are the one error an adapter translates: a database that has not had its migration
applied is a state the application tolerates with a single warning, and it must not have to know what psycopg calls it.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Protocol, TypedDict

from app.ports.slot import Slot

# --- budget overrides -------------------------------------------------------------------------------------------------------

TENANT_SUBJECT = ""  # the tenant's own limit
ALL_PRINCIPALS = "*"  # the personal limit of every person in the tenant
RESERVED_SUBJECTS = frozenset({TENANT_SUBJECT, ALL_PRINCIPALS})
PERIODS = ("day", "month")


@dataclass(frozen=True)
class Override:
    """One row. `limit_usd` None means explicitly NO cap; 0 means refuse everything."""

    subject: str
    period: str
    limit_usd: float | None


class PolicyTableMissing(Exception):
    """The budget-overrides table does not exist: the migration (postgres-init/18) has not been applied."""


class BudgetPolicyStore(Protocol):
    async def for_person(self, tenant: str, principal: str) -> list[Override]:
        """The tenant's own override, the tenant-wide personal default and this person's. Raises `PolicyTableMissing` when the table is absent."""
        ...

    async def for_tenant(self, tenant: str) -> list[Override]:
        """Every override of the tenant, ordered by subject then period."""
        ...

    async def upsert(self, tenant: str, subject: str, period: str, limit_usd: float | None, updated_by: str) -> None: ...

    async def delete(self, tenant: str, subject: str, period: str) -> bool:
        """True if a row existed."""
        ...


# --- budget holds -----------------------------------------------------------------------------------------------------------

class BudgetHoldStore(Protocol):
    async def reserve(self, tenant: str, hold_id: str, amount_usd: float, *, stale_after_minutes: int) -> None:
        """Takes the hold and, in the same transaction, drops this tenant's holds older than `stale_after_minutes`."""
        ...

    async def release(self, tenant: str, hold_id: str) -> None:
        """Deletes the hold, scoped to the tenant: a late or duplicate release is a no-op."""
        ...

    async def in_flight(self, tenant: str, *, stale_after_minutes: int) -> float:
        """The sum of the tenant's holds younger than `stale_after_minutes`."""
        ...


# --- usage events -----------------------------------------------------------------------------------------------------------

class UsageEventRow(TypedDict):
    event_id: str
    tenant: str
    principal: str
    thread_id: str
    kind: str
    model_alias: str
    resolved_model: str | None
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int
    total_tokens: int
    cost_usd: float | None
    price_input_per_token: float | None
    price_output_per_token: float | None


@dataclass(frozen=True)
class UsageTotals:
    total_tokens: int
    total_cost_usd: float


@dataclass(frozen=True)
class Window:
    """`[start, end]` in UTC, both inclusive (the gateway's own convention, so both sides read the same instants)."""

    start: datetime
    end: datetime

    @property
    def empty(self) -> bool:
        return self.end < self.start


Key = tuple[str, date]


@dataclass(frozen=True)
class EventsByTenantDay:
    """USD per tenant-day, unpriced events per tenant-day, and the number of events in all. An unpriced event adds nothing to the USD
    figure (its cost is unknown, never zero), so it is counted."""

    usd: dict[Key, Decimal]
    unpriced: dict[Key, int]
    events: int


class UsageTableMissing(Exception):
    """The usage-events table (or a column of it) does not exist: the migration (postgres-init/19) has not been applied."""


class UsageEventStore(Protocol):
    async def insert(self, row: UsageEventRow) -> bool:
        """Writes one event, idempotent on `event_id`: True if it was written, False if it was already there. A newly written event is
        handed to billing in the same transaction (a duplicate is not). Raises `UsageTableMissing` when the table is absent."""
        ...

    async def totals(self, tenant: str, principal: str | None = None, since: datetime | None = None) -> UsageTotals:
        """Tokens and cost for the tenant, optionally narrowed to one principal and to events on or after `since`."""
        ...

    async def delete_older_than(self, *, days: int, limit: int) -> int:
        """Deletes up to `limit` events older than `days` days, and returns how many went."""
        ...

    async def by_tenant_day(self, window: Window) -> EventsByTenantDay: ...

    async def known_tenants(self) -> set[str]:
        """Every tenant that has ever had a usage event."""
        ...


# --- the LLM gateway's spend log -------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class GatewaySpend:
    by_end_user_day: Mapping[tuple[str, date], Decimal]  # an empty end_user is the key "" (no identity was sent)
    rows: int
    complete: bool
    reason: str = ""


class GatewaySpendLog(Protocol):
    async def spend(self, window: Window, *, max_pages: int) -> GatewaySpend:
        """Every spend-log row in the window, summed by end user and UTC day. Raises on a transport error or a response that is not the
        documented shape; returns `complete=False`, with the reason, when the read cannot be trusted to be whole."""
        ...


usage_events: Slot[UsageEventStore] = Slot("usage_events", UsageEventStore)
budget_holds: Slot[BudgetHoldStore] = Slot("budget_holds", BudgetHoldStore)
budget_policies: Slot[BudgetPolicyStore] = Slot("budget_policies", BudgetPolicyStore)
