"""Structured (JSON) logging, shared by every long-running service process
(app/api/main.py, app/turns/agent_worker.py, app/ingestion/ingest_worker.py,
app/channels/telegram.py) — NOT the interactive CLI tools (app/channels/chat.py,
scripts/hitl_demo.py) or one-shot operator scripts (scripts/seed.py, scripts/eval.py),
which print directly to the terminal for a human watching it and would just
get JSON noise interleaved with that.

Built on structlog, not a hand-rolled `logging.Formatter` — but every log
CALL site in this app already logs through the stdlib `logging` module
(`logger = logging.getLogger(__name__)`), many with a per-call
`extra={...}` dict carrying real correlation data (request_id, tool name,
error_class, tenant — see e.g. app/turns/agent_worker.py::process_request,
app/core/metrics.py::MetricsCallbackHandler). `configure_logging()` below
wires structlog's `ProcessorFormatter` onto the ROOT logger's handler
instead — the standard "structlog processes stdlib logging" recipe — so
every one of those call sites gets structlog's processor pipeline (JSON
rendering, request_id injection, exception formatting) with ZERO changes to
any individual `logger.info(...)`/`logger.warning(...)` call, exactly as
before this swap.

This doesn't change any log CALL site. Two problems this fixes, neither by
changing what's logged:

1. `logging.basicConfig(level=logging.INFO)` (what every service process
   used before this module existed) renders only the plain message string
   with the default formatter — every `extra` field is silently dropped,
   not just unstructured. `configure_logging()` below is the same level,
   with a formatter that actually surfaces those fields as real JSON keys.
2. app/api/main.py had no logging setup AT ALL — no handler anywhere in the
   root logger's chain means Python's logging module discards every
   `logger.info(...)` call outright (only WARNING+ reaches stderr, via the
   interpreter's own bare-format last-resort handler, with none of the
   `extra` fields either). Under `make serve`, this meant the majority of
   this app's own instrumentation (tool-call audit lines, HITL decisions,
   moderation outcomes, retrieval degradation, ...) was invisible, not
   merely unstructured — verified empirically: `logging.getLogger().
   handlers` is `[]` and effective level is WARNING with no setup at all.

A third, separate gap this module also closes: `request_id` (or
`thread_id`, on an in-process/non-queued path) was only ever attached to
the ONE log line at a turn's failure boundary (e.g.
app/turns/agent_worker.py::process_request's `except` clause) — every OTHER log
line touched while processing that same turn (deep inside app/agent/graph.py,
app/agent/tools.py, app/core/metrics.py's tool-call audit lines, ...) carried no
correlation id at all, making "show me every log line for turn X" not
actually answerable from the logs alone. `bind_request_id`/`request_id_var`
below fix this the standard way: a contextvar set once at the entry point
(propagates through every `await` in that same task automatically) plus a
structlog processor that reads it onto every event dict — with ZERO changes
to any individual `logger.info(...)`/`logger.warning(...)` call site.

Output shape is unchanged from the pre-structlog formatter (same JSON keys:
`timestamp`, `level`, `logger`, `message`, `request_id` when bound, plus
every `extra=` field verbatim, plus `exc_info` as a formatted traceback
string) — the one deliberate difference is `level`, now lowercase
(`"info"`, not `"INFO"`) per structlog's own convention, which the
Promtail/Loki pipeline this feeds (docker-compose.observability.yml) also
expects.
"""
import logging
import sys
import traceback
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

import structlog
from structlog.typing import EventDict, Processor, WrappedLogger

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)


@contextmanager
def bind_request_id(request_id: str) -> Iterator[None]:
    """Wrap one turn/job's processing (app/turns/agent_worker.py::process_request,
    app/ingestion/ingest_worker.py::process_job, app/api/main.py's in-process endpoints —
    `thread_id` doubles as the correlation id on those, there being no
    separate request_id concept for a non-queued turn). Every log line
    emitted anywhere during the wrapped block — including deep inside
    app/agent/graph.py/app/agent/tools.py, which have no idea this id exists — carries
    it, via `_add_request_id` below. Reset on exit (not left dangling) so
    a worker's NEXT turn on the same asyncio task doesn't inherit a stale id
    if something logs outside any `bind_request_id` block.
    """
    token = request_id_var.set(request_id)
    try:
        yield
    finally:
        request_id_var.reset(token)


