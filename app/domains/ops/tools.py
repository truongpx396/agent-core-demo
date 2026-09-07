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
from app.domains.ops import metrics_client, sandbox_session, store
from app.domains.policy import ActionAllowlistPolicy
from app.ingestion.web_crawler import CRAWL_TOOL_TIMEOUT_SECONDS, render_url_to_markdown

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
            "check_vendor_status",
            "run_command_in_sandbox",
            "read_sandbox_file",
            "write_sandbox_file",
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


class CheckVendorStatusPageArgs(BaseModel):
    url: str = Field(..., description="A vendor/dependency's public status page (https:// only).")


def _check_vendor_status_page_impl(url: str) -> str:
    return render_url_to_markdown(url)


@tool(args_schema=CheckVendorStatusPageArgs)
def check_vendor_status_page(url: str, config: RunnableConfig) -> str:
    """Read a vendor/upstream-dependency's public status page LIVE (real
    headless-browser render) — use this to check whether an anomaly you
    found via fetch_metrics_summary correlates with a known incident on
    their side before opening one of your own with log_incident. Reaches
    the open internet — declared "outward" in TOOL_CAPABILITIES, so it
    always requires human approval before it runs, same as
    post_to_team_channel."""
    ctx = _ctx_or_refuse(config, "check_vendor_status")
    if ctx is None:
        return _NO_CTX_REFUSAL
    return _run_with_timeout(
        _check_vendor_status_page_impl, url, _timeout_seconds=CRAWL_TOOL_TIMEOUT_SECONDS
    )


def _thread_id_from_config(config: RunnableConfig | None) -> str:
    return (config or {}).get("configurable", {}).get("thread_id", "unknown")


# Three narrow, purpose-built tools over OpenSandbox's raw MCP catalog
# (app/domains/ops/sandbox_session.py, GRAPH_PATTERNS.md pattern 50) —
# NOT the raw ~19-tool catalog itself. Real, live-verified finding behind
# this: handing a small local model (qwen2.5:3b) OpenSandbox's own
# stateful create/connect/run tools directly produced reproducible
# failures (a hallucinated sandbox_id, then reaching for sandbox_connect
# instead of sandbox_create after an error) that a prompt-only fix did not
# resolve — see sandbox_session.py's own docstring for the full writeup.
# These three give the model the exact same flat, 1-3-field shape every
# other tool in this app already has; the sandbox's entire lifecycle
# (create-or-reuse per thread, connect_if_missing) is handled in
# sandbox_session.py, never exposed to the model.
#
# Built conditionally: `_RAW_SANDBOX_TOOLS` is empty if opensandbox-mcp
# isn't installed/reachable (sandbox_session.load_raw_sandbox_tools's own
# degrade — see app/domains/sandbox_tools.py's docstring for why that's
# safe to check eagerly, at this module's own import time) — the same
# "resolves to nothing usable, not a crash" contract
# app/agent/tools.py::make_domain_subagent_tool already established for an
# AGENT.md with no real tools to offer. `_SANDBOX_TOOLS`/entries below are
# ordinary members of TOOLS/TOOL_CAPABILITIES, not a special merge step —
# app/domains/ops/domain.py needs no sandbox-specific code at all anymore.
_RAW_SANDBOX_TOOLS = sandbox_session.load_raw_sandbox_tools()
_SANDBOX_TOOLS = []

