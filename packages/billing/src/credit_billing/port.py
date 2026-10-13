"""The billing port: the contract between a host application and the billing core (specs/011-billing-module-boundary,
contracts/billing-port.md, decisions D12, D18, D19).

A host that uses the wallet needs two things from it and nothing else: to **charge a usage event** in its own
transaction, and to **read a tenant's balance** (the gate before a turn, the usage endpoint). Those two are the whole
protocol. They are written as the shape a service API would have (plain data types, an idempotency key on the write,
a tenant on both), so a network implementation can satisfy the same protocol later without the host changing. The
one thing that is not plain is the caller's `conn` on the write, and that is deliberate: today the debit is atomic with
the usage event the caller just inserted, in the caller's transaction and inside the caller's savepoint, and that
atomicity is what makes a replay unable to double-debit. A remote wallet cannot take a connection; what it takes
instead (an at-least-once consume keyed by the event id) is the consistency change `design-note.md` describes.

`InProcessWallet` is the implementation that exists. It adds nothing: each method is a call into `credits`, with the
arguments the host used to pass directly, resolved at CALL time (so a test that patches `credits.debit_in` still sees
its patch, and the hermetic suite's connection guard still applies). The host does not import this module. It imports
`app.billing_integration.port`, which holds the implementation in use, so the choice of implementation is made in one
place on the host's side and importing it wires the core to the host's pool.

What is deliberately not here: grants, adjustments and webhook intake (the core serves those itself: the operator CLI
and `app/billing/http.py`), checkout, holds, and rating. `credits_for_cost` is a pure function and stays one; moving
rating behind the port is a later feature (O1, D15).

Imports nothing from `app` outside `app.billing` (enforced by tests/billing/test_billing_core_boundary.py).
"""
from decimal import Decimal
from typing import Protocol, runtime_checkable

from psycopg import AsyncConnection

from app.billing import credits
from app.billing.credits import Applied, Balance, Pricing

__all__ = ["Applied", "Balance", "InProcessWallet", "Pricing", "WalletPort", "credits_for_cost"]


@runtime_checkable
class WalletPort(Protocol):
    async def record_usage_in(
        self,
        conn: AsyncConnection,
        tenant: str,
        amount: Decimal,
        *,
        event_id: str,
        kind: str,
        pricing: Pricing | None = None,
    ) -> Applied:
        """Debits `amount` credits from the tenant's wallet for the usage event `event_id`, on the caller's connection and
        in the caller's transaction. A replay of the same `event_id` changes nothing and returns the original transaction.
        A tenant with no wallet is a no-op (`no_account`). Never refuses for a short balance: the shortfall is booked as
        overdraft (the model call already happened)."""
        ...

    async def account_balance(self, tenant: str) -> Balance | None:
        """The tenant's balance, or None when the tenant has no wallet (so it is not on credit billing)."""
        ...


def credits_for_cost(cost_usd, credits_per_usd, markup) -> Decimal:
    """`round_half_up(cost_usd x credits_per_usd x markup, 6)`: a pure function, so it is the same on either side of a
    network. Rating stays in the host's hands through spec 011 (O1)."""
    return credits.credits_for_cost(cost_usd, credits_per_usd, markup)


class InProcessWallet:
    """The wallet in the same process, over the same database connection pool as the caller."""

    async def record_usage_in(
        self,
        conn: AsyncConnection,
        tenant: str,
        amount: Decimal,
        *,
        event_id: str,
        kind: str,
        pricing: Pricing | None = None,
    ) -> Applied:
        return await credits.debit_in(
            conn,
            tenant,
            amount,
            idempotency_key=event_id,
            usage_event_id=event_id,
            reason=f"model call ({kind})",
            pricing=pricing,
        )

    async def account_balance(self, tenant: str) -> Balance | None:
        return await credits.account_balance(tenant)
