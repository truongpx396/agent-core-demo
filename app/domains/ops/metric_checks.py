"""What the ops copilot counts as "something's wrong": the checks, their thresholds, and how a set of readings becomes anomalies and text.

`CHECKS` reuses the EXACT PromQL expressions and thresholds already alerted on in `observability/prometheus/alerts.yml` — turn error rate, p95 latency,
tool error rate, moderation-block rate, rate-limit rejections, retrieval degradation, checkpoint issues — rather than inventing a second, parallel
definition of "something's wrong" that could quietly drift out of sync with what actually pages someone. `detect_anomalies` is deliberately a plain
threshold comparison, not a learned/statistical model: "anomaly" here literally means "one of this app's own alert rules would be firing right now."

Pure: no I/O. Getting the readings is the `MetricsReader` port (`app/ports/ops.py`; the adapter is `metrics_client.py`).
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class MetricCheck:
    name: str
    description: str
    expr: str
    threshold: float
    # Always a "greater than" ceiling, mirroring alerts.yml's `> value`
    # expressions — no "less than" checks exist in this app's alert set.


# Mirrors observability/prometheus/alerts.yml's `agent-core-slo` group
# name-for-name/threshold-for-threshold (see that file for reasoning).
# Omits ScrapeTargetDown and tenant-budget/semantic-cache checks as noise
# for a daily digest, not because they're unimportant.
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
        description="Payment-webhook rate-limit rejections per second (5m)",
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
