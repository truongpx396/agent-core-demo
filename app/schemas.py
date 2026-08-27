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


class Citation(BaseModel):
    marker: str = Field(..., description="The bracket marker as it appears in `answer`, e.g. '[1]'.")
    doc_id: str = Field(..., description="Source point id in Qdrant.")
    title: str = Field(..., description="Source title/topic/kind, whichever the payload had.")
    text: str = Field(..., description="The cited chunk's text.")
    score: float = Field(..., description="Retrieval/rerank relevance score.")


class ErrorEnvelopeModel(BaseModel):
    """Mirrors app/errors.py::ErrorEnvelope — the canonical `{code, message,
    details}` shape (GRAPH_PATTERNS.md pattern 30). A separate Pydantic
    model rather than reusing the dataclass directly: this is the
    HTTP-facing schema (drives the OpenAPI docs), the dataclass is the
    internal one; keeping them distinct means a change to one doesn't
    silently change the other's wire shape."""

    code: str = Field(..., description="Stable error code — switch on this, not `message`.")
    message: str = Field(..., description="Human-readable explanation. Free text; may change.")
    details: dict | None = Field(default=None, description="Optional structured context.")


class ChatResponse(BaseModel):
    thread_id: str = Field(..., description="Echoes the thread id (memory + Langfuse session).")
    answer: str = Field(..., description="The agent's final answer.")
    citations: list[Citation] = Field(
        default_factory=list,
        description=(
            "Sources the answer actually cited (by bracket marker) — a subset "
            "of everything retrieved, filtered post-hoc from the model's "
            "output text rather than trusted from a self-report (GRAPH_PATTERNS.md "
            "pattern 20)."
        ),
    )
    error: ErrorEnvelopeModel | None = Field(
        default=None,
        description=(
            "Set only when the turn ended in a genuine failure (timeout, a "
            "stale/incompatible checkpoint refused to resume into) — `None` "
            "for a normal success, including a successful HITL auto-decline "
            "(GRAPH_PATTERNS.md pattern 30)."
        ),
    )
    ungrounded_claims_count: int = Field(
        default=0,
        description=(
            "How many [n] markers in `answer` don't match any real citation "
            "— the model inventing a source number. A structural field, "
            "always present; 0 is itself meaningful (no hallucinated "
            "citations this turn), not the absence of a signal "
            "(GRAPH_PATTERNS.md pattern 39)."
        ),
    )


class HealthResponse(BaseModel):
    status: str = "ok"


class ReadinessResponse(BaseModel):
    """GET /health/ready's body — see app/health.py's module docstring for
    why this is a separate question from GET /health's liveness probe."""

    status: str = Field(..., description='"ready" if every check passed, else "degraded".')
    checks: dict[str, bool] = Field(
        ..., description="Per-dependency reachability (app/health.py::check_dependencies)."
    )


class UsageResponse(BaseModel):
    """GET /usage's body (app/meter.py::usage_summary) — this caller's own
    tenant, all-time, plus the same rolling-24h number
    app/agent.py::_tenant_over_daily_budget checks before every turn, so a
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
