"""The process-wide Langfuse client.

Every turn used to build its own `Langfuse()` (`_open_trace`) and, at its end,
another one just to call `.flush()` on it. A client is not cheap: construction
starts three background threads (ingestion consumer(s) and media-upload
consumer) that nothing stops, keys set or not — measured on the installed SDK
(2.60.10) at +3 threads per client, so a worker gained six per turn (spec 008,
B18). The second client also did nothing useful: `Langfuse.flush()` joins THAT
instance's own queue, so flushing a fresh client flushed an empty one and never
the trace's events.

So there is one client, created on first use and shared by every trace in the
process. Its own background consumer sends events as they arrive; it is flushed
and stopped once, at process exit (`atexit`), which is the one place a blocking
flush is acceptable — `flush()` joins the queue, so calling it per turn from the
event loop would stall the loop for the length of a network round trip to a
Langfuse that may be down.

Tracing is optional. If the SDK is missing or the client cannot be built,
`get_langfuse()` returns `None` and the turn runs untraced; the failure is
remembered, so a broken tracing setup costs one attempt, not one per turn.
"""
import atexit
import logging
import threading
from typing import Any

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_client: Any = None
_settled = False  # True once creation was attempted (success or not) or the client was shut down


def get_langfuse() -> Any | None:
    """The shared client, created on first call. `None` when tracing is
    unavailable (SDK not importable, construction failed) or the client has
    already been shut down."""
    global _client, _settled
    if _settled:
        return _client
    with _lock:
        if _settled:
            return _client
        try:
            from langfuse import Langfuse

            _client = Langfuse()
            atexit.register(shutdown_langfuse)
        except Exception as exc:  # noqa: BLE001 - tracing is optional; a turn must not fail because of it
            logger.warning("tracing_client_unavailable", extra={"error_class": type(exc).__name__})
            _client = None
        _settled = True
        return _client


def shutdown_langfuse() -> None:
    """Flush what is queued and stop the client's threads. Idempotent, never
    raises, and leaves tracing off for the rest of the process — a trace opened
    after this would otherwise quietly start new threads during interpreter
    exit."""
    global _client, _settled
    with _lock:
        client, _client, _settled = _client, None, True
    if client is None:
        return
    try:
        client.flush()
        client.shutdown()
    except Exception as exc:  # noqa: BLE001 - best-effort at exit
        logger.warning("tracing_client_shutdown_failed", extra={"error_class": type(exc).__name__})


def _reset_for_tests() -> None:
    """Forget the client WITHOUT stopping it, so a test can start from 'never
    created'. Production never calls this."""
    global _client, _settled
    with _lock:
        _client, _settled = None, False
