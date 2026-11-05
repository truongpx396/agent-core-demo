"""The ops module's published use cases: what a driving adapter outside it calls (the published ops MCP server, the cron digest).

`tools.py` is the agent's way in (argument schemas, `ctx_or_refuse`, timeout); this is the same reads without the tool layer. Metrics come from the
`MetricsReader` port and the thresholds are the domain's (`metric_checks.py`); incidents come from the `IncidentStore` port (`ports.py`).
"""
from app.domains.ops import metric_checks
from app.domains.ops.metric_checks import format_readings
from app.domains.ops.ports import incident_store, metrics_reader
from app.domains.shared.policy import ActionAllowlistPolicy

__all__ = [
    "OPS_POLICY",
    "fetch_metrics_summary_impl",
    "format_readings",
    "list_recent_incidents_impl",
    "read_metrics",
]

OPS_POLICY = ActionAllowlistPolicy(
    frozenset(
        {
            "fetch_metrics",
            "post_to_channel",
            "log_incident",
            "list_recent_incidents",
            "resolve_incident",
            "check_vendor_status",
            "web_search",
            "run_command_in_sandbox",
            "run_python_in_sandbox",
            "read_sandbox_file",
            "write_sandbox_file",
        }
    )
)


async def read_metrics() -> tuple[dict[str, float | None], list[str]]:
    """Every check's current reading (`None` for one that failed or has no data) and the anomalies among them."""
    readings = await metrics_reader.get().fetch_readings()
    return readings, metric_checks.detect_anomalies(readings)


async def fetch_metrics_summary_impl() -> str:
    readings, anomalies = await read_metrics()
    lines = [metric_checks.format_readings(readings)]
    if anomalies:
        lines.append("\nAnomalies (past an alert-matching threshold):")
        lines.extend(f"- {a}" for a in anomalies)
    else:
        lines.append("\nNo anomalies — every metric is within its normal range.")
    return "\n".join(lines)


async def list_recent_incidents_impl(status: str | None) -> str:
    incidents = await incident_store.get().list_recent_incidents(status=status)
    if not incidents:
        return "No incidents on record."
    lines = [
        f"- #{i['id']} [{i['status']}] {i['summary']} (opened by {i['opened_by']}, {i['created_at']})"
        for i in incidents
    ]
    return "\n".join(lines)
