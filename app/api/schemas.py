"""Pydantic request/response models for the FastAPI service."""
import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, description="The user's message to the agent.")
    thread_id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        description="Conversation id; reuse it across calls to keep memory.",
    )
    images: list[str] = Field(
        default_factory=list,
        description=(
            "Optional image URLs or data URIs to attach to this turn "
            "(GRAPH_PATTERNS.md pattern 44) — passed straight through to "
            "whichever model is configured behind CHAT_MODEL; a "
            "non-vision-capable model simply can't act on them. Never "
            "fetched or decoded by this app itself."
        ),
    )


class ResumeRequest(BaseModel):
    thread_id: str = Field(..., description="The paused conversation's thread id.")
    approved: bool = Field(
        ..., description="Approve (true) or reject (false) the pending tool call(s)."
    )


class CancelRequest(BaseModel):
    thread_id: str = Field(
        ...,
        description=(
            "The conversation's thread id to cancel — whether it's actively "
            "streaming right now or paused at human_approval; POST /chat/cancel "
            "handles both cases from this one field."
        ),
    )


class IngestUploadResult(BaseModel):
    filename: str = Field(..., description="The uploaded file's original name.")
    job_id: str | None = Field(
        None, description="Poll/stream this at GET /ingest/stream/{job_id}. None if this file failed."
    )
    error: str | None = Field(
        None,
        description=(
            "Set instead of job_id if THIS file failed (bad type, upload, or "
            "publish failure) — other files in the same request still succeed "
            "independently; see ingest_upload's own docstring."
        ),
    )


class SessionSummary(BaseModel):
    thread_id: str = Field(..., description="Reuse as ChatRequest.thread_id to continue this session.")
    title: str = Field(..., description="The opening message that started this session, truncated.")
    created_at: datetime = Field(..., description="When this thread_id was first seen.")
    last_active_at: datetime = Field(..., description="Most recent turn on this thread_id.")


class SessionMessage(BaseModel):
    role: str = Field(
        ...,
        description='"user" or "assistant", or "system" for a compact_history '
        "breadcrumb noting that older turns were trimmed/summarized.",
    )
    text: str = Field(..., description="The message's text content.")


class PendingApproval(BaseModel):
    tool_calls: list[dict] = Field(
        ..., description="The tool call(s) awaiting approval, same shape as the approval_required SSE event."
    )
    resumable: bool = Field(
        ...,
        description="False if this checkpoint's state_schema_version no longer matches the running "
        "build — approve/reject would be refused; the caller should show it as unresumable, not a working button.",
    )


class HealthResponse(BaseModel):
    status: str = "ok"


class ReadinessResponse(BaseModel):
    """GET /health/ready's body — see app/api/health.py's module docstring for
    why this is a separate question from GET /health's liveness probe."""

    status: str = Field(..., description='"ready" if every check passed, else "degraded".')
    checks: dict[str, bool] = Field(
        ..., description="Per-dependency reachability (app/api/health.py::check_dependencies)."
    )


class BudgetStatus(BaseModel):
    """One spend limit that applies to the caller, with how much of it is used."""

    scope: str = Field(..., description='"tenant" (the whole organisation) or "principal" (this caller alone).')
    window: str = Field(..., description='"day" (rolling 24h) or "month" (the calendar month, UTC).')
    limit_usd: float = Field(..., description="The cap, after any per-tenant or per-person override.")
    spent_usd: float = Field(..., description="Recorded spend inside the window.")
    remaining_usd: float = Field(
        ..., description="What is left, counting in-flight turns for a tenant limit; never below 0."
    )
    resets_at: datetime | None = Field(
        None, description="When a monthly window next starts from zero; null for the rolling day."
    )


class CreditBalance(BaseModel):
    """The caller's tenant's wallet (app/billing/credits.py). Exact decimals, serialised as strings:
    a JSON number would round-trip through a float and a balance must not drift."""

    available: Decimal = Field(
        ..., description="What may still be consumed right now: live (unexpired) credits minus any debt. Can be negative."
    )
    debt: Decimal = Field(..., description="Credits consumed beyond the balance, repaid first by the next grant; 0 if none.")
    ledger: Decimal = Field(
        ..., description="Every lot including expired ones not yet swept, plus debt: the ledger's own total."
    )
    enforced: bool = Field(
        ..., description="Whether a turn is refused (insufficient_credits) when `available` is not positive."
    )


class UsageResponse(BaseModel):
    """GET /usage's body — this caller's tenant, all-time plus the same
    rolling-24h number budgets.check_tenant_daily checks, so a caller can see
    how close they are to the daily budget without getting refused first."""

    total_tokens: int = Field(..., description="All-time tokens recorded for this tenant.")
    total_cost_usd: float = Field(..., description="All-time cost (USD) recorded for this tenant.")
    last_24h_cost_usd: float = Field(
        ..., description="Cost recorded in the last rolling 24h — what the daily budget check sees."
    )
    daily_budget_usd: float = Field(
        ..., description="MAX_COST_USD_PER_TENANT_PER_DAY — the ceiling last_24h_cost_usd is checked against."
    )
    budgets: list[BudgetStatus] = Field(
        default_factory=list,
        description=(
            "Every spend limit that applies to THIS caller — the tenant's and the caller's own, "
            "overrides included — with how much is used, so they can see how close they are "
            "before being refused. Only the caller's own personal figures; never another person's."
        ),
    )
    credits: CreditBalance | None = Field(
        None,
        description=(
            "The tenant's credit wallet, or null when the deployment has credits off or this tenant has "
            "no wallet (it is then never charged or gated). Tenant-wide, like the cost figures above."
        ),
    )
