"""Pydantic request/response models for the FastAPI service."""
import uuid

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, description="The user's message to the agent.")
    thread_id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        description="Conversation id; reuse it across calls to keep memory.",
    )


class Citation(BaseModel):
    marker: str = Field(..., description="The bracket marker as it appears in `answer`, e.g. '[1]'.")
    doc_id: str = Field(..., description="Source point id in Qdrant.")
    title: str = Field(..., description="Source title/topic/kind, whichever the payload had.")
    text: str = Field(..., description="The cited chunk's text.")
    score: float = Field(..., description="Retrieval/rerank relevance score.")


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


class HealthResponse(BaseModel):
    status: str = "ok"
