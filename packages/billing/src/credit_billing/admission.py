"""The credit gate: may this tenant start another unit of work on its prepaid balance? (specs/010 T016, moved
here by specs/013.)

It lived in `app/agent/cost/budgets.py` with the rate, markup and failure policy passed in; it is the
package's now, because every part of it is billing. A host asks one question, `admit(tenant,
reserved_usd=...)`, and maps the answer onto its own refusal.

A tenant that HAS a wallet (a `credit_accounts` row) is refused (`exhausted`) once its available credits minus
the caller's in-flight holds are not positive. A tenant with no wallet is never gated, so this changes nothing
until an operator, or a verified purchase, opens one.

- **Holds are in credits.** The caller reserves dollars while work runs (the agent: `MAX_COST_USD_PER_TURN`
  per running turn) and the gate converts them at the configured rate, so a tenant with 100 credits can't
  start a second concurrent turn that may spend 500. The first is checked against the balance alone and can
  overdraw by at most what it spends; the wallet books that as debt (`credits.debit_in`) and the next grant
  repays it. `reserved_usd` is a callable awaited only for a tenant with a wallet, so most-tenants-have-none
  deployments pay for no hold read.
- **Off means untouched.** With `CREDITS_ENFORCEMENT` off nothing is read. Shadow mode (a rate set,
  enforcement off) still debits, in `usage.py`, but doesn't refuse.
- **An unreadable wallet** follows `CREDIT_CHECK_FAILURE_POLICY`, separate from the host's spend-read policy
  because the two fail separately. "open" serves the caller (`degraded`) and counts
  `agent_cost_governance_degraded_total{path="credit_read"}` (alert `CreditGateUnenforced`); "closed" refuses
  (`unavailable`), blaming no one's balance. The hold read is the caller's and degrades to 0 on its own
  failure.

Imports nothing from `app` (enforced by tests/test_boundary.py).
"""
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from credit_billing import credits, metrics
from credit_billing.settings import (
    CREDIT_CHECK_FAILURE_POLICY,
    CREDITS_ENFORCEMENT,
    CREDITS_PER_USD,
    MARKUP,
)

logger = logging.getLogger(__name__)

Status = Literal["allowed", "exhausted", "unavailable", "degraded"]


@dataclass(frozen=True)
class Admission:
    """The gate's answer. `available` and `reserved` (both credits) are set only for `exhausted`, the one answer a person is told about."""

    status: Status
    available: Decimal | None = None
    reserved: Decimal | None = None

    @property
    def refused(self) -> bool:
        return self.status in ("exhausted", "unavailable")


async def admit(tenant: str, *, reserved_usd: Callable[[], Awaitable[Decimal | float]]) -> Admission:
    """Whether `tenant` may start another unit of work. Never raises for a wallet fault (that is `unavailable` or `degraded` by policy)."""
    if not CREDITS_ENFORCEMENT:
        return Admission("allowed")
    if CREDITS_PER_USD is None:
        raise RuntimeError("CREDITS_ENFORCEMENT is on but CREDITS_PER_USD is not set")
    try:
        wallet = await credits.account_balance(tenant)
    except Exception as exc:  # noqa: BLE001 - a wallet read failing must not by itself take every request down; CREDIT_CHECK_FAILURE_POLICY decides, and it is counted and alerted either way
        metrics.agent_cost_governance_degraded_total.labels(path="credit_read").inc()
        logger.warning("credit_check_failed", extra={"error_class": type(exc).__name__, "fail_policy": CREDIT_CHECK_FAILURE_POLICY})
        return Admission("unavailable") if CREDIT_CHECK_FAILURE_POLICY == "closed" else Admission("degraded")
    if wallet is None:
        return Admission("allowed")  # no wallet: not on credit billing, never gated
    reserved = credits.credits_for_cost(await reserved_usd(), CREDITS_PER_USD, MARKUP)
    if wallet.available - reserved > 0:
        return Admission("allowed")
    metrics.agent_credit_enforcement_refused_total.inc()
    logger.warning(
        "credits_exhausted", extra={"tenant": tenant, "available_credits": str(wallet.available), "reserved_credits": str(reserved)}
    )
    return Admission("exhausted", available=wallet.available, reserved=reserved)
