"""Tools for the support-copilot domain (app/domains/support/domain.py) —
Tier-1 customer support: look things up in the knowledge base (reused
as-is from app/agent/tools.py, see domain.py), open/check/escalate a
support ticket, list a customer's own tickets, and add a follow-up comment
to one already open. Sandboxed by design: this domain's AgentManifest never
exposes calculator/add_note/remember/query_employees/run_subagent — see
domain.py's own docstring for why that's the literal meaning of
"sandboxed... knowledge base + ticket system" access.

Same conventions as app/agent/tools.py throughout: an explicit Pydantic
`args_schema`, `config: RunnableConfig` for ctx (auto-excluded from the
schema the LLM sees), `_run_with_timeout` (reused, not reimplemented) for
the shared timeout budget + output scrubbing, and a fail-closed ctx check
before touching Postgres.
"""
from enum import Enum

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from pydantic import BaseModel, Field, field_validator

from app.agent.tools import _run_with_timeout
from app.core.security import SecurityCtx, valid_ctx
from app.domains import notify, sandbox_session
from app.domains.policy import ActionAllowlistPolicy
from app.domains.support import store
from app.ingestion.web_crawler import CRAWL_TOOL_TIMEOUT_SECONDS, render_url_to_markdown

_NO_CTX_REFUSAL = (
    "Refused: no valid tenant/principal context for this request. "
    "This isn't something you can work around — it means the request "
    "never got a security context stamped on it upstream."
)

