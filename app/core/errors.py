"""Canonical error envelope — `{code, message, details}` — the one shape
every error/terminal-state surface in this app uses (pattern 30), drawn
from a single registry (`ErrorCode`) so a caller can switch on `code`
instead of parsing free-text `message`.

Applied to operator/caller-facing surfaces: the SSE `error` event
(`runtime_stream.py::_run_graph_stream`) and the CLI's error text
(`app/channels/chat.py`). NOT applied to `ToolMessage` content — a failing
tool's message to the LLM (`graph_utils.py::_friendly_tool_error`) is
natural-language by design, a different audience.
"""
from dataclasses import asdict, dataclass
from enum import Enum


class ErrorCode(str, Enum):
    TIMEOUT = "timeout"
    MODERATION_BLOCKED = "moderation_blocked"
    CHECKPOINT_LOST = "checkpoint_lost"
    CHECKPOINT_INCOMPATIBLE = "checkpoint_incompatible"
    PENDING_APPROVAL = "pending_approval"
    UNATTENDED_PAUSE = "unattended_pause"
    CANCELLED = "cancelled"
    COST_CEILING_EXCEEDED = "cost_ceiling_exceeded"
    TENANT_BUDGET_EXCEEDED = "tenant_budget_exceeded"
    NO_PROGRESS = "no_progress"
    INTERNAL = "internal"


class TurnCancelled(Exception):
    """Raised by `runtime_stream.py::_iterate_with_timeout` when `cancel_check`
    reports a user-initiated stop mid-turn (the "actively streaming, not
    paused at approval" case — a paused run is cancelled directly via
    `cancel_run` instead). Caught specifically in `_run_graph_stream`, apart
    from the generic `except Exception`, so it reports `ErrorCode.CANCELLED`
    instead of looking like an unexpected failure."""


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
