"""Canonical error envelope — `{code, message, details}` — the one shape
every error/terminal-state surface in this app uses (pattern 30), drawn
from a single registry (`ErrorCode`) so a caller can switch on `code`
instead of parsing free-text `message`.

Applied to operator/caller-facing surfaces: the SSE `error` event
(`runtime_stream.py::_run_graph_stream`), the queue worker's catch-all
(`job_queue/agent_worker.py::process_request`) and the CLI's error text
(`app/channels/chat.py`). NOT applied to `ToolMessage` content — a failing
tool's message to the LLM (`graph_utils.py::_friendly_tool_error`) is
natural-language by design, a different audience.

The envelope's `message` is written for the *caller*, never for the
operator. An unexpected exception's own text is the operator's business —
a driver error can name an internal host, a SQL fragment or a credential-
shaped DSN, and a library message can echo user input — so it goes to the
trace/logs and never into an envelope; use `internal_error_envelope` for
any failure that has no dedicated `ErrorCode`.
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
    PERSONAL_BUDGET_EXCEEDED = "personal_budget_exceeded"
    BUDGET_CHECK_UNAVAILABLE = "budget_check_unavailable"
    INSUFFICIENT_CREDITS = "insufficient_credits"
    PROVIDER_BUDGET_EXCEEDED = "provider_budget_exceeded"
    MODEL_UNPRICED = "model_unpriced"
    THREAD_BUSY = "thread_busy"
    NO_PROGRESS = "no_progress"
    WORKER_LOST = "worker_lost"
    INTERNAL = "internal"


class TurnCancelled(Exception):
    """Raised by `runtime_stream.py::_iterate_with_timeout` when `cancel_check`
    reports a user-initiated stop mid-turn (the "actively streaming, not
    paused at approval" case — a paused run is cancelled directly via
    `cancel_run` instead). Caught specifically in `_run_graph_stream`, apart
    from the generic `except Exception`, so it reports `ErrorCode.CANCELLED`
    instead of looking like an unexpected failure."""


INTERNAL_ERROR_MESSAGE = (
    "Something went wrong on our side while handling this request. "
    "Please try again; if it keeps happening, contact support."
)


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


def internal_error_envelope(exc: BaseException) -> ErrorEnvelope:
    """The caller-safe envelope for an unexpected failure (`ErrorCode.INTERNAL`):
    a fixed message plus the exception *class name* — metadata that lets an
    operator triaging a client report match it to a log line — and never
    `str(exc)`, which routinely carries internal hostnames, SQL, DSNs or
    echoed user input (pattern 30)."""
    return ErrorEnvelope(
        code=ErrorCode.INTERNAL,
        message=INTERNAL_ERROR_MESSAGE,
        details={"error_class": type(exc).__name__},
    )
