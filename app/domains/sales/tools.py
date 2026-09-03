"""Tools for the sales/CRM-concierge domain (app/domains/sales/domain.py):
log an inbound lead interaction, schedule a follow-up, package a brief on
a lead, hand a hot lead off to a human rep, check the pending follow-up
queue, and close out a lead that isn't going to convert. Same conventions
as app/agent/tools.py / app/domains/support/tools.py throughout (Pydantic
args_schema, RunnableConfig ctx, _run_with_timeout reuse, fail-closed ctx
check).

No tool here sends anything to the customer. "Drafts replies in your
voice" (the use case's own words) is satisfied by the LLM's ordinary final
answer, shaped by SALES_MANIFEST's system prompt (app/domains/sales/domain.py)
— a human reviews and actually sends it, the same "the model never gets a
send button" boundary app/agent/tools.py already draws for add_note/
remember (always human-approved) and app/domains/support/tools.py draws
for escalate_to_human.
"""
from datetime import UTC, datetime, timedelta

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from pydantic import BaseModel, Field, field_validator

from app.agent.tools import _run_with_timeout
from app.core.security import SecurityCtx, valid_ctx
from app.domains import notify
from app.domains.policy import ActionAllowlistPolicy
from app.domains.sales import store

_NO_CTX_REFUSAL = (
    "Refused: no valid tenant/principal context for this request. "
    "This isn't something you can work around — it means the request "
    "never got a security context stamped on it upstream."
)

SALES_POLICY = ActionAllowlistPolicy(
    frozenset(
        {
            "log_lead_interaction",
            "schedule_followup",
            "package_lead_brief",
            "handoff_to_human",
            "list_pending_followups",
            "mark_lead_lost",
        }
    )
)


def _ctx_from_config(config: RunnableConfig | None) -> SecurityCtx | None:
    if not config:
        return None
    return config.get("configurable", {}).get("ctx")


def _ctx_or_refuse(config: RunnableConfig | None, action: str) -> SecurityCtx | None:
    ctx = _ctx_from_config(config)
    if not valid_ctx(ctx) or not SALES_POLICY.permit(action, ctx):
        return None
    return ctx


class LogLeadInteractionArgs(BaseModel):
    name: str = Field(..., description="The lead's name.")
    contact: str = Field(..., description="Email, phone, or channel handle — the lead's stable identifier.")
    notes: str = Field(..., description="What was said/asked in this interaction.")

    @field_validator("name", "contact", "notes")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must not be empty")
        return v


def _log_lead_interaction_impl(name: str, contact: str, notes: str, ctx: SecurityCtx) -> str:
    lead_id = store.find_or_create_lead(ctx["tenant"], name, contact, notes)
    return f"Logged interaction for lead #{lead_id} ({name}, {contact})."


@tool(args_schema=LogLeadInteractionArgs)
def log_lead_interaction(name: str, contact: str, notes: str, config: RunnableConfig) -> str:
    """Record an inbound interaction with a lead — finds the existing lead
    by contact or creates a new one. Call this for every meaningful inbound
    message before deciding what to do next."""
    ctx = _ctx_or_refuse(config, "log_lead_interaction")
    if ctx is None:
        return _NO_CTX_REFUSAL
    return _run_with_timeout(_log_lead_interaction_impl, name, contact, notes, ctx)


class ScheduleFollowupArgs(BaseModel):
    contact: str = Field(..., description="The lead's contact — must already have been logged via log_lead_interaction.")
    due_in_days: int = Field(..., ge=0, le=365, description="How many days from now this follow-up is due.")
    note: str = Field(..., description="What to do/say at follow-up time.")

    @field_validator("note")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("note must not be empty")
        return v


def _schedule_followup_impl(contact: str, due_in_days: int, note: str, ctx: SecurityCtx) -> str:
    due_at = datetime.now(UTC) + timedelta(days=due_in_days)
    followup_id = store.add_followup(ctx["tenant"], contact, due_at, note, ctx["principal"])
    if followup_id is None:
        return f"No lead found for contact {contact!r} — log an interaction with them first."
    return f"Follow-up #{followup_id} scheduled for {due_at.date().isoformat()}: {note}"


@tool(args_schema=ScheduleFollowupArgs)
def schedule_followup(contact: str, due_in_days: int, note: str, config: RunnableConfig) -> str:
    """Schedule a future follow-up for a lead. scripts/followup_sweep.py
    (run via cron) picks these up once due and drafts a nudge for a human
    to review and send."""
    ctx = _ctx_or_refuse(config, "schedule_followup")
    if ctx is None:
        return _NO_CTX_REFUSAL
    return _run_with_timeout(_schedule_followup_impl, contact, due_in_days, note, ctx)


class PackageLeadBriefArgs(BaseModel):
    contact: str = Field(..., description="The lead's contact to brief on.")


def _package_lead_brief_impl(contact: str, ctx: SecurityCtx) -> str:
    history = store.lead_history(ctx["tenant"], contact)
    if history is None:
        return f"No lead found for contact {contact!r}."
    lines = [
        f"Lead: {history['name']} ({history['contact']})",
        f"Status: {history['status']}",
        f"Notes:\n{history['notes'] or '(none)'}",
    ]
    if history["followups"]:
        lines.append("Follow-ups:")
        lines.extend(
            f"  - [{f['status']}] due {f['due_at']}: {f['note']}" for f in history["followups"]
        )
    return "\n".join(lines)


