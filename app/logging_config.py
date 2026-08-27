"""Structured (JSON) logging, shared by every long-running service process
(app/api.py, app/agent_worker.py, app/ingest_worker.py,
app/telegram_channel.py) — NOT the interactive CLI tools (app/chat.py,
app/hitl_demo.py) or one-shot operator scripts (app/ingest.py, app/eval.py),
which print directly to the terminal for a human watching it and would just
get JSON noise interleaved with that.

This doesn't change any log CALL site. Every module in this app already
logs through the stdlib `logging` module, many with a per-call
`extra={...}` dict carrying real correlation data (request_id, tool name,
error_class, tenant — see e.g. app/agent_worker.py::process_request,
app/metrics.py::MetricsCallbackHandler). Two problems, both fixed by
wiring THIS UP, neither by changing what's logged:

1. `logging.basicConfig(level=logging.INFO)` (what every service process
   used before this module existed) renders only the plain message string
   with the default formatter — every `extra` field is silently dropped,
   not just unstructured. `configure_logging()` below is the same level,
   with a formatter that actually surfaces those fields as real JSON keys.
2. app/api.py had no logging setup AT ALL — no handler anywhere in the
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
app/agent_worker.py::process_request's `except` clause) — every OTHER log
line touched while processing that same turn (deep inside app/graph.py,
app/tools.py, app/metrics.py's tool-call audit lines, ...) carried no
correlation id at all, making "show me every log line for turn X" not
actually answerable from the logs alone. `bind_request_id`/`request_id_var`
below fix this the standard way: a contextvar set once at the entry point
(propagates through every `await` in that same task automatically) plus a
logging.Filter that stamps it onto every record — with ZERO changes to any
individual `logger.info(...)`/`logger.warning(...)` call site.
"""
import json
import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

# Every attribute a bare LogRecord carries by default — anything else on a
# given record was added via that call's own `extra={...}`, and is what
# actually gets serialized into the JSON line below.
_STANDARD_ATTRS = frozenset(vars(logging.makeLogRecord({})).keys())


class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in vars(record).items():
            if key not in _STANDARD_ATTRS and key not in payload:
                payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        # default=str: an extra field is sometimes a non-JSON-native value
        # (an Enum, a UUID, ...) at real call sites in this app — stringify
        # rather than let one such field crash the whole log line.
        return json.dumps(payload, default=str)


request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)


@contextmanager
def bind_request_id(request_id: str) -> Iterator[None]:
    """Wrap one turn/job's processing (app/agent_worker.py::process_request,
    app/ingest_worker.py::process_job, app/api.py's in-process endpoints —
    `thread_id` doubles as the correlation id on those, there being no
    separate request_id concept for a non-queued turn). Every log line
    emitted anywhere during the wrapped block — including deep inside
    app/graph.py/app/tools.py, which have no idea this id exists — carries
    it, via `_RequestIdFilter` below. Reset on exit (not left dangling) so
    a worker's NEXT turn on the same asyncio task doesn't inherit a stale id
    if something logs outside any `bind_request_id` block.
    """
    token = request_id_var.set(request_id)
    try:
        yield
    finally:
        request_id_var.reset(token)


class _RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        request_id = request_id_var.get()
        if request_id is not None and not hasattr(record, "request_id"):
            record.request_id = request_id
        return True


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
    logger's configuration.
    """
    handler = _DynamicStreamHandler()
    handler.setFormatter(JSONFormatter())
    handler.addFilter(_RequestIdFilter())
    logging.basicConfig(level=level, handlers=[handler], force=True)
