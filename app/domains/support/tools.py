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
from app.domains import notify
from app.domains.policy import ActionAllowlistPolicy
from app.domains.support import store

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


TOOLS = [
    create_ticket,
    check_ticket_status,
    escalate_to_human,
    list_my_tickets,
    add_ticket_comment,
]

TOOL_CAPABILITIES = {
    "create_ticket": "mutating",
    "check_ticket_status": "read_only",
    "escalate_to_human": "mutating",
    "list_my_tickets": "read_only",
    "add_ticket_comment": "mutating",
}