@tool(args_schema=PackageLeadBriefArgs)
def package_lead_brief(contact: str, config: RunnableConfig) -> str:
    """Assemble a structured brief on a lead — history, notes, follow-ups
    — read-only, for either your own reasoning or as the summary handed to
    a human rep via handoff_to_human."""
    ctx = _ctx_or_refuse(config, "package_lead_brief")
    if ctx is None:
        return _NO_CTX_REFUSAL
    return _run_with_timeout(_package_lead_brief_impl, contact, ctx)


class HandoffToHumanArgs(BaseModel):
    contact: str = Field(..., description="The lead's contact to hand off.")
    brief_summary: str = Field(..., description="A short summary of why this lead is ready for a rep — usually built from package_lead_brief's output.")
    reason: str = Field(..., description="Why now — what made this lead 'hot'.")

    @field_validator("brief_summary", "reason")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must not be empty")
        return v


def _handoff_to_human_impl(contact: str, brief_summary: str, reason: str, ctx: SecurityCtx) -> str:
    updated = store.set_lead_status(ctx["tenant"], contact, "handed_off")
    if not updated:
        return f"No lead found for contact {contact!r} — nothing to hand off."
    notify.post_to_team_channel(
        "sales-handoffs",
        f"[{ctx['tenant']}] Hot lead {contact} handed off by {ctx['principal']} ({reason}):\n{brief_summary}",
    )
    return f"Lead {contact} handed off to a human rep."


@tool(args_schema=HandoffToHumanArgs)
def handoff_to_human(contact: str, brief_summary: str, reason: str, config: RunnableConfig) -> str:
    """Mark a lead 'hot' and hand it to a human rep with a packaged brief
    — use this once a lead is ready to talk to a person, never to send
    anything to the lead itself."""
    ctx = _ctx_or_refuse(config, "handoff_to_human")
    if ctx is None:
        return _NO_CTX_REFUSAL
    return _run_with_timeout(_handoff_to_human_impl, contact, brief_summary, reason, ctx)


class ListPendingFollowupsArgs(BaseModel):
    contact: str | None = Field(
        default=None,
        description="Optional: narrow to one lead's follow-ups. Omit to see the whole pending queue.",
    )


def _list_pending_followups_impl(contact: str | None, ctx: SecurityCtx) -> str:
    followups = store.list_pending_followups(ctx["tenant"], contact)
    if not followups:
        return "No pending follow-ups." if contact is None else f"No pending follow-ups for {contact!r}."
    lines = [
        f"- due {f['due_at']}: {f['lead_name']} ({f['contact']}) — {f['note']}" for f in followups
    ]
    return "\n".join(lines)


@tool(args_schema=ListPendingFollowupsArgs)
def list_pending_followups(config: RunnableConfig, contact: str | None = None) -> str:
    """List pending follow-ups, most-imminent first — the whole queue, or
    just one lead's if `contact` is given. Use this to see what's coming
    up rather than guessing whether a lead already has one scheduled."""
    ctx = _ctx_or_refuse(config, "list_pending_followups")
    if ctx is None:
        return _NO_CTX_REFUSAL
    return _run_with_timeout(_list_pending_followups_impl, contact, ctx)


class MarkLeadLostArgs(BaseModel):
    contact: str = Field(..., description="The lead's contact to mark lost.")
    reason: str = Field(..., description="Why this lead isn't converting — be specific.")

    @field_validator("reason")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("reason must not be empty")
        return v


def _mark_lead_lost_impl(contact: str, reason: str, ctx: SecurityCtx) -> str:
    updated = store.mark_lead_lost(ctx["tenant"], contact, reason)
    if not updated:
        return f"No lead found for contact {contact!r} — nothing to mark lost."
    return f"Lead {contact} marked lost ({reason}); its pending follow-ups were cancelled."


@tool(args_schema=MarkLeadLostArgs)
def mark_lead_lost(contact: str, reason: str, config: RunnableConfig) -> str:
    """Close out a lead that has clearly decided not to buy or gone
    unresponsive — cancels its pending follow-ups so it stops surfacing in
    the sweep. Use this instead of leaving a dead lead's follow-ups
    dangling; never use it just because a lead hasn't replied yet."""
    ctx = _ctx_or_refuse(config, "mark_lead_lost")
    if ctx is None:
        return _NO_CTX_REFUSAL
    return _run_with_timeout(_mark_lead_lost_impl, contact, reason, ctx)


TOOLS = [
    log_lead_interaction,
    schedule_followup,
    package_lead_brief,
    handoff_to_human,
    list_pending_followups,
    mark_lead_lost,
]

TOOL_CAPABILITIES = {
    "log_lead_interaction": "mutating",
    "schedule_followup": "mutating",
    "package_lead_brief": "read_only",
    "handoff_to_human": "mutating",
    "list_pending_followups": "read_only",
    "mark_lead_lost": "mutating",
}