def _add_request_id(
    logger: WrappedLogger, method_name: str, event_dict: EventDict
) -> EventDict:
    """structlog processor: stamps the ambient `bind_request_id` value onto
    every event, unless a call site already supplied one explicitly via
    `extra={"request_id": ...}` — mirrors the old `_RequestIdFilter`'s
    `not hasattr(record, "request_id")` check. Must run AFTER
    `structlog.stdlib.ExtraAdder` in the processor chain so an explicit
    `extra` value is already in `event_dict` by the time this checks it."""
    request_id = request_id_var.get()
    if request_id is not None and "request_id" not in event_dict:
        event_dict["request_id"] = request_id
    return event_dict


def _rename_event_to_message(
    logger: WrappedLogger, method_name: str, event_dict: EventDict
) -> EventDict:
    """structlog's own convention is an `event` key; this app's log
    consumers (this module's own tests, any existing dashboard/alert query
    over these JSON lines) expect `message`, matching the previous
    hand-rolled formatter."""
    event_dict["message"] = event_dict.pop("event")
    return event_dict


def _format_exc_info(
    logger: WrappedLogger, method_name: str, event_dict: EventDict
) -> EventDict:
    """Renders the exc_info 3-tuple ProcessorFormatter attaches for a
    stdlib `logger.exception(...)`/`logger.error(..., exc_info=True)` call
    into a formatted traceback string under the same `exc_info` key the
    previous formatter used (`Formatter.formatException`) — never the raw,
    non-JSON-serializable tuple."""
    exc_info = event_dict.pop("exc_info", None)
    if exc_info:
        if exc_info is True:
            exc_info = sys.exc_info()
        event_dict["exc_info"] = "".join(traceback.format_exception(*exc_info))
    return event_dict


# Shared by both real log records (via ProcessorFormatter's foreign_pre_chain,
# for every plain stdlib `logger.info(...)` call site in this app) and any
# native structlog call (none today, but `wrap_for_formatter` needs this
# same chain configured via structlog.configure() below regardless).
_SHARED_PROCESSORS: list[Processor] = [
    structlog.stdlib.add_logger_name,
    structlog.stdlib.add_log_level,
    structlog.processors.TimeStamper(fmt="iso", key="timestamp"),
    structlog.stdlib.ExtraAdder(),
    _add_request_id,
    _rename_event_to_message,
]


def build_formatter() -> structlog.stdlib.ProcessorFormatter:
    """Exposed (not just used internally by `configure_logging`) so tests
    can attach it to an isolated, non-propagating logger instead of going
    through the real root logger — the same isolation the old
    JSONFormatter-based fixture used, see tests/core/test_logging_config.py."""
    return structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=_SHARED_PROCESSORS,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            _format_exc_info,
            # default=str: an extra field is sometimes a non-JSON-native
            # value (an Enum, a UUID, ...) at real call sites in this app —
            # stringify rather than let one such field crash the whole log line.
            structlog.processors.JSONRenderer(default=str),
        ],
    )


class _DynamicStreamHandler(logging.StreamHandler):
    """`logging.StreamHandler()` resolves `sys.stderr` ONCE, at
    construction time, and holds that reference — a real problem if the
    process's stderr is later swapped or closed out from under it (pytest's
    per-test capture doing exactly that is what surfaced this: a
    third-party library's `__del__` finalizer logging a warning during
    interpreter shutdown, well after pytest had already closed the capture
    stream this handler was holding onto, raised "I/O operation on closed
    file" instead of printing the warning). Re-resolving `sys.stderr` on
    every emit avoids the whole class of bug — the same failure mode a
    production log-rotation setup that reopens fds could hit, not purely a
    test artifact.
    """

    def emit(self, record: logging.LogRecord) -> None:
        self.stream = sys.stderr
        super().emit(record)


def configure_logging(level: int = logging.INFO) -> None:
    """Call once, at process startup, before anything logs. `force=True`
    (stdlib 3.8+) replaces any handler a prior import may have already
    attached to the root logger (e.g. a third-party library that calls
    its own `basicConfig`) — this app's own JSON formatting should win
    regardless of import order, not silently lose a race for the root
    logger's configuration. Freely re-callable (unlike
    app/core/telemetry.py::configure_telemetry) — each call just replaces
    the root handlers again.
    """
    structlog.configure(
        processors=_SHARED_PROCESSORS
        + [structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    handler = _DynamicStreamHandler()
    handler.setFormatter(build_formatter())
    logging.basicConfig(level=level, handlers=[handler], force=True)
