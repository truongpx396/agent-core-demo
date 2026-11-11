"""The server-sent events the streaming routes send, as models for the OpenAPI document and for nothing else.

A stream is a sequence of `data: <one JSON object>` frames and nothing else (no `event:` or `id:` field), and each object's `type` says
which. OpenAPI 3.1 has no way to say "a stream of these", so the document puts the schema of ONE event under `text/event-stream`, which is
what 3.2's `itemSchema` says properly; the route's description says so too.

Nothing builds an event through these models: events are plain dicts made in a hot loop (a `token` per token) in
app/agent/runtime/stream.py and the workers. What keeps a model from lying is the tests, which hold the events the real stream code and the
workers' own publishing make to the committed document (tests/agent/conftest.py, tests/adapters/inbound/workers/conftest.py).
"""
from typing import Annotated, Any, Literal

from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field


class EventStreamResponse(StreamingResponse):
    """For `response_class=` only, so the document lists a route's body as `text/event-stream`. FastAPI documents the body under the response
    class's media type, and StreamingResponse has none (it would say `application/json`)."""

    media_type = "text/event-stream"


# --- a chat turn: POST /chat/stream/queued, /chat/resume and /chat/cancel ---------------------------------------------------------


class Citation(BaseModel):
    marker: str = Field(description="`[1]`, `[2]`... as the answer cites it.")
    doc_id: str
    title: str
    text: str
    score: float


class ToolCall(BaseModel):
    name: str
    args: dict[str, Any]


class TokenEvent(BaseModel):
    """A piece of the answer. A `retry` event discards every token before it."""

    type: Literal["token"]
    content: str


class ToolStartEvent(BaseModel):
    type: Literal["tool_start"]
    tool: str
    args: dict[str, Any]
    subagent: str | None = Field(default=None, description="Set when the call happened inside a subagent's nested run.")


class ToolEndEvent(BaseModel):
    type: Literal["tool_end"]
    tool: str
    subagent: str | None = None


class RetryEvent(BaseModel):
    """Not terminal. Discard everything streamed so far: the answer was rejected or replaced, and fresh tokens (or a corrected one) follow."""

    type: Literal["retry"]


class CompactedEvent(BaseModel):
    """Not terminal. Informational: older context was trimmed before the model ran."""

    type: Literal["compacted"]


class CitationsEvent(BaseModel):
    """Just before `done`, when the answer cited a source."""

    type: Literal["citations"]
    items: list[Citation]
    ungrounded_claims_count: int


class FollowupsEvent(BaseModel):
    """Just before `done`, when the agent has suggestions."""

    type: Literal["followups"]
    items: list[str]


class ApprovalRequiredEvent(BaseModel):
    """Terminal for this request: the turn is paused until `POST /chat/resume` approves or rejects the calls."""

    type: Literal["approval_required"]
    tool_calls: list[ToolCall]


class DoneEvent(BaseModel):
    """Terminal: the turn finished."""

    type: Literal["done"]


class ErrorEvent(BaseModel):
    """Terminal. `code`, `message` and `details` are the canonical error envelope (app/core/errors.py): switch on `code`. They are absent on
    one event, the queue's "no agent worker answered in time", which carries only `content`."""

    type: Literal["error"]
    content: str = Field(description="A message for a person; also what a client without the envelope shows.")
    code: str | None = None
    message: str | None = None
    details: dict[str, Any] | None = None


ChatEvent = Annotated[
    TokenEvent
    | ToolStartEvent
    | ToolEndEvent
    | RetryEvent
    | CompactedEvent
    | CitationsEvent
    | FollowupsEvent
    | ApprovalRequiredEvent
    | DoneEvent
    | ErrorEvent,
    Field(discriminator="type"),
]


# --- an upload's progress: GET /ingest/stream/{job_id} -----------------------------------------------------------------------------


class IngestStartedEvent(BaseModel):
    type: Literal["started"]


class IngestProgressEvent(BaseModel):
    type: Literal["progress"]
    done: int
    total: int


class IngestDoneEvent(BaseModel):
    """Terminal."""

    type: Literal["done"]
    chunks: int


class IngestErrorEvent(BaseModel):
    """Terminal. Only a message: an upload's error is not the chat turn's envelope."""

    type: Literal["error"]
    content: str


IngestEvent = Annotated[IngestStartedEvent | IngestProgressEvent | IngestDoneEvent | IngestErrorEvent, Field(discriminator="type")]
