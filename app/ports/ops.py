"""The ops copilot's data: incidents it logs, and the operational metrics it reads.

Incidents are the ops team's own record (not per-tenant data), written exactly-once at the row level by `tool_call_id`
(`app/domains/ops/store.py`). Metrics come from the Prometheus this repo ships (`app/domains/ops/metrics_client.py`); a reading that cannot be had is
`None` ("unknown"), never an exception, so one dead query does not fail a whole digest. The thresholds and what counts as an anomaly are the domain's
(`app/domains/ops/metric_checks.py`, the same expressions `observability/prometheus/alerts.yml` pages on).
"""
from __future__ import annotations

from typing import Protocol

from app.ports.slot import Slot


class IncidentStore(Protocol):
    async def log_incident(self, opened_by: str, summary: str, detail: str | None, tool_call_id: str | None = None) -> int: ...

    async def list_recent_incidents(self, limit: int = 10, status: str | None = None) -> list[dict]: ...

    async def resolve_incident(self, incident_id: int, resolution: str) -> bool:
        """True if the incident exists and was open."""
        ...


class MetricsReader(Protocol):
    async def fetch_readings(self) -> dict[str, float | None]:
        """`{check name: value}` for every check, run concurrently; `None` for a reading that failed or has no data."""
        ...


incident_store: Slot[IncidentStore] = Slot("incident_store", IncidentStore)
metrics_reader: Slot[MetricsReader] = Slot("metrics_reader", MetricsReader)
