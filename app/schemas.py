"""Pydantic request/response models for the FastAPI service."""
import uuid

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, description="The user's message to the agent.")
    thread_id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        description="Conversation id; reuse it across calls to keep memory.",
    )


class ChatResponse(BaseModel):
    thread_id: str = Field(..., description="Echoes the thread id (memory + Langfuse session).")
    answer: str = Field(..., description="The agent's final answer.")


class HealthResponse(BaseModel):
    status: str = "ok"
