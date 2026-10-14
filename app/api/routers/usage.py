"""`GET /usage` — the caller's own tenant spend."""
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends

from app.agent import budgets, spend
from app.api.deps import get_ctx
from app.api.schemas import BudgetStatus, CreditBalance, UsageResponse
from app.billing_integration import port as billing_port
from app.core.config import (
    MAX_COST_USD_PER_PRINCIPAL_PER_DAY,
    MAX_COST_USD_PER_PRINCIPAL_PER_MONTH,
    MAX_COST_USD_PER_TENANT_PER_DAY,
    MAX_COST_USD_PER_TENANT_PER_MONTH,
)
from app.core.security import SecurityCtx

router = APIRouter()


@router.get("/usage", response_model=UsageResponse)
async def usage(ctx: SecurityCtx = Depends(get_ctx)) -> UsageResponse:
    """This caller's own tenant usage — exposes
    `spend.usage_summary` (the usage events) over HTTP, so a caller can see how close they are to
    MAX_COST_USD_PER_TENANT_PER_DAY without getting refused first. `budgets` adds every limit
    that applies to this caller (the tenant's and their own, overrides included).
    Tenant-scoped only; no way to query another tenant's spend, or another person's.

    `credits` is the tenant's wallet, present only when the deployment has credits on and the tenant has
    a wallet (the billing port decides; this endpoint only shows what it is given). Like the budget figures it
    does not fail open: an endpoint that cannot read the wallet says so rather than report a calm "no wallet"."""
    all_time = await spend.usage_summary(ctx["tenant"])
    since = datetime.now(UTC) - timedelta(hours=24)
    last_24h = await spend.usage_summary(ctx["tenant"], since=since)
    statuses = await budgets.usage_status(
        ctx,
        defaults=budgets.Defaults(
            tenant_day=MAX_COST_USD_PER_TENANT_PER_DAY,
            tenant_month=MAX_COST_USD_PER_TENANT_PER_MONTH,
            principal_day=MAX_COST_USD_PER_PRINCIPAL_PER_DAY,
            principal_month=MAX_COST_USD_PER_PRINCIPAL_PER_MONTH,
        ),
    )
    wallet = await billing_port.credit_summary(ctx["tenant"])
    return UsageResponse(
        total_tokens=all_time["total_tokens"],
        total_cost_usd=all_time["total_cost_usd"],
        last_24h_cost_usd=last_24h["total_cost_usd"],
        daily_budget_usd=MAX_COST_USD_PER_TENANT_PER_DAY,
        budgets=[
            BudgetStatus(
                scope=status.scope,
                window=status.window,
                limit_usd=status.limit_usd,
                spent_usd=status.spent_usd,
                remaining_usd=status.remaining_usd,
                resets_at=status.resets_at,
            )
            for status in statuses
        ],
        credits=(
            CreditBalance(
                available=wallet.balance.available, debt=wallet.balance.debt, ledger=wallet.balance.ledger, enforced=wallet.enforced
            )
            if wallet is not None
            else None
        ),
    )
