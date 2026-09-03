"""Tools for the internal ops-bot domain (app/domains/ops/domain.py):
`fetch_metrics_summary` pulls this app's own operational metrics
(app/domains/ops/metrics_client.py) and flags anything past its
alert-matching threshold; `post_to_team_channel` is this repo's first REAL
use of the `outward` tool capability (app/agent/tools.py::TOOL_CAPABILITIES
has always documented it as a possible value — every existing tool is
either read_only or mutating); `log_incident`/`list_recent_incidents`/
`resolve_incident` give an investigation a durable place to record what it
found (app/domains/ops/store.py, `ops_incidents`) rather than only ever a
one-off channel post that scrolls away.

Ctx is still required here (fail-closed, same as every other tool in this
app) even though the underlying data isn't tenant-scoped — there's no
per-tenant metrics dashboard, this is global operational data about the
app itself. `ctx` here proves "a legitimate caller of this deployment,"
not a filter over rows a caller isn't supposed to see; see
app/domains/policy.py's own docstring for why `ActionAllowlistPolicy` is
the right (if slightly informational, for this one domain) fit anyway —
consistency with the rest of this app's tools matters more than skipping
a check that happens to have nothing to scope here. `log_incident`/
`resolve_incident` stamp `opened_by`/attribution from `ctx["principal"]`
even though the incident row itself carries no tenant column — see
app/domains/ops/store.py's own docstring.
"""
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from pydantic import BaseModel, Field, field_validator

from app.agent.tools import _run_with_timeout
from app.core.security import SecurityCtx, valid_ctx
from app.domains import notify
from app.domains.ops import metrics_client, store
from app.domains.policy import ActionAllowlistPolicy

_NO_CTX_REFUSAL = (
    "Refused: no valid tenant/principal context for this request. "
    "This isn't something you can work around — it means the request "
    "never got a security context stamped on it upstream."
)

OPS_POLICY = ActionAllowlistPolicy(
    frozenset(
        {
            "fetch_metrics",
            "post_to_channel",
            "log_incident",
            "list_recent_incidents",
            "resolve_incident",
        }
    )
)


def _ctx_from_config(config: RunnableConfig | None) -> SecurityCtx | None:
    if not config:
        return None
    return config.get("configurable", {}).get("ctx")


def _ctx_or_refuse(config: RunnableConfig | None, action: str) -> SecurityCtx | None:
    ctx = _ctx_from_config(config)
    if not valid_ctx(ctx) or not OPS_POLICY.permit(action, ctx):
        return None
    return ctx


class FetchMetricsSummaryArgs(BaseModel):
    pass


def _fetch_metrics_summary_impl() -> str:
    readings = metrics_client.fetch_readings()
    anomalies = metrics_client.detect_anomalies(readings)
    lines = [metrics_client.format_readings(readings)]
    if anomalies:
        lines.append("\nAnomalies (past an alert-matching threshold):")
        lines.extend(f"- {a}" for a in anomalies)
    else:
        lines.append("\nNo anomalies — every metric is within its normal range.")
    return "\n".join(lines)


@tool(args_schema=FetchMetricsSummaryArgs)
def fetch_metrics_summary(config: RunnableConfig) -> str:
    """Fetch this app's current operational metrics (turn error rate,
    latency, tool error rate, moderation blocks, rate limiting, retrieval
    degradation, checkpoint issues) and flag anything past its
    alert-matching threshold. Read-only — pulls from Prometheus, changes
    nothing."""
    ctx = _ctx_or_refuse(config, "fetch_metrics")
    if ctx is None:
        return _NO_CTX_REFUSAL
    return _run_with_timeout(_fetch_metrics_summary_impl)


class PostToTeamChannelArgs(BaseModel):
    channel: str = Field(..., description="Which team channel, e.g. 'ops-digest' or 'ops-alerts'.")
    message: str = Field(..., description="The message to post.")

    @field_validator("channel", "message")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must not be empty")
        return v


def _post_to_team_channel_impl(channel: str, message: str) -> str:
    return notify.post_to_team_channel(channel, message)


