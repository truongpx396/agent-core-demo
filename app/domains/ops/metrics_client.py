"""The `MetricsReader` adapter (`app/ports/ops.py`): pulls this app's own operational metrics from the Prometheus this repo already ships
(`docker-compose.observability.yml`, `make obs-up`) — not a new data source, and not a new dependency (`httpx` is already used elsewhere, e.g.
app/channels/telegram.py, app/ingestion/ingestor.py).

Which checks exist, and what makes a reading an anomaly, is the domain's (`metric_checks.py`).
"""
import asyncio

import httpx

from app.core.config import PROMETHEUS_URL
from app.domains.ops.metric_checks import CHECKS

_QUERY_TIMEOUT_SECONDS = 10


async def _query_one(expr: str) -> float | None:
    """A single Prometheus instant query. Returns None on any failure
    (stack not running, malformed response, empty result vector) rather
    than raising — the caller degrades that reading to "unknown" instead
    of failing the whole digest over one metric."""
    try:
        async with httpx.AsyncClient(timeout=_QUERY_TIMEOUT_SECONDS) as client:
            resp = await client.get(
                f"{PROMETHEUS_URL}/api/v1/query", params={"query": expr}
            )
        resp.raise_for_status()
        result = resp.json()["data"]["result"]
        if not result:
            return None
        return float(result[0]["value"][1])
    except Exception:  # noqa: BLE001 - degrade to "unknown," never fail the digest
        return None


async def fetch_readings() -> dict[str, float | None]:
    """{check_name: value | None} for every check in CHECKS. One HTTP call
    per check (no batch-query endpoint), run concurrently via
    asyncio.gather rather than sequentially."""
    values = await asyncio.gather(*(_query_one(check.expr) for check in CHECKS))
    return dict(zip((check.name for check in CHECKS), values, strict=True))


class PrometheusMetricsReader:
    """The adapter: `fetch_readings` behind the port, looked up at CALL time so a test that patches the module function is seen."""

    async def fetch_readings(self) -> dict[str, float | None]:
        return await fetch_readings()
