"""The one module host code imports to use billing (specs/011-billing-module-boundary, D19; widened by
specs/013-billing-owns-the-meter).

Outside `packages/billing/src/credit_billing/` and `app/billing_integration/`, nothing under `app/` or
`scripts/` imports `credit_billing` (tests/billing/test_billing_core_boundary.py enforces it), so this is
where the agent app meets billing. The agent hands it plain facts and gets plain answers; rating, the wallet,
the export and the gate's policy are the package's:

  * `record_usage_in(conn, Usage(...))`: once per metered model call, inside the transaction that records the
    agent's own usage event (`app/agent/cost/usage_events.py`). The package rates it, keeps its own record,
    debits the wallet and queues an export, and never raises for a fault. With billing not configured it does
    nothing.
  * `admit(tenant, reserved_usd=...)`: asks the credit gate whether the tenant may start another turn
    (`app/agent/cost/budgets.py`). With enforcement off it reads nothing.
  * `credit_summary(tenant)`: what `GET /usage` shows of the wallet, or None.

`billing` is the implementation in use: today `InProcessBilling`, a thin call into the package over the
caller's pool. A network client implementing the same `BillingPort` would be selected here, and a test
installs a stub with `monkeypatch.setattr(billing_port, "billing", stub)`: a module-level name read at CALL
time, the repo's idiom for a seam a test replaces. The functions below add nothing; they let call sites read
as "the billing port" and look the implementation up on every call.

Importing this module first imports the `app.billing_integration` package, which wires the package to the
agent's pool, so a host path reaching billing through here can't reach it unwired.

`Usage`, `Recorded`, `Admission`, `CreditSummary` and `Balance` are the contract's plain data types,
re-exported so a host module never needs the package.
"""
import logging
from collections.abc import Awaitable, Callable
from decimal import Decimal

from psycopg import AsyncConnection

from credit_billing import metrics as billing_metrics
from credit_billing.port import (
    Admission,
    Balance,
    BillingPort,
    CreditSummary,
    InProcessBilling,
    Recorded,
    Usage,
)

__all__ = ["Admission", "Balance", "CreditSummary", "Recorded", "Usage", "account_balance", "admit", "billing", "credit_summary", "record_usage_in"]

logger = logging.getLogger(__name__)

billing: BillingPort = InProcessBilling()


async def record_usage_in(conn: AsyncConnection, usage: Usage) -> Recorded:
    """Records one unit of usage in the CALLER's transaction (see `packages/billing/src/credit_billing/port.py` for the contract).

    The package never raises for an operational fault, and every way it can fail is counted. This wrapper is the second line, for a fault the
    package did not foresee (a bug, a different implementation behind the port): the caller's own record is already written in the same
    transaction, and an exception here would roll it back, so it is counted on the package's own path (`billing_event_write`, which pages) and
    swallowed. A fault that left the transaction aborted surfaces at the caller's next statement or commit, exactly as it would have anyway."""
    try:
        return await billing.record_usage_in(conn, usage)
    except Exception as exc:  # noqa: BLE001 - billing must never take down the caller's own record; counted and alerted instead (BillingEventWriteFailing)
        billing_metrics.agent_cost_governance_degraded_total.labels(path="billing_event_write").inc()
        logger.warning("billing_usage_hand_off_failed", extra={"error_class": type(exc).__name__})
        return Recorded("failed")


async def account_balance(tenant: str) -> Balance | None:
    """The tenant's balance, or None when it has no wallet."""
    return await billing.account_balance(tenant)


async def admit(tenant: str, *, reserved_usd: Callable[[], Awaitable[Decimal | float]]) -> Admission:
    """Whether the tenant may start more work on its prepaid balance. Reads nothing when enforcement is off."""
    return await billing.admit(tenant, reserved_usd=reserved_usd)


async def credit_summary(tenant: str) -> CreditSummary | None:
    """What to show a tenant of its wallet, or None when credits are off or it has no wallet. Raises when the wallet cannot be read."""
    return await billing.credit_summary(tenant)