@tool(args_schema=PostToTeamChannelArgs)
def post_to_team_channel(channel: str, message: str, config: RunnableConfig) -> str:
    """Post a message to a team channel — e.g. a metrics digest or an
    anomaly you found during an investigation. This reaches OUTSIDE this
    app's own corpus (a real deployment would post to Slack), unlike the
    read-only fetch_metrics_summary — declared "outward" in
    TOOL_CAPABILITIES, so it is always gated behind human_approval before
    it runs, same as any mutating tool."""
    ctx = _ctx_or_refuse(config, "post_to_channel")
    if ctx is None:
        return _NO_CTX_REFUSAL
    return _run_with_timeout(_post_to_team_channel_impl, channel, message)


class LogIncidentArgs(BaseModel):
    summary: str = Field(..., description="Short summary of what's wrong.")
    detail: str | None = Field(
        default=None, description="Optional: the specific numbers/evidence behind this incident."
    )

    @field_validator("summary")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("summary must not be empty")
        return v


def _log_incident_impl(summary: str, detail: str | None, ctx: SecurityCtx) -> str:
    incident_id = store.log_incident(ctx["principal"], summary, detail)
    return f"Incident #{incident_id} logged: {summary}"


@tool(args_schema=LogIncidentArgs)
def log_incident(summary: str, config: RunnableConfig, detail: str | None = None) -> str:
    """Record a real anomaly found during an investigation as a durable
    incident — use this once you've confirmed something is actually wrong
    (past its alert-matching threshold), not for every routine check."""
    ctx = _ctx_or_refuse(config, "log_incident")
    if ctx is None:
        return _NO_CTX_REFUSAL
    return _run_with_timeout(_log_incident_impl, summary, detail, ctx)


class ListRecentIncidentsArgs(BaseModel):
    status: str | None = Field(
        default=None, description="Optional filter: 'open' or 'resolved'. Omit for both."
    )


def _list_recent_incidents_impl(status: str | None) -> str:
    incidents = store.list_recent_incidents(status=status)
    if not incidents:
        return "No incidents on record."
    lines = [
        f"- #{i['id']} [{i['status']}] {i['summary']} (opened by {i['opened_by']}, {i['created_at']})"
        for i in incidents
    ]
    return "\n".join(lines)


@tool(args_schema=ListRecentIncidentsArgs)
def list_recent_incidents(config: RunnableConfig, status: str | None = None) -> str:
    """List recently logged incidents, most recent first — use this to
    check whether something happening now has happened before. Read-only —
    changes nothing."""
    ctx = _ctx_or_refuse(config, "list_recent_incidents")
    if ctx is None:
        return _NO_CTX_REFUSAL
    return _run_with_timeout(_list_recent_incidents_impl, status)


class ResolveIncidentArgs(BaseModel):
    incident_id: int = Field(..., description="The incident number to resolve.")
    resolution: str = Field(..., description="What fixed it, or why it's no longer a concern.")

    @field_validator("resolution")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("resolution must not be empty")
        return v


def _resolve_incident_impl(incident_id: int, resolution: str, ctx: SecurityCtx) -> str:
    updated = store.resolve_incident(incident_id, resolution)
    if not updated:
        return f"No incident #{incident_id} found to resolve."
    return f"Incident #{incident_id} resolved: {resolution}"


@tool(args_schema=ResolveIncidentArgs)
def resolve_incident(incident_id: int, resolution: str, config: RunnableConfig) -> str:
    """Mark a previously logged incident resolved, with what fixed it or
    why it's no longer a concern."""
    ctx = _ctx_or_refuse(config, "resolve_incident")
    if ctx is None:
        return _NO_CTX_REFUSAL
    return _run_with_timeout(_resolve_incident_impl, incident_id, resolution, ctx)


TOOLS = [
    fetch_metrics_summary,
    post_to_team_channel,
    log_incident,
    list_recent_incidents,
    resolve_incident,
]

TOOL_CAPABILITIES = {
    "fetch_metrics_summary": "read_only",
    "post_to_team_channel": "outward",
    "log_incident": "mutating",
    "list_recent_incidents": "read_only",
    "resolve_incident": "mutating",
}
