"""The one module host code imports to use the billing wallet (specs/011-billing-module-boundary, D19).

Outside `packages/billing/src/credit_billing/` and `app/billing_integration/` nothing under `app/` or `scripts/` imports `credit_billing`
(tests/billing/test_billing_core_boundary.py enforces it), so this is where the agent app meets the wallet:

  * `wallet` is the implementation in use. Today it is `InProcessWallet`, a thin call into `packages/billing/src/credit_billing/credits.py`
    over the same pool as the caller. A network client implementing the same `WalletPort` would be selected here, and
    a test installs a stub here (`monkeypatch.setattr(billing_port, "wallet", stub)`): it is a module-level name read
    at CALL time, which is the repo's idiom for a seam a test replaces.
  * The functions below are what the host calls. They add nothing; they exist so call sites read as "the billing
    port" and so the implementation is looked up on every call.
  * Importing this module imports the `app.billing_integration` package first, and that wires the core to the agent's
    connection pool. A host path that reaches the wallet through here cannot reach it unwired.

`Pricing` and `Balance` are the plain data types of the contract, re-exported so a host module never needs the core.
"""
from decimal import Decimal

from psycopg import AsyncConnection

from credit_billing.port import Applied, Balance, InProcessWallet, Pricing, WalletPort
from credit_billing.port import credits_for_cost as _credits_for_cost

__all__ = ["Applied", "Balance", "Pricing", "account_balance", "credits_for_cost", "record_usage_in", "wallet"]

wallet: WalletPort = InProcessWallet()


async def record_usage_in(
    conn: AsyncConnection,
    tenant: str,
    amount: Decimal,
    *,
    event_id: str,
    kind: str,
    pricing: Pricing | None = None,
) -> Applied:
    """Debits the usage event's credits in the CALLER's transaction (see `packages/billing/src/credit_billing/port.py` for the contract)."""
    return await wallet.record_usage_in(conn, tenant, amount, event_id=event_id, kind=kind, pricing=pricing)


async def account_balance(tenant: str) -> Balance | None:
    """The tenant's balance, or None when it has no wallet."""
    return await wallet.account_balance(tenant)


def credits_for_cost(cost_usd, credits_per_usd, markup) -> Decimal:
    """Rating, a pure function (it stays in the host's hands through spec 011, O1)."""
    return _credits_for_cost(cost_usd, credits_per_usd, markup)
