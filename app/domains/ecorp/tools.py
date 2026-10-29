"""The Ecorp domain's own tools: `add_note` and `remember` (both `mutating`, so every call is gated behind
`human_approval`) and `query_employees` (a fixed, structured `read_only` query).

Tool layer only: argument schemas, `ctx_or_refuse`, `idempotent()` and the timeout wrapper. Data access is in
`store.py`. Both follow `.claude/rules/side-effect-tools.md`.

All tools declare an explicit Pydantic `args_schema`, the JSON schema the LLM sees, constraining what it can
send and rejecting bad arguments loudly. `add_note`/`remember` lean on it harder: their args are a fixed,
closed set, never a free-form or model-generated write target (GRAPH_PATTERNS.md "fixed tools, never generated
queries").

## Tenant isolation (app/core/security.py)

`add_note`, `remember` and `query_employees` receive `config: RunnableConfig` (auto-injected by `ToolNode`,
auto-excluded from the LLM-visible schema) to read `SecurityCtx` from `config["configurable"]["ctx"]`. Each
calls `ctx_or_refuse` first and fails closed if `ctx` is missing or malformed, rather than running an unscoped
query or untenanted write.
"""
from typing import Annotated

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolCallId, tool
from pydantic import BaseModel, Field, field_validator

from app.agent.tools.builtin import ToolCapability, Topic
from app.core.tool_idempotency import idempotent
from app.core.tool_runtime import NO_CTX_REFUSAL, arun_with_timeout, ctx_or_refuse
from app.core.tool_schema import ToolArgs
from app.domains.ecorp.usecases import (
    MAX_MEMORY_CONTENT_CHARS,
    MAX_NOTE_CONTENT_CHARS,
    MAX_NOTE_TITLE_CHARS,
    Department,
    add_note_impl,
    query_employees_impl,
    remember_impl,
)


class AddNoteArgs(BaseModel):
    title: str = Field(..., max_length=MAX_NOTE_TITLE_CHARS, description="Short title for the note.")
    content: str = Field(..., max_length=MAX_NOTE_CONTENT_CHARS, description="The note's text.")
    topic: Topic = Field(
        ..., description="Must be one of: langgraph, qdrant, company."
    )
    # Injected by ToolNode, never shown to the LLM — must be declared on
    # the args_schema itself (LangChain's InjectedToolCallId detection
    # scans args_schema fields, unlike `config: RunnableConfig` which scans
    # the raw function signature). Lets add_note run through
    # tool_idempotency.idempotent(): a reclaimed, retried job re-invoking
    # this exact tool call (same id) gets the first call's own result back
    # instead of writing a second, duplicate note.
    tool_call_id: Annotated[str, InjectedToolCallId]

    @field_validator("title", "content")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must not be empty")
        return v


@tool(args_schema=AddNoteArgs)
async def add_note(
    title: str,
    content: str,
    topic: Topic,
    config: RunnableConfig,
    tool_call_id: Annotated[str, InjectedToolCallId],
) -> str:
    """Add a new note to the knowledge base so future searches can find it.

    This WRITES to the knowledge base — unlike search_docs/calculator, this
    changes what other users and future turns will see. It is declared
    "mutating" in TOOL_CAPABILITIES, which means app/agent/graph/core.py's
    should_continue routes it through human_approval every time, regardless
    of whether the caller opted into require_approval.
    """
    ctx = ctx_or_refuse(config, "write_note")
    if ctx is None:
        return NO_CTX_REFUSAL
    return await idempotent(
        tool_call_id=tool_call_id,
        ctx=ctx,
        config=config,
        tool_name="add_note",
        fn=lambda: arun_with_timeout(add_note_impl, title, content, topic, ctx, tool_call_id),
    )


class RememberArgs(BaseModel):
    content: str = Field(
        ...,
        max_length=MAX_MEMORY_CONTENT_CHARS,
        description="The fact to remember about this conversation/user.",
    )
    # See AddNoteArgs's own tool_call_id field for why this is here.
    tool_call_id: Annotated[str, InjectedToolCallId]

    @field_validator("content")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("content must not be empty")
        return v


@tool(args_schema=RememberArgs)
async def remember(
    content: str, config: RunnableConfig, tool_call_id: Annotated[str, InjectedToolCallId]
) -> str:
    """Save a fact about this user/conversation for future turns and
    sessions to recall — e.g. a stated preference or a piece of context
    they'll likely reference again.

    This WRITES a persistent, cross-session memory — declared "mutating"
    in TOOL_CAPABILITIES, so it's gated behind human_approval every time,
    same as add_note. Only use this for something worth remembering
    long-term, not for information already answerable from the current
    conversation.
    """
    ctx = ctx_or_refuse(config, "write_memory")
    if ctx is None:
        return NO_CTX_REFUSAL
    return await idempotent(
        tool_call_id=tool_call_id,
        ctx=ctx,
        config=config,
        tool_name="remember",
        fn=lambda: arun_with_timeout(remember_impl, content, ctx, tool_call_id),
    )


class QueryEmployeesArgs(ToolArgs):
    # Required but nullable (see ToolArgs): `null` means "do not narrow by this".
    department: Department | None = Field(
        ..., description="Filter: one of Engineering, Support, Sales. Pass null for any department."
    )
    name_contains: str | None = Field(
        ..., description="Case-insensitive substring match on name. Pass null for any name."
    )


@tool(args_schema=QueryEmployeesArgs)
async def query_employees(
    config: RunnableConfig,
    department: Department | None = None,
    name_contains: str | None = None,
) -> str:
    """Look up Ecorp employees, optionally filtered by department or
    name. Call this directly, immediately, the moment a question needs a
    staff/roster answer — it is read-only public staff-directory
    information, never requires confirmation first, and there is nothing
    to ask permission for. A fixed, structured-data query — not a database
    the model can ask arbitrary questions of; department and name_contains
    are the only two ways to narrow the result set. NOT for a general
    company-facts question (hours, policies, procedures) just because it
    mentions a department-sounding word — "support hours" asks about
    business hours, not who's on the Support team; that belongs to
    search_docs instead. This tool only answers "who works here", never
    "what are the hours/policies"."""
    ctx = ctx_or_refuse(config, "query_structured_data")
    if ctx is None:
        return NO_CTX_REFUSAL
    return await arun_with_timeout(query_employees_impl, department, name_contains, ctx)


# The action tools Ecorp adds to the shared ones (`app/agent/tools/builtin.py`). Each declares its
# capability tier here; an undeclared tool is treated as "outward" (fail closed).
TOOLS = [add_note, remember, query_employees]

TOOL_CAPABILITIES: dict[str, ToolCapability] = {
    "query_employees": "read_only",
    "add_note": "mutating",
    "remember": "mutating",
}