if _RAW_SANDBOX_TOOLS:

    class RunCommandInSandboxArgs(BaseModel):
        command: str = Field(..., description="A shell command to run, e.g. a Python one-liner or script.")

        @field_validator("command")
        @classmethod
        def _not_blank(cls, v: str) -> str:
            if not v.strip():
                raise ValueError("command must not be empty")
            return v

    def _run_command_in_sandbox_impl(command: str, thread_id: str, ctx: SecurityCtx) -> str:
        return sandbox_session.run_command_in_sandbox_impl(command, thread_id, _RAW_SANDBOX_TOOLS)

    @tool(args_schema=RunCommandInSandboxArgs)
    def run_command_in_sandbox(command: str, config: RunnableConfig) -> str:
        """Run a shell command inside an isolated, disposable sandbox —
        use this for real computation calculator's plain arithmetic can't
        do (recomputing a statistic from raw readings, parsing a pasted
        log dump, diffing two configs). One sandbox is created
        automatically per investigation and reused for every call in it —
        you never create, connect to, or track a sandbox yourself, just
        describe the command. The sandbox has NO network access by
        default, so stick to the Python standard library rather than
        `pip install`-ing anything. Reaches an external service — always
        requires human approval before it runs."""
        ctx = _ctx_or_refuse(config, "run_command_in_sandbox")
        if ctx is None:
            return _NO_CTX_REFUSAL
        return _run_with_timeout(
            _run_command_in_sandbox_impl,
            command,
            _thread_id_from_config(config),
            ctx,
            _timeout_seconds=sandbox_session.SANDBOX_CALL_TIMEOUT_SECONDS,
        )

    class ReadSandboxFileArgs(BaseModel):
        path: str = Field(..., description="Path of the file to read inside the sandbox.")

    def _read_sandbox_file_impl(path: str, thread_id: str, ctx: SecurityCtx) -> str:
        return sandbox_session.read_sandbox_file_impl(path, thread_id, _RAW_SANDBOX_TOOLS)

    @tool(args_schema=ReadSandboxFileArgs)
    def read_sandbox_file(path: str, config: RunnableConfig) -> str:
        """Read a text file from this investigation's sandbox (e.g. a
        script's output written to disk, or a file written earlier with
        write_sandbox_file). Same auto-created, per-investigation sandbox
        as run_command_in_sandbox. Reaches an external service — always
        requires human approval before it runs."""
        ctx = _ctx_or_refuse(config, "read_sandbox_file")
        if ctx is None:
            return _NO_CTX_REFUSAL
        return _run_with_timeout(
            _read_sandbox_file_impl,
            path,
            _thread_id_from_config(config),
            ctx,
            _timeout_seconds=sandbox_session.SANDBOX_CALL_TIMEOUT_SECONDS,
        )

    class WriteSandboxFileArgs(BaseModel):
        path: str = Field(..., description="Destination path for the file inside the sandbox.")
        content: str = Field(..., description="The file's full text content.")

    def _write_sandbox_file_impl(path: str, content: str, thread_id: str, ctx: SecurityCtx) -> str:
        return sandbox_session.write_sandbox_file_impl(path, content, thread_id, _RAW_SANDBOX_TOOLS)

    @tool(args_schema=WriteSandboxFileArgs)
    def write_sandbox_file(path: str, content: str, config: RunnableConfig) -> str:
        """Write a text file into this investigation's sandbox (e.g. stage
        a script before running it with run_command_in_sandbox, or a log
        dump/config to diff). Same auto-created, per-investigation sandbox
        as run_command_in_sandbox. Reaches an external service — always
        requires human approval before it runs."""
        ctx = _ctx_or_refuse(config, "write_sandbox_file")
        if ctx is None:
            return _NO_CTX_REFUSAL
        return _run_with_timeout(
            _write_sandbox_file_impl,
            path,
            content,
            _thread_id_from_config(config),
            ctx,
            _timeout_seconds=sandbox_session.SANDBOX_CALL_TIMEOUT_SECONDS,
        )

    _SANDBOX_TOOLS = [run_command_in_sandbox, read_sandbox_file, write_sandbox_file]


TOOLS = [
    fetch_metrics_summary,
    post_to_team_channel,
    log_incident,
    list_recent_incidents,
    resolve_incident,
    check_vendor_status_page,
    *_SANDBOX_TOOLS,
]

TOOL_CAPABILITIES = {
    "fetch_metrics_summary": "read_only",
    "post_to_team_channel": "outward",
    "log_incident": "mutating",
    "list_recent_incidents": "read_only",
    "resolve_incident": "mutating",
    "check_vendor_status_page": "outward",
    **{t.name: "outward" for t in _SANDBOX_TOOLS},
}
