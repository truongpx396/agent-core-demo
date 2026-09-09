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
from app.domains import notify, sandbox_session
from app.domains.ops import metrics_client, store
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
            "run_python_in_sandbox",
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
    their side before opening one of your own with log_incident. A result
    coming back at all means the URL WAS valid and the fetch succeeded —
    if the page's content doesn't look like a status page (no incident
    history, no uptime numbers), say that plainly rather than telling the
    user the URL itself was invalid; that's a different, false claim about
    a call that actually worked. For the
    full worked procedure (crawl their page, compute real stats from it
    in the sandbox, cross-reference our own incident history, decide
    whether to log a new incident), use_skill("vendor-incident-postmortem")
    has the whole thing — don't freehand it from scratch. Short version:
    pass this page's own text into run_command_in_sandbox to compute real
    stats from their own incident history (frequency, total downtime
    window); to check whether THIS SPECIFIC vendor has come up in OUR
    OWN incident log before, that's what the vendor-history-researcher
    subagent (run_subagent) is for — a different, more targeted question
    than metrics-researcher's own "what does current telemetry look
    like." Reaches the open internet — declared "outward" in
    TOOL_CAPABILITIES, so it always requires human approval before it
    runs, same as post_to_team_channel."""
    ctx = _ctx_or_refuse(config, "check_vendor_status")
    if ctx is None:
        return _NO_CTX_REFUSAL
    return _run_with_timeout(
        _check_vendor_status_page_impl, url, _timeout_seconds=CRAWL_TOOL_TIMEOUT_SECONDS
    )


def _thread_id_from_config(config: RunnableConfig | None) -> str:
    return (config or {}).get("configurable", {}).get("thread_id", "unknown")


# Three narrow, purpose-built tools over OpenSandbox's raw MCP catalog
# (app/domains/sandbox_session.py, GRAPH_PATTERNS.md pattern 50) —
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
# Always defined and always in TOOLS/TOOL_CAPABILITIES below — NOT gated
# on opensandbox-mcp's reachability at this module's own import time,
# unlike an earlier version of this code. That gate meant a process that
# finished booting before opensandbox-server finished starting cached an
# empty tool set FOREVER (see sandbox_session.load_raw_sandbox_tools's own
# docstring for the live trace that surfaced this) — no self-healing short
# of a full process restart, since a domain module only ever runs its
# top-level code once per process. These three tools now match
# fetch_external_reference's own established shape instead: always present
# in the graph's tool list, each impl below calls
# sandbox_session.load_raw_sandbox_tools() FRESH on every call (never a
# value captured once at import time) and raises a plain, catchable error
# if it's still empty — the same "a real call can fail, that's not the
# same as the tool not existing" contract every other outward tool in this
# app already has. app/domains/ops/domain.py needs no sandbox-specific
# code at all either way.


class RunCommandInSandboxArgs(BaseModel):
    command: str = Field(..., description="A shell command to run, e.g. a Python one-liner or script.")

    @field_validator("command")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("command must not be empty")
        return v


def _raw_sandbox_tools_or_raise() -> dict:
    raw = sandbox_session.load_raw_sandbox_tools()
    if not raw:
        raise sandbox_session.SandboxCallFailed(
            "OpenSandbox is not reachable right now (opensandbox-mcp/opensandbox-server may still be "
            "starting, or the sandbox profile isn't running) — try again in a moment."
        )
    return raw


def _run_command_in_sandbox_impl(command: str, thread_id: str, ctx: SecurityCtx) -> str:
    return sandbox_session.run_command_in_sandbox_impl(command, thread_id, _raw_sandbox_tools_or_raise())


@tool(args_schema=RunCommandInSandboxArgs)
def run_command_in_sandbox(command: str, config: RunnableConfig) -> str:
    """Run a shell command inside an isolated, disposable sandbox —
    use this for grep/diff/cat and other plain shell tasks (diffing two
    configs, reading a file with `cat <path>`). For actual PYTHON
    computation — recomputing a statistic, parsing a pasted log dump —
    use run_python_in_sandbox instead, not this tool: it takes your
    script as a plain parameter, with no shell quoting to get right and
    no separate write-then-run dance. Passing Python source here via
    `python -c '...'` is exactly the mistake run_python_in_sandbox
    exists to make impossible — any quote or apostrophe in your code
    (a dict literal, an f-string) collides with the shell's own
    quoting and silently breaks the script, confirmed via repeated
    real failures. For the full worked procedure that combines this
    with a live crawl and a subagent lookup, call
    use_skill("vendor-incident-postmortem") BEFORE writing anything —
    it walks through the whole investigation end to end, don't
    freehand it. One sandbox is created
    automatically per investigation and reused for every call in it —
    you never create, connect to, or track a sandbox yourself, just
    describe the command. Don't `pip install` anything — numpy and
    pandas are already available (Python standard library plus those
    two), so use them directly for anything beyond plain arithmetic.
    Reaches an external service — always requires human approval
    before it runs."""
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


class RunPythonInSandboxArgs(BaseModel):
    script: str = Field(..., description="Python source code to run, as plain text (not a shell command).")

    @field_validator("script")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("script must not be empty")
        return v


def _run_python_in_sandbox_impl(script: str, thread_id: str, ctx: SecurityCtx) -> str:
    return sandbox_session.run_python_in_sandbox_impl(script, thread_id, _raw_sandbox_tools_or_raise())


@tool(args_schema=RunPythonInSandboxArgs)
def run_python_in_sandbox(script: str, config: RunnableConfig) -> str:
    """Run real Python computation calculator's plain arithmetic can't
    do (recomputing a statistic from raw readings, parsing a pasted log
    dump, diffing two configs) — use this, not run_command_in_sandbox,
    for anything beyond a single shell command. Pass your FULL Python
    source as `script`, exactly as you'd write it in a file — multi-line
    code, quotes, apostrophes, f-strings, dict literals, all fine, none
    of it goes through a shell. Embed already-fetched content directly
    in your script — e.g. paste check_vendor_status_page's own returned
    text into a small parsing script here to compute real stats from it
    (incident count, total downtime) rather than reading it by eye; the
    sandbox itself has no network access, so it can only work with what
    you hand it. For the full worked procedure that combines this with
    a live crawl and a subagent lookup, call
    use_skill("vendor-incident-postmortem") BEFORE writing anything —
    it walks through the whole investigation end to end, don't freehand
    it. One sandbox is created automatically per investigation and
    reused for every call in it (shared with run_command_in_sandbox) —
    you never create, connect to, or track a sandbox yourself, or write
    the script to a file first. Don't `pip install` anything — numpy
    and pandas are already available, so use them directly for anything
    beyond plain arithmetic. Reaches an external service — always
    requires human approval before it runs."""
    ctx = _ctx_or_refuse(config, "run_python_in_sandbox")
    if ctx is None:
        return _NO_CTX_REFUSAL
    return _run_with_timeout(
        _run_python_in_sandbox_impl,
        script,
        _thread_id_from_config(config),
        ctx,
        _timeout_seconds=sandbox_session.SANDBOX_CALL_TIMEOUT_SECONDS,
    )


class ReadSandboxFileArgs(BaseModel):
    path: str = Field(..., description="Path of the file to read inside the sandbox.")


def _read_sandbox_file_impl(path: str, thread_id: str, ctx: SecurityCtx) -> str:
    return sandbox_session.read_sandbox_file_impl(path, thread_id, _raw_sandbox_tools_or_raise())


@tool(args_schema=ReadSandboxFileArgs)
def read_sandbox_file(path: str, config: RunnableConfig) -> str:
    """Read a text file from this investigation's sandbox (e.g. a
    script's output written to disk, or a file written earlier with
    write_sandbox_file). Same auto-created, per-investigation sandbox
    as run_command_in_sandbox. If this returns an unexpected "file not
    found" for a file you know exists, use run_command_in_sandbox with
    `cat <path>` instead — a disclosed, environment-specific gap in
    this particular tool, not in write_sandbox_file or
    run_command_in_sandbox. Reaches an external service — always
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
    return sandbox_session.write_sandbox_file_impl(path, content, thread_id, _raw_sandbox_tools_or_raise())


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


_SANDBOX_TOOLS = [run_command_in_sandbox, run_python_in_sandbox, read_sandbox_file, write_sandbox_file]


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
