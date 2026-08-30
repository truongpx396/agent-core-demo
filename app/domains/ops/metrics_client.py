"""Pulls this app's own operational metrics from the Prometheus this repo
already ships (`docker-compose.observability.yml`, `make obs-up`) — not a
new data source, and not a new dependency (`httpx` is already used
elsewhere, e.g. app/channels/telegram.py, app/ingestion/ingestor.py).

`READINGS` reuses the EXACT PromQL expressions and thresholds already
alerted on in `observability/prometheus/alerts.yml` — turn error rate, p95
latency, tool error rate, moderation-block rate, rate-limit rejections,
retrieval degradation, checkpoint issues — rather than inventing a
second, parallel definition of "something's wrong" that could quietly
drift out of sync with what actually pages someone. `detect_anomalies` is
deliberately a plain threshold comparison, not a learned/statistical
model: "anomaly" here literally means "one of this app's own alert rules
would be firing right now."
"""
from dataclasses import dataclass

import httpx

from app.core.config import PROMETHEUS_URL

_QUERY_TIMEOUT_SECONDS = 10


@dataclass(frozen=True)
class MetricCheck:
    name: str
    description: str
    expr: str
    threshold: float
    # Every check below is a "greater than" ceiling (mirrors
    # observability/prometheus/alerts.yml's own `> value` alert
    # expressions) — there's no "less than" style check in this app's
    # alert set to mirror, so this stays a fixed direction rather than a
    # per-check field with only one value ever used.


# Mirrors observability/prometheus/alerts.yml's `agent-core-slo` group,
# name-for-name and threshold-for-threshold — see that file for the
# reasoning behind each one. Deliberately NOT every alert in that file:
# ScrapeTargetDown (infra group) and the tenant-budget/semantic-cache
# checks are omitted here as noise for a daily human-readable digest, not
# because they're unimportant — a real deployment wanting the full set
# would just extend this list.
CHECKS: tuple[MetricCheck, ...] = (
    MetricCheck(
        name="turn_error_rate",
        description="Fraction of agent turns ending in outcome=\"error\" (5m)",
        expr=(
            'sum(rate(agent_requests_total{outcome="error"}[5m])) '
            "/ sum(rate(agent_requests_total[5m]))"
        ),
        threshold=0.05,
    ),
    MetricCheck(
        name="p95_latency_seconds",
        description="p95 end-to-end turn latency (5m)",
        expr="histogram_quantile(0.95, sum(rate(agent_latency_seconds_bucket[5m])) by (le))",
        threshold=30,
    ),
    MetricCheck(
        name="tool_error_rate",
        description="Fraction of tool calls that errored (10m)",
        expr="sum(rate(agent_tool_errors_total[5m])) / sum(rate(agent_tool_calls_total[5m]))",
        threshold=0.1,
    ),
    MetricCheck(
        name="moderation_block_rate",
        description="Blocked (moderation) inputs per second (5m)",
        expr='sum(rate(agent_moderation_total{outcome=~"blocked_.*"}[5m]))',
        threshold=0.5,
    ),
    MetricCheck(
        name="rate_limit_rejections_per_sec",
        description="Per-tenant rate-limit rejections per second (5m)",
        expr="sum(rate(agent_rate_limit_exceeded_total[5m]))",
        threshold=1,
    ),
    MetricCheck(
        name="retrieval_degraded_rate",
        description="Hybrid retrieval degradation events per second (15m)",
        expr="sum(rate(agent_retrieval_degraded_total[15m]))",
        threshold=0,
    ),
    MetricCheck(
        name="checkpoint_issues_15m",
        description="Refused/lost checkpoint resumes in the last 15m",
        expr="increase(agent_checkpoint_issue_total[15m])",
        threshold=0,
    ),
)


def _query_one(expr: str) -> float | None:
    """A single Prometheus instant query. Returns None on any failure
    (Prometheus/otel stack not running, a malformed response, an empty
    result vector because that metric has never fired) rather than
    raising — the caller degrades that one reading to "unknown," never
    fails the whole digest over one metric (same posture
    app/retrieval/semantic_cache.py/app/agent/moderation.py already take
    on their own optional dependencies)."""
    try:
        resp = httpx.get(
            f"{PROMETHEUS_URL}/api/v1/query",
            params={"query": expr},
            timeout=_QUERY_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        result = resp.json()["data"]["result"]
        if not result:
            return None
        return float(result[0]["value"][1])
    except Exception:  # noqa: BLE001 - degrade to "unknown," never fail the digest
        return None


def fetch_readings() -> dict[str, float | None]:
    """{check_name: value | None} for every check in CHECKS — one HTTP
    call per check (Prometheus has no documented batch-query endpoint;
    this is a handful of calls, run once a day or on demand, not a hot
    path)."""
    return {check.name: _query_one(check.expr) for check in CHECKS}


def detect_anomalies(readings: dict[str, float | None]) -> list[str]:
    """Which readings exceed their CHECKS threshold — a plain, pure
    function (no I/O), unit-tested directly against synthetic readings. A
    None reading (metric unavailable) is never flagged as an anomaly —
    "we don't know" is not the same claim as "something's wrong."
    """
    by_name = {check.name: check for check in CHECKS}
    anomalies = []
    for name, value in readings.items():
        check = by_name.get(name)
        if check is None or value is None:
            continue
        if value > check.threshold:
            anomalies.append(
                f"{check.description}: {value:.3g} (threshold {check.threshold:g})"
            )
    return anomalies


def format_readings(readings: dict[str, float | None]) -> str:
    """Human-readable line per check, for feeding to the digest's
    summarization prompt (scripts/ops_digest.py) or an ad-hoc
    investigation's fetch_metrics_summary tool result."""
    by_name = {check.name: check for check in CHECKS}
    lines = []
    for name, value in readings.items():
        check = by_name.get(name)
        label = check.description if check else name
        value_str = f"{value:.3g}" if value is not None else "unknown (query failed or no data)"
        lines.append(f"- {label}: {value_str}")
    return "\n".join(lines)
