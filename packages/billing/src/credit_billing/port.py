"""The billing port: the contract between a host application and the billing package
(specs/011-billing-module-boundary, contracts/billing-port.md, D12, D18, D19; widened by specs/013).

A host that bills needs four things from the package and nothing else:
- **record a unit of usage** in its own transaction (`record_usage_in`): the package rates, keeps, debits and
  queues it, and never raises for an operational fault (`usage.py`);
- **ask whether a tenant may start more work** (`admit`): the credit gate (`admission.py`);
- **read a tenant's balance** (`account_balance`);
- **read what to show a tenant** (`credit_summary`): the balance and whether the gate enforces, or None when
  credits are off or there is no wallet.

They have the shape of a service API (plain types, an idempotency key on the write, a tenant on every call),
so a network implementation could satisfy the same protocol without the host changing. The one thing that
isn't plain is the caller's `conn` on the write, deliberately: the record, the debit and the export row are
atomic with the host's own event, in its transaction and savepoint, and that atomicity stops a replay from
double-debiting. A remote service can't take a connection; what it takes instead (an at-least-once consume
keyed by the event id) is the change `design-note.md` describes.

`InProcessBilling` is the implementation that exists. It adds nothing: each method calls the module that does
the work, resolved at CALL time so a test patching `usage.record_usage_in` or `credits.account_balance` still
sees its patch (and the hermetic suite's connection guard still applies). The host imports
`app.billing_integration.port`, not this module, so the choice is made in one place and importing it wires the
package to the host's pool.

Not here: grants, adjustments and webhook intake (served by the operator CLI and `http.py`), checkout and
holds. `credits_for_cost` is a pure function and stays one.

Imports nothing from `app` outside `credit_billing` (enforced by tests/test_boundary.py).
"""
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol, runtime_checkable

from psycopg import AsyncConnection

from credit_billing import admission, credits
from credit_billing import usage as usage_module
from credit_billing.admission import Admission
from credit_billing.credits import Applied, Balance, IdempotencyConflict, Pricing
from credit_billing.settings import CREDITS_ENFORCEMENT, CREDITS_PER_USD
from credit_billing.usage import Recorded, Usage

__all__ = [
    "Admission", "Applied", "Balance", "BillingPort", "CreditSummary", "IdempotencyConflict", "InProcessBilling", "Pricing", "Recorded",
    "Usage", "credits_for_cost",
]


@dataclass(frozen=True)
class CreditSummary:
    """What a tenant is shown of its wallet: the balance, and whether running out refuses its requests."""

    balance: Balance
    enforced: bool


@runtime_checkable
class BillingPort(Protocol):
    async def record_usage_in(self, conn: AsyncConnection, usage: Usage) -> Recorded:
        """Rates `usage`, records it under `(tenant, event_id)`, debits the tenant's wallet and queues it for export, on the caller's
        connection and in the caller's transaction. A replay of the same `(tenant, event_id)` changes nothing. Never raises for an operational
        fault (it is counted); with no rate and no usage-billing provider it does nothing. A tenant with no wallet is not debited. The
        debit never refuses for a short balance: the shortfall is booked as overdraft (the work already happened)."""
        ...

    async def account_balance(self, tenant: str) -> Balance | None:
        """The tenant's balance, or None when the tenant has no wallet (so it is not on credit billing)."""
        ...

    async def admit(self, tenant: str, *, reserved_usd: Callable[[], Awaitable[Decimal | float]]) -> Admission:
        """Whether the tenant may start more work on its prepaid balance. Reads nothing when enforcement is off. `reserved_usd` is awaited
        only for a tenant that has a wallet."""
        ...

    async def credit_summary(self, tenant: str) -> CreditSummary | None:
        """The tenant's balance and whether the gate enforces, or None when credits are off or the tenant has no wallet. Raises when the
        wallet cannot be read: an endpoint that shows it should say so rather than report a calm "no wallet"."""
        ...


def credits_for_cost(cost_usd, credits_per_usd, markup) -> Decimal:
    """`round_half_up(cost_usd x credits_per_usd x markup, 6)`: a pure function, so it is the same on either side of a network."""
    return credits.credits_for_cost(cost_usd, credits_per_usd, markup)


class InProcessBilling:
    """Billing in the same process, over the same database connection pool as the caller."""

    async def record_usage_in(self, conn: AsyncConnection, usage: Usage) -> Recorded:
        return await usage_module.record_usage_in(conn, usage)

    async def account_balance(self, tenant: str) -> Balance | None:
        return await credits.account_balance(tenant)

    async def admit(self, tenant: str, *, reserved_usd: Callable[[], Awaitable[Decimal | float]]) -> Admission:
        return await admission.admit(tenant, reserved_usd=reserved_usd)

    async def credit_summary(self, tenant: str) -> CreditSummary | None:
        if CREDITS_PER_USD is None:
            return None
        balance = await credits.account_balance(tenant)
        return None if balance is None else CreditSummary(balance, CREDITS_ENFORCEMENT)
