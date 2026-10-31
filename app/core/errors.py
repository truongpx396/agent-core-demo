"""Canonical error envelope — `{code, message, details}` — the one shape
every error/terminal-state surface in this app uses (pattern 30), drawn
from a single registry (`ErrorCode`) so a caller can switch on `code`
instead of parsing free-text `message`.

Applied to operator/caller-facing surfaces: the SSE `error` event
(`runtime/stream.py::_run_graph_stream`), the queue worker's catch-all
(`job_queue/agent_worker.py::process_request`) and the CLI's error text
(`app/channels/chat.py`). NOT applied to `ToolMessage` content — a failing
tool's message to the LLM (`graph/utils.py::friendly_tool_error`) is
natural-language by design, a different audience.

The envelope's `message` is written for the *caller*, never for the
operator. An unexpected exception's own text is the operator's business —
a driver error can name an internal host, a SQL fragment or a credential-
shaped DSN, and a library message can echo user input — so it goes to the
trace/logs and never into an envelope; use `internal_error_envelope` for
any failure that has no dedicated `ErrorCode`.
"""
import os
import traceback
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
    EMBEDDING_IDENTITY_MISMATCH = "embedding_identity_mismatch"
    THREAD_BUSY = "thread_busy"
    NO_PROGRESS = "no_progress"
    WORKER_LOST = "worker_lost"
    # The four the front door (the authlimit service, specs/018) answers with, in the same envelope; the API itself emits the first and
    # the last (a request that reaches it with no valid identity, or while it cannot check one). See AUTH_ERRORS.
    UNAUTHENTICATED = "unauthenticated"
    FORBIDDEN = "forbidden"
    RATE_LIMITED = "rate_limited"
    AUTH_UNAVAILABLE = "auth_unavailable"
    INTERNAL = "internal"


class TurnCancelled(Exception):
    """Raised by `runtime/stream.py::_iterate_with_timeout` when `cancel_check`
    reports a user-initiated stop mid-turn (the "actively streaming, not
    paused at approval" case — a paused run is cancelled directly via
    `cancel_run` instead). Caught specifically in `_run_graph_stream`, apart
    from the generic `except Exception`, so it reports `ErrorCode.CANCELLED`
    instead of looking like an unexpected failure."""


INTERNAL_ERROR_MESSAGE = (
    "Something went wrong on our side while handling this request. "
    "Please try again; if it keeps happening, contact support."
)


# What the front door says for each of its codes: the HTTP status and the message, word for word. the Go service keeps its own copy
# of this table in a JSON file and the two are held equal by tests/core/test_error_codes_authlimit.py, so a person sees the same words
# whichever layer refused them. (The messages are written for the caller: none carries a token, a claim, or an exception's text.)
AUTH_ERRORS: dict[ErrorCode, tuple[int, str]] = {
    ErrorCode.UNAUTHENTICATED: (401, "Authentication is required: send a valid access token."),
    ErrorCode.FORBIDDEN: (403, "The access token does not allow this request."),
    ErrorCode.RATE_LIMITED: (429, "Too many requests. Wait and try again."),
    ErrorCode.AUTH_UNAVAILABLE: (503, "Authentication is temporarily unavailable. Try again shortly."),
    ErrorCode.INTERNAL: (500, INTERNAL_ERROR_MESSAGE),
}


def error_origin(exc: BaseException) -> str:
    """Where `exc` was raised, as `app/agent/runtime/stream.py:612 in _run_graph_stream`: the innermost frame in this repo's code
    (else the innermost frame at all). Never the message: an exception's text can name an internal
    host, a SQL fragment or a DSN (pattern 30), which is why the logs carry only its class, and the
    class alone cannot say where a bare `IndexError` came from."""
    frames = traceback.extract_tb(exc.__traceback__)
    if not frames:
        return "unknown"
    ours = [f for f in frames if "/app/" in f.filename.replace(os.sep, "/")]
    frame = (ours or frames)[-1]
    path = frame.filename.replace(os.sep, "/")
    short = path[path.rindex("/app/") + 1 :] if "/app/" in path else os.path.basename(path)
    return f"{short}:{frame.lineno} in {frame.name}"


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


# What a data store's DRIVER raises (psycopg's `Error`, today). The kernel must not import a driver, so the composition root registers the
# base classes of the drivers it wires (`app/bootstrap.py`), and the kernel asks `is_store_error`. The reason it needs to ask: a driver's
# message carries the failing statement and names tables and columns, and a tool's failure text is read by the model and repeated to the
# person, so the message of a store error must never reach a prompt (`app/agent/graph/utils.py::friendly_tool_error`).
_STORE_ERRORS: list[type[BaseException]] = []


def register_store_error(error_class: type[BaseException]) -> None:
    """Declares `error_class` (and its subclasses) a data-store driver error. Idempotent."""
    if error_class not in _STORE_ERRORS:
        _STORE_ERRORS.append(error_class)


def is_store_error(exc: BaseException) -> bool:
    return bool(_STORE_ERRORS) and isinstance(exc, tuple(_STORE_ERRORS))
