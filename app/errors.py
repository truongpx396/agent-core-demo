"""Canonical error envelope — `{code, message, details}` — the one shape
every meaningful error/terminal-state surface in this app uses
(GRAPH_PATTERNS.md pattern 30), drawn from a single registry (`ErrorCode`)
rather than each call site inventing its own string. An integrating
caller can switch on `code` (a small, stable enum) without parsing
`message` (free text, safe to reword) or pattern-matching prose to tell
"was this a timeout" from "was this a stale checkpoint" from "was this
moderation."

Applied to this app's own OPERATOR/CALLER-facing surfaces: the SSE
`error` event (`app/agent.py::_run_graph_stream`), `ChatResponse.error`
(`app/schemas.py`, `app/api.py`), and the CLI's printed error text
(`app/chat.py`). Deliberately NOT applied to `ToolMessage` content — what
a failing tool returns to the LLM (`app/graph.py::_friendly_tool_error`)
is natural-language by design, read and reacted to by the model, a
different audience than an operator or an integrating caller.
"""
from dataclasses import asdict, dataclass
from enum import Enum


class ErrorCode(str, Enum):
    TIMEOUT = "timeout"
    MODERATION_BLOCKED = "moderation_blocked"
    CHECKPOINT_LOST = "checkpoint_lost"
    CHECKPOINT_INCOMPATIBLE = "checkpoint_incompatible"
    UNATTENDED_PAUSE = "unattended_pause"
    CANCELLED = "cancelled"
    COST_CEILING_EXCEEDED = "cost_ceiling_exceeded"
    NO_PROGRESS = "no_progress"
    INTERNAL = "internal"


@dataclass(frozen=True)
class ErrorEnvelope:
    code: ErrorCode
    message: str
    details: dict | None = None

    def to_dict(self) -> dict:
        """JSON-safe: `code` (an Enum member) becomes its plain string
        value, so this can be dropped straight into an SSE `data: ...`
        payload or a Pydantic response field without a custom encoder."""
        d = asdict(self)
        d["code"] = self.code.value
        return d
