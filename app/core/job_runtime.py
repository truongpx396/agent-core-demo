"""Process setup for a scheduled or one-shot job (`scripts/ops_digest.py`,
`followup_sweep.py`, `tool_call_dedup_sweep.py`, `ops_investigate.py`).

A long-lived process (the API, a worker) configures telemetry at startup and lets
the exporter's 15 s timer push as it goes. A job lives for seconds, so that timer
never fires: configuring telemetry alone exports nothing, and every counter the
job incremented dies with the process. The job must be flushed before it exits —
including when it fails, since a failure is exactly what an alert on those
counters is for (spec 008, B22: `TeamChannelNotifyFailing` could not fire for the
digest's own failed post).
"""
from collections.abc import Iterator
from contextlib import contextmanager

from app.core.logging_config import configure_logging
from app.core.telemetry import configure_telemetry, shutdown_telemetry


@contextmanager
def scheduled_job(service_name: str) -> Iterator[None]:
    """Logging and telemetry up for the job's body, telemetry flushed after it
    whether it returned or raised. `service_name` is the OTel `service.name`, so
    each job's series is distinguishable on the dashboards."""
    configure_logging()
    configure_telemetry(service_name)
    try:
        yield
    finally:
        shutdown_telemetry()
