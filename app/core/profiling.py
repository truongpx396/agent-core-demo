"""Continuous CPU profiling: pushes this process's Python stacks to a Pyroscope server (specs/022).

Call `start_profiling(service_name)` once at real process startup, right after `configure_telemetry` (the failure counter below needs the
meter provider it installs), in the three long-lived entrypoints: the API's `lifespan`, `agent_worker.py` and `ingest_worker.py`. Never at
import time, and never in a short-lived job: the SDK uploads every 10 s, so a job that lives seconds would upload nothing.

Off unless `PYROSCOPE_SERVER_ADDRESS` is set, and the SDK is imported only then, so a build without it (or a process that does not want it)
behaves exactly as before. It never raises: a profiler is diagnostics, and "the profiler could not start" must not become "the API could
not start". The sampler is a native thread of the SDK, and measured against a real server: `configure` returns in about 13 ms, an
unreachable server or a refused port costs no start-up or shutdown time, and a server that is not yet ready drops the push silently. So the
one failure this process can see, and counts, is that the profiler never started.

Tags are `domain` and `replica`, never a tenant, principal or thread: every distinct value is a new series in the profile store, and a
tenant id would put customer names in a UI engineers browse. The last seconds before a graceful stop (under the 10 s upload interval) are
not uploaded; there is no `stop_profiling()` on purpose, the loss is a flame graph's rounding error.
"""
import logging
import socket

from app.core.config import (
    AGENT_DOMAIN,
    PYROSCOPE_SAMPLE_RATE,
    PYROSCOPE_SERVER_ADDRESS,
)
from app.core.metrics import agent_profiling_start_failures_total

logger = logging.getLogger(__name__)

_started = False


def start_profiling(service_name: str) -> bool:
    """True when the profiler is running in this process. Idempotent; blank `PYROSCOPE_SERVER_ADDRESS` is a quiet False."""
    global _started
    if _started:
        return True
    if not PYROSCOPE_SERVER_ADDRESS:
        logger.info("profiling_disabled", extra={"service_name": service_name})
        return False
    try:
        import pyroscope
    except ImportError:
        agent_profiling_start_failures_total.labels(reason="sdk_missing").inc()
        logger.warning("profiling_sdk_missing", extra={"service_name": service_name})
        return False
    try:
        pyroscope.configure(
            application_name=service_name,
            server_address=PYROSCOPE_SERVER_ADDRESS,
            sample_rate=PYROSCOPE_SAMPLE_RATE,
            tags={"domain": AGENT_DOMAIN, "replica": socket.gethostname()},
        )
    except Exception as exc:  # noqa: BLE001 - diagnostics must not stop the process they observe
        agent_profiling_start_failures_total.labels(reason="configure_failed").inc()
        logger.warning("profiling_start_failed", extra={"service_name": service_name, "error_class": type(exc).__name__})
        return False
    _started = True
    logger.info("profiling_started", extra={"service_name": service_name, "server": PYROSCOPE_SERVER_ADDRESS, "sample_rate": PYROSCOPE_SAMPLE_RATE})
    return True
