"""`GET /usage` — the caller's own tenant spend."""
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends

from app.agent import budgets, spend
from app.api.deps import get_ctx
from app.api.schemas import BudgetStatus, CreditBalance, UsageResponse
from app.billing import credits
from app.core.config import (
    CREDITS_ENFORCEMENT,
    CREDITS_PER_USD,
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

    `credits` is the tenant's wallet, present only when the deployment has credits on
    (`CREDITS_PER_USD`) and the tenant has a wallet. Like the budget figures it does not fail open:
    an endpoint that cannot read the wallet says so rather than report a calm "no wallet"."""
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
    wallet = await credits.account_balance(ctx["tenant"]) if CREDITS_PER_USD is not None else None
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
                available=wallet.available, debt=wallet.debt, ledger=wallet.ledger, enforced=CREDITS_ENFORCEMENT
            )
            if wallet is not None
            else None
        ),
    )
