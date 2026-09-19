"""Structured (JSON) logging, shared by every long-running service process
(app/api/main.py, agent_worker.py, ingest_worker.py, app/channels/telegram.py)
— NOT the interactive CLI (app/channels/chat.py) or one-shot scripts, which
print for a human and would just get JSON noise interleaved.

Built on structlog, but no log call site changes: every call already goes
through stdlib `logging` (`logger = logging.getLogger(__name__)`) with
per-call `extra={...}` correlation data. `configure_logging()` wires
structlog's `ProcessorFormatter` onto the ROOT logger's handler (the
standard "structlog processes stdlib logging" recipe), so every call site
gets JSON rendering, request_id injection, and exception formatting for free.

Three problems this fixes, without changing what's logged:
1. Plain `logging.basicConfig` silently drops every `extra` field —
   unstructured, not just plain-text.
2. app/api/main.py previously had no logging handler at all under
   `make serve` — Python's logging module discarded every `logger.info(...)`
   call outright (only WARNING+ reached stderr, with no `extra` fields).
3. `request_id`/`thread_id` was only ever attached at a turn's failure
   boundary — every other log line for that same turn had no correlation
   id. `bind_request_id`/`request_id_var` fix this: a contextvar set once
   at the entry point (propagates through every `await` in that task) plus
   a structlog processor that reads it onto every event dict.

Output JSON keys are unchanged from the old formatter (`timestamp`,
`level`, `logger`, `message`, `request_id` when bound, `extra=` fields,
`exc_info` as a formatted traceback) except `level` is now lowercase
(structlog convention), which the Promtail/Loki pipeline also expects.
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
    """Wrap one turn/job's processing (agent_worker.py::process_request,
    ingest_worker.py::process_job, app/api/main.py's in-process endpoints —
    `thread_id` doubles as the id for non-queued turns). Every log line
    emitted anywhere during the wrapped block — including deep inside
    graph.py/tools.py, which have no idea this id exists — carries it, via
    `_add_request_id` below. Reset on exit so a worker's next turn on the
    same asyncio task doesn't inherit a stale id.
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
    every event, unless a call site already supplied one via
    `extra={"request_id": ...}`. Must run AFTER
    `structlog.stdlib.ExtraAdder` so an explicit value is already in
    `event_dict` by the time this checks it."""
    request_id = request_id_var.get()
    if request_id is not None and "request_id" not in event_dict:
        event_dict["request_id"] = request_id
    return event_dict


def _rename_event_to_message(
    logger: WrappedLogger, method_name: str, event_dict: EventDict
) -> EventDict:
    """structlog's own convention is an `event` key; this app's log
    consumers expect `message`, matching the previous formatter."""
    event_dict["message"] = event_dict.pop("event")
    return event_dict


def _format_exc_info(
    logger: WrappedLogger, method_name: str, event_dict: EventDict
) -> EventDict:
    """Renders the exc_info 3-tuple ProcessorFormatter attaches (from
    `logger.exception(...)`/`exc_info=True`) into a formatted traceback
    string under the same `exc_info` key — never the raw, non-JSON-
    serializable tuple."""
    exc_info = event_dict.pop("exc_info", None)
    if exc_info:
        if exc_info is True:
            exc_info = sys.exc_info()
        event_dict["exc_info"] = "".join(traceback.format_exception(*exc_info))
    return event_dict


# Shared by real log records (via ProcessorFormatter's foreign_pre_chain)
# and any native structlog call (none today, but wrap_for_formatter still
# needs this chain configured via structlog.configure() below).
_SHARED_PROCESSORS: list[Processor] = [
    structlog.stdlib.add_logger_name,
    structlog.stdlib.add_log_level,
    structlog.processors.TimeStamper(fmt="iso", key="timestamp"),
    structlog.stdlib.ExtraAdder(),
    _add_request_id,
    _rename_event_to_message,
]


def build_formatter() -> structlog.stdlib.ProcessorFormatter:
    """Exposed separately from `configure_logging` so tests can attach it to
    an isolated, non-propagating logger instead of the real root logger."""
    return structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=_SHARED_PROCESSORS,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            _format_exc_info,
            # default=str: an extra field is sometimes non-JSON-native (Enum,
            # UUID, ...) — stringify rather than crash the whole log line.
            structlog.processors.JSONRenderer(default=str),
        ],
    )


class _DynamicStreamHandler(logging.StreamHandler):
    """`logging.StreamHandler()` resolves `sys.stderr` ONCE at construction
    and holds that reference — breaks if stderr is later swapped/closed
    (surfaced by pytest's per-test capture: a finalizer logging during
    shutdown raised "I/O operation on closed file" against an already-closed
    capture stream). Re-resolving `sys.stderr` on every emit avoids this —
    also relevant to a production log-rotation setup that reopens fds.
    """

    def emit(self, record: logging.LogRecord) -> None:
        self.stream = sys.stderr
        super().emit(record)


def configure_logging(level: int = logging.INFO) -> None:
    """Call once, at process startup, before anything logs. `force=True`
    replaces any handler a prior import already attached to the root logger
    (e.g. a third-party `basicConfig` call) so this app's JSON formatting
    wins regardless of import order. Freely re-callable (unlike
    telemetry.py::configure_telemetry) — each call just replaces the root
    handlers again.
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