SUPPORT_POLICY = ActionAllowlistPolicy(
    frozenset(
        {
            "create_ticket",
            "check_ticket_status",
            "escalate_to_human",
            "list_my_tickets",
            "add_ticket_comment",
            "fetch_external_reference",
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
    if not valid_ctx(ctx) or not SUPPORT_POLICY.permit(action, ctx):
        return None
    return ctx


class TicketPriority(str, Enum):
    low = "low"
    normal = "normal"
    high = "high"
    urgent = "urgent"


class CreateTicketArgs(BaseModel):
    subject: str = Field(..., description="Short summary of the customer's issue.")
    description: str = Field(..., description="Full description, including anything the customer already told you.")
    priority: TicketPriority = Field(default=TicketPriority.normal)

    @field_validator("subject", "description")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must not be empty")
        return v


def _create_ticket_impl(
    subject: str, description: str, priority: TicketPriority, ctx: SecurityCtx
) -> str:
    ticket_id = store.create_ticket(
        tenant=ctx["tenant"],
        requester=ctx["principal"],
        subject=subject,
        description=description,
        priority=priority.value,
    )
    return f"Ticket #{ticket_id} opened ({priority.value} priority): {subject}"


@tool(args_schema=CreateTicketArgs)
def create_ticket(
    subject: str, description: str, config: RunnableConfig, priority: TicketPriority = TicketPriority.normal
) -> str:
    """Open a new Tier-1 support ticket for the current customer. Use this
    when the knowledge base doesn't resolve the issue and it needs to be
    tracked/followed up on."""
    ctx = _ctx_or_refuse(config, "create_ticket")
    if ctx is None:
        return _NO_CTX_REFUSAL
    return _run_with_timeout(_create_ticket_impl, subject, description, priority, ctx)


class CheckTicketStatusArgs(BaseModel):
    ticket_id: int = Field(..., description="The ticket number to look up.")


def _check_ticket_status_impl(ticket_id: int, ctx: SecurityCtx) -> str:
    ticket = store.get_ticket(ctx["tenant"], ticket_id)
    if ticket is None:
        return f"No ticket #{ticket_id} found."
    line = f"Ticket #{ticket['id']} — {ticket['status']} ({ticket['priority']} priority): {ticket['subject']}"
    if ticket.get("escalation_reason"):
        line += f"\nEscalated: {ticket['escalation_reason']}"
    if ticket.get("notes"):
        line += f"\nFollow-up notes:\n{ticket['notes']}"
    return line


@tool(args_schema=CheckTicketStatusArgs)
def check_ticket_status(ticket_id: int, config: RunnableConfig) -> str:
    """Look up an existing support ticket's current status by its number."""
    ctx = _ctx_or_refuse(config, "check_ticket_status")
    if ctx is None:
        return _NO_CTX_REFUSAL
    return _run_with_timeout(_check_ticket_status_impl, ticket_id, ctx)


class EscalateToHumanArgs(BaseModel):
    ticket_id: int = Field(..., description="The ticket number to escalate.")
    reason: str = Field(..., description="Why this is beyond Tier-1 scope — be specific.")

    @field_validator("reason")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("reason must not be empty")
        return v


def _escalate_to_human_impl(ticket_id: int, reason: str, ctx: SecurityCtx) -> str:
    updated = store.escalate_ticket(ctx["tenant"], ticket_id, reason)
    if not updated:
        return f"No ticket #{ticket_id} found to escalate."
    notify.post_to_team_channel(
        "support-escalations",
        f"[{ctx['tenant']}] Ticket #{ticket_id} escalated by {ctx['principal']}: {reason}",
    )
    return f"Ticket #{ticket_id} escalated to a human agent: {reason}"


@tool(args_schema=EscalateToHumanArgs)
def escalate_to_human(ticket_id: int, reason: str, config: RunnableConfig) -> str:
    """Hand an existing ticket off to a human agent — use this for anything
    outside Tier-1 scope (refunds, account changes, anything the knowledge
    base doesn't cover, or a customer explicitly asking for a person).
    This is the sandbox boundary: you cannot resolve these yourself, only
    flag them."""
    ctx = _ctx_or_refuse(config, "escalate_to_human")
    if ctx is None:
        return _NO_CTX_REFUSAL
    return _run_with_timeout(_escalate_to_human_impl, ticket_id, reason, ctx)


class ListMyTicketsArgs(BaseModel):
    pass


def _list_my_tickets_impl(ctx: SecurityCtx) -> str:
    tickets = store.list_tickets_for_requester(ctx["tenant"], ctx["principal"])
    if not tickets:
        return "You have no support tickets on file."
    lines = [
        f"- #{t['id']} — {t['status']} ({t['priority']} priority): {t['subject']}"
        for t in tickets
    ]
    return "\n".join(lines)


@tool(args_schema=ListMyTicketsArgs)
def list_my_tickets(config: RunnableConfig) -> str:
    """List the current customer's own support tickets, most recent first.
    Use this when a customer asks about "my tickets" or "what have I
    reported" without naming a specific ticket number."""
    ctx = _ctx_or_refuse(config, "list_my_tickets")
    if ctx is None:
        return _NO_CTX_REFUSAL
    return _run_with_timeout(_list_my_tickets_impl, ctx)


class AddTicketCommentArgs(BaseModel):
    ticket_id: int = Field(..., description="The ticket number to add a follow-up comment to.")
    comment: str = Field(..., description="Additional detail the customer just provided.")

    @field_validator("comment")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("comment must not be empty")
        return v


def _add_ticket_comment_impl(ticket_id: int, comment: str, ctx: SecurityCtx) -> str:
    updated = store.add_comment(ctx["tenant"], ticket_id, comment)
    if not updated:
        return f"No ticket #{ticket_id} found to add a comment to."
    return f"Added your follow-up to ticket #{ticket_id}."


@tool(args_schema=AddTicketCommentArgs)
def add_ticket_comment(ticket_id: int, comment: str, config: RunnableConfig) -> str:
    """Add more detail to an existing ticket the customer already opened —
    use this when they follow up with extra information rather than
    opening a duplicate ticket for the same issue."""
    ctx = _ctx_or_refuse(config, "add_ticket_comment")
    if ctx is None:
        return _NO_CTX_REFUSAL
    return _run_with_timeout(_add_ticket_comment_impl, ticket_id, comment, ctx)


class FetchExternalReferenceArgs(BaseModel):
    url: str = Field(
        ..., description="A third-party page relevant to the customer's issue (https:// only) — e.g. a link they shared."
    )


def _fetch_external_reference_impl(url: str) -> str:
    return render_url_to_markdown(url)


@tool(args_schema=FetchExternalReferenceArgs)
def fetch_external_reference(url: str, config: RunnableConfig) -> str:
    """Read a customer-linked or otherwise relevant third-party page LIVE
    (real headless-browser render) for THIS turn's answer — e.g. the
    customer links the API/webhook doc that doesn't match what they're
    seeing. This does NOT add anything to the knowledge base (search_docs
    is unaffected) — it's a one-off live read, never a corpus write, which
    is why this domain can offer it without breaking its own "sandboxed to
    knowledge base + ticket system" design. Reaches the open internet —
    declared "outward" in TOOL_CAPABILITIES, so it always requires human
    approval before it runs."""
    ctx = _ctx_or_refuse(config, "fetch_external_reference")
    if ctx is None:
        return _NO_CTX_REFUSAL
    return _run_with_timeout(
        _fetch_external_reference_impl, url, _timeout_seconds=CRAWL_TOOL_TIMEOUT_SECONDS
    )


def _thread_id_from_config(config: RunnableConfig | None) -> str:
    return (config or {}).get("configurable", {}).get("thread_id", "unknown")


# Three narrow, purpose-built tools over OpenSandbox's raw MCP catalog
# (app/domains/sandbox_session.py, GRAPH_PATTERNS.md pattern 50) — the same
# shared plumbing app/domains/ops/tools.py already uses (see its own
# comment for the full "why not the raw ~19-tool catalog" writeup); this is
# just this domain's own @tool wrapper layer around it, same "one shared
# impl, one wrapper per domain" shape fetch_external_reference already has
# around render_url_to_markdown.
#
# Always defined and always in TOOLS/TOOL_CAPABILITIES below — NOT gated on
# opensandbox-mcp's reachability at this module's own import time, unlike
# an earlier version of this code (see sandbox_session.load_raw_sandbox_tools's
# own docstring for the live trace that surfaced why: a process that
# finished booting before opensandbox-server finished starting cached an
# empty tool set forever, with no self-healing short of a restart). These
# three tools now match fetch_external_reference's own established shape
# instead: always present, each impl below calls
# sandbox_session.load_raw_sandbox_tools() FRESH on every call and raises a
# plain, catchable error if it's still empty.


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
    use this for plain shell tasks (grep/diff, reading a file with
    `cat <path>`), NOT for Python computation — for actually PARSING a
    customer-pasted error log, stack trace, or webhook/JSON payload,
    use run_python_in_sandbox instead: it takes your script as a plain
    parameter, no shell quoting to get right. Passing Python source
    here via `python -c '...'` is exactly the mistake
    run_python_in_sandbox exists to make impossible — a pasted payload
    with its own quote or apostrophe collides with the shell's own
    quoting and silently breaks the script, confirmed via repeated
    real failures. For the full worked procedure, call
    use_skill("support-log-triage") before writing anything — don't
    freehand it. One sandbox is created automatically per conversation
    and reused for every call in it — you never create, connect to, or
    track a sandbox yourself, just describe the command. Don't
    `pip install` anything (no network access) — numpy and pandas are
    already available if you need them. Reaches an external service —
    always requires human approval before it runs."""
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
    """Run real Python computation to actually PARSE a customer-pasted
    error log, stack trace, or webhook/JSON payload (count occurrences
    of an error code, pull out the real exception type, validate the
    JSON is even well-formed) instead of eyeballing raw pasted text —
    use this, not run_command_in_sandbox, for anything beyond a single
    shell command. Pass your FULL Python source as `script`, exactly as
    you'd write it in a file — multi-line code, quotes, apostrophes,
    f-strings, dict literals, all fine, none of it goes through a
    shell; embed the customer's pasted content directly in your script,
    e.g. as a string literal fed to `json.loads`/a regex. Also useful
    together with fetch_external_reference: crawl a docs/status page
    live, then check a specific customer-reported value (a rate limit,
    a version number) against what that page actually says, rather
    than comparing them by eye. If you want to know whether this exact
    issue has come up in other tickets before adding a comment or
    opening a new one, that read-only history lookup is exactly what
    the ticket-researcher subagent (run_subagent) already exists for.
    For the full worked procedure, call use_skill("support-log-triage")
    BEFORE writing anything — don't freehand it. One sandbox is
    created automatically per conversation and reused for every call
    in it (shared with run_command_in_sandbox) — you never create,
    connect to, or track a sandbox yourself, or write the script to a
    file first. Don't `pip install` anything (no network access) —
    numpy and pandas are already available if you need them. Reaches
    an external service — always requires human approval before it
    runs."""
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
    """Read a text file from this conversation's sandbox (e.g. a
    script's output written to disk, or a file written earlier with
    write_sandbox_file). Same auto-created sandbox as
    run_command_in_sandbox. If this returns an unexpected "file not
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
    """Write a text file into this conversation's sandbox (e.g. stage
    a big pasted log dump before parsing it with run_command_in_sandbox,
    rather than passing all of it inline in a command string). Same
    auto-created sandbox as run_command_in_sandbox. Reaches an
    external service — always requires human approval before it
    runs."""
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
    create_ticket,
    check_ticket_status,
    escalate_to_human,
    list_my_tickets,
    add_ticket_comment,
    fetch_external_reference,
    *_SANDBOX_TOOLS,
]

TOOL_CAPABILITIES = {
    "create_ticket": "mutating",
    "check_ticket_status": "read_only",
    "escalate_to_human": "mutating",
    "list_my_tickets": "read_only",
    "add_ticket_comment": "mutating",
    "fetch_external_reference": "outward",
    **{t.name: "outward" for t in _SANDBOX_TOOLS},
}
