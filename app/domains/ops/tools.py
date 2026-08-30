"""Tools for the internal ops-bot domain (app/domains/ops/domain.py):
`fetch_metrics_summary` pulls this app's own operational metrics
(app/domains/ops/metrics_client.py) and flags anything past its
alert-matching threshold; `post_to_team_channel` is this repo's first REAL
use of the `outward` tool capability (app/agent/tools.py::TOOL_CAPABILITIES
has always documented it as a possible value — every existing tool is
either read_only or mutating).

Ctx is still required here (fail-closed, same as every other tool in this
app) even though the underlying data isn't tenant-scoped — there's no
per-tenant metrics dashboard, this is global operational data about the
app itself. `ctx` here proves "a legitimate caller of this deployment,"
not a filter over rows a caller isn't supposed to see; see
app/domains/policy.py's own docstring for why `ActionAllowlistPolicy` is
the right (if slightly informational, for this one domain) fit anyway —
consistency with the rest of this app's tools matters more than skipping
a check that happens to have nothing to scope here.
"""
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from pydantic import BaseModel, Field, field_validator

from app.agent.tools import _run_with_timeout
from app.core.security import SecurityCtx, valid_ctx
from app.domains import notify
from app.domains.ops import metrics_client
from app.domains.policy import ActionAllowlistPolicy

_NO_CTX_REFUSAL = (
    "Refused: no valid tenant/principal context for this request. "
    "This isn't something you can work around — it means the request "
    "never got a security context stamped on it upstream."
)

OPS_POLICY = ActionAllowlistPolicy(frozenset({"fetch_metrics", "post_to_channel"}))


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


TOOLS = [fetch_metrics_summary, post_to_team_channel]

TOOL_CAPABILITIES = {
    "fetch_metrics_summary": "read_only",
    "post_to_team_channel": "outward",
}
