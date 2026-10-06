"""The spend allowance: may this tenant start another turn? (spec 008, GRAPH_PATTERNS.md
patterns 26 and 35.)

Distinct from `MAX_COST_USD_PER_TURN` (graph_routing.py::should_continue), which only
tracks one turn's own running total: this is the cross-turn accumulator, checked before a
turn starts so an over-budget tenant is refused before it reaches the LLM/tool loop.

Extracted from runtime.py, which owns the graph singleton and had no business owning a
billing rule. Everything here takes its limits as arguments, so the decision is a plain
function of (ctx, ledger, holds); runtime.py's thin wrappers read the configured values at
call time, which is what lets tests re-point them.

Why the check adds in-flight holds to the ledger sum: `spent` (usage_ledger's persisted sum)
only reflects turns that have already COMPLETED and recorded their cost. A sibling turn for
the same tenant that is already running has not landed its row yet, so without counting it
N concurrent turns would all read the same stale `spent`, all pass, and all proceed — a
check-then-act race. Every turn reserves `MAX_COST_USD_PER_TURN` for its duration
(`usage_ledger.reserve_budget`), so this check can see a burst the ledger alone would miss.

Two ways the check can fail to answer, and what each does:
  * the ledger READ fails — governed by `BUDGET_CHECK_FAILURE_POLICY`. "open" (the default)
    serves the turn and counts the failure (`agent_cost_governance_degraded_total`, alert
    `TenantAllowanceUnenforced`): a ledger outage must not also take down every turn. "closed"
    refuses it with `ErrorCode.BUDGET_CHECK_UNAVAILABLE`: with real money behind the ceiling,
    some deployments would rather refuse than run unmetered. Either way it is a decision, not
    an accident.
  * the in-flight hold read fails — always open, to 0.0, counted. It only closes a race
    between concurrent turns; the ledger check underneath is still enforced, so a hold-table
    hiccup degrades to the pre-reservation behaviour instead of failing the whole check.
"""
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from app.agent import usage_ledger
from app.core import metrics
from app.core.errors import ErrorCode, ErrorEnvelope
from app.core.security import SecurityCtx, valid_ctx

logger = logging.getLogger(__name__)

# The rolling window of the tenant daily allowance: now - 24h, not a calendar day, so a
# tenant's near-limit status never resets mid-day.
DAILY_WINDOW = timedelta(hours=24)


@dataclass(frozen=True)
class Allowance:
    """The answer to "may this turn start?".

    `status` is "ok", "exceeded" (the tenant has used its allowance), or "unavailable" (the
    check could not be made AND the policy is "closed"). `degraded` marks an "ok" that was
    granted only because a read failed under the "open" policy, so a caller or a test can tell
    a verified pass from an unverified one."""

    status: Literal["ok", "exceeded", "unavailable"]
    spent_usd: float = 0.0
    reserved_usd: float = 0.0
    limit_usd: float = 0.0
    degraded: bool = False

    @property
    def refused(self) -> bool:
        return self.status != "ok"


async def check_tenant_daily(
    ctx: SecurityCtx | None,
    *,
    limit_usd: float,
    warning_fraction: float,
    fail_policy: str,
) -> Allowance:
    """Spend over the trailing 24h (`usage_ledger`) plus in-flight holds, against `limit_usd`.

    An invalid ctx is unattributable, so there is nothing to meter: "ok", without a ledger
    read. At or past the limit the turn is "exceeded" (counted, and logged with the tenant
    and principal, which the counter deliberately has no label for). Past `warning_fraction`
    of it the turn proceeds but is counted and logged, an early signal before refusals begin.
    """
    if not valid_ctx(ctx):
        return Allowance("ok")

    try:
        since = datetime.now(UTC) - DAILY_WINDOW
        spent = (await usage_ledger.usage_summary(ctx["tenant"], since=since))["total_cost_usd"]
    except Exception as exc:  # noqa: BLE001 - a ledger read failing must not by itself take every turn down; BUDGET_CHECK_FAILURE_POLICY decides, and it is counted and alerted either way
        # While this is firing under "open" the allowance is UNENFORCED for every turn that
        # hits it (alert TenantAllowanceUnenforced, spec 008 A1).
        metrics.agent_cost_governance_degraded_total.labels(path="ledger_read").inc()
        logger.warning(
            "tenant_budget_check_failed",
            extra={"error_class": type(exc).__name__, "fail_policy": fail_policy},
        )
        if fail_policy == "closed":
            return Allowance("unavailable", limit_usd=limit_usd)
        return Allowance("ok", limit_usd=limit_usd, degraded=True)

    reserved = await usage_ledger.in_flight_reservation(ctx["tenant"])
    projected = spent + reserved

    if projected >= limit_usd:
        metrics.agent_tenant_budget_exceeded_total.inc()
        logger.warning(
            "tenant_budget_exceeded",
            extra={
                "tenant": ctx["tenant"],
                "principal": ctx["principal"],
                "spent_usd": spent,
                "reserved_usd": reserved,
                "limit_usd": limit_usd,
            },
        )
        return Allowance("exceeded", spent, reserved, limit_usd)
    if projected >= warning_fraction * limit_usd:
        metrics.agent_tenant_budget_warning_total.inc()
        logger.warning(
            "tenant_approaching_daily_budget",
            extra={
                "tenant": ctx["tenant"],
                "spent_usd": spent,
                "reserved_usd": reserved,
                "limit_usd": limit_usd,
            },
        )
    return Allowance("ok", spent, reserved, limit_usd)


def refusal_envelope(allowance: Allowance) -> ErrorEnvelope:
    """The caller-facing error for a refused `allowance`. Never call it for an "ok" one."""
    if allowance.status == "unavailable":
        return ErrorEnvelope(
            code=ErrorCode.BUDGET_CHECK_UNAVAILABLE,
            message="Usage could not be verified right now, so this request was not started. Please try again shortly.",
        )
    return ErrorEnvelope(
        code=ErrorCode.TENANT_BUDGET_EXCEEDED,
        message="This tenant's daily usage budget has been reached. Please try again later.",
    )
