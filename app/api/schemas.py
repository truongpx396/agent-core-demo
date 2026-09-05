"""Pydantic request/response models for the FastAPI service."""
import uuid
from datetime import datetime

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
    job_id: str = Field(..., description="Poll/stream this at GET /ingest/stream/{job_id}.")


class SessionSummary(BaseModel):
    thread_id: str = Field(..., description="Reuse as ChatRequest.thread_id to continue this session.")
    title: str = Field(..., description="The opening message that started this session, truncated.")
    created_at: datetime = Field(..., description="When this thread_id was first seen.")
    last_active_at: datetime = Field(..., description="Most recent turn on this thread_id.")


class SessionMessage(BaseModel):
    role: str = Field(..., description='"user" or "assistant".')
    text: str = Field(..., description="The message's text content.")


class HealthResponse(BaseModel):
    status: str = "ok"


class ReadinessResponse(BaseModel):
    """GET /health/ready's body — see app/api/health.py's module docstring for
    why this is a separate question from GET /health's liveness probe."""

    status: str = Field(..., description='"ready" if every check passed, else "degraded".')
    checks: dict[str, bool] = Field(
        ..., description="Per-dependency reachability (app/api/health.py::check_dependencies)."
    )


class UsageResponse(BaseModel):
    """GET /usage's body (app/agent/meter.py::usage_summary) — this caller's own
    tenant, all-time, plus the same rolling-24h number
    app/agent/runtime.py::_tenant_over_daily_budget checks before every turn, so a
    caller can see how close they are to MAX_COST_USD_PER_TENANT_PER_DAY
    without waiting to actually get refused by it."""

    total_tokens: int = Field(..., description="All-time tokens recorded for this tenant.")
    total_cost_usd: float = Field(..., description="All-time cost (USD) recorded for this tenant.")
    last_24h_cost_usd: float = Field(
        ..., description="Cost recorded in the last rolling 24h — what the daily budget check sees."
    )
    daily_budget_usd: float = Field(
        ..., description="MAX_COST_USD_PER_TENANT_PER_DAY — the ceiling last_24h_cost_usd is checked against."
    )
