"""LangGraph tools the agent can call.

- `search_docs`: tenant-scoped retrieval from Qdrant, with an optional
  metadata (topic) filter.
- `calculator`: a third tool so the agent must *choose* which to call.
- `add_note`: writes a new note into the Qdrant knowledge base — a
  *mutating* tool. See `TOOL_CAPABILITIES` below for why it's declared, not
  just implemented, as mutating.
- `remember`: the *only* way a memory gets written — a second mutating
  tool. See the "Cross-session memory" section below for why recall is
  automatic (folded into retrieve_context) but writing never is.

All tools declare an explicit **Pydantic `args_schema`**. This is the robust
way to define tool inputs: the schema (enums, descriptions, validators) is what
the LLM sees as the tool's JSON schema, so it constrains what the model can
send and rejects bad arguments loudly instead of failing silently. `add_note`
and `remember` lean on this harder than the read-only tools do: their args are
a fixed, closed set — there is no free-form query or generated-write path
here, deliberately. A tool that let the model construct its own write target
(the equivalent of letting it generate SQL) would defeat the whole point of
gating writes behind a narrow, reviewable surface — see GRAPH_PATTERNS.md's
"fixed tools, never generated queries" note.

## Tenant isolation (app/security.py)

`search_docs`, `add_note`, and `remember` all receive `config:
RunnableConfig` — a LangChain-standard parameter that's auto-injected by
`ToolNode` and, critically, auto-*excluded* from the schema the LLM sees
(verified: `tool.args` never lists it) — to read `SecurityCtx` from
`config["configurable"]["ctx"]`. Every one of them calls `_ctx_or_refuse`
first and refuses (fails closed) rather than running an unscoped query or
an untenanted write if `ctx` is missing or malformed. This is the same
"config carries what the LLM must never see or set" channel app/graph.py's
nodes already use for `deps` — ctx just travels one level further, into
the tools themselves.

## Cross-session memory

A memory is written *only* by the `remember` tool — nothing in this app
extracts facts from turn text automatically. That's deliberate: whatever
writes memory decides what gets replayed into every future prompt, which
makes autonomous extraction a privileged, unaudited side channel. Recall,
by contrast, is automatic — folded into `_default_search` (app/graph.py)
alongside document retrieval, and re-filtered against *current* ctx on
every call (`Policy.lower(ctx, "memories")`, scoped to `tenant` AND
`owner`) rather than trusted from a prior turn's snapshot. A recalled
memory is framed exactly like a retrieved document — untrusted content,
delimited, never an instruction — because it's the same textbook injection
vector with a longer memory: unlike a one-off retrieved chunk, a poisoned
memory keeps coming back on every later turn until removed
(`qdrant_store.delete_by_filter` is the removal mechanism; deliberately not
an agent-facing tool — see its docstring for why).
"""
import ast
import concurrent.futures
import operator
import uuid
from enum import Enum
from typing import Literal

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from pydantic import BaseModel, Field, field_validator
from qdrant_client.models import PointStruct

from app import qdrant_store
from app.config import DEFAULT_TENANT
from app.embeddings import embed_text
from app.security import DEFAULT_POLICY, SecurityCtx, valid_ctx

# Whitelisted operators for the safe calculator.
_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Pow: operator.pow,
    ast.USub: operator.neg,
}

# Safety budget: bound how long any single tool call can block the graph
# (a hung Qdrant/embedding call, or a pathological expression like
# `2**99999999999`, would otherwise stall — or in the exponent case,
# potentially exhaust memory in — the whole turn indefinitely).
TOOL_TIMEOUT_SECONDS = 15
_TOOL_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=8, thread_name_prefix="tool-timeout"
)


def _run_with_timeout(func, *args, **kwargs):
    """Run `func` in a worker thread and stop waiting after
    TOOL_TIMEOUT_SECONDS. The raised TimeoutError is caught by ToolNode's
    `handle_tool_errors=_friendly_tool_error` in app/graph.py and turned
    into a message the agent sees on its next turn, same as any other tool
    exception.

    Soft timeout: Python can't forcibly kill the worker thread, so this
    bounds how long the *graph* waits, not how long the call actually runs
    in the background.
    """
    future = _TOOL_EXECUTOR.submit(func, *args, **kwargs)
    try:
        return future.result(timeout=TOOL_TIMEOUT_SECONDS)
    except concurrent.futures.TimeoutError as exc:
        raise TimeoutError(
            f"Tool call exceeded the {TOOL_TIMEOUT_SECONDS}s timeout."
        ) from exc


_NO_CTX_REFUSAL = (
    "Refused: no valid tenant/principal context for this request. "
    "This isn't something you can work around — it means the request "
    "never got a security context stamped on it upstream."
)


def _ctx_from_config(config: RunnableConfig | None) -> SecurityCtx | None:
    if not config:
        return None
    return config.get("configurable", {}).get("ctx")


def _ctx_or_refuse(config: RunnableConfig | None, action: str) -> SecurityCtx | None:
    """The one fail-closed check every ctx-aware tool makes first: a
    missing/malformed ctx, or a ctx this Policy doesn't `permit` for
    `action`, returns None (the caller returns _NO_CTX_REFUSAL) instead of
    running an unscoped query or an untenanted write. Never raises — a
    refusal is a normal ToolMessage the agent sees and can react to, not an
    exception path (consistent with handle_tool_errors existing for actual
    failures, not for "this request was never allowed")."""
    ctx = _ctx_from_config(config)
    if not valid_ctx(ctx) or not DEFAULT_POLICY.permit(action, ctx):
        return None
    return ctx


def _safe_eval(node: ast.AST) -> float:
    if isinstance(node, ast.Constant):  # numbers
        return node.value
    if isinstance(node, ast.BinOp):
        return _OPS[type(node.op)](_safe_eval(node.left), _safe_eval(node.right))
    if isinstance(node, ast.UnaryOp):
        return _OPS[type(node.op)](_safe_eval(node.operand))
    raise ValueError("Unsupported expression")


class Topic(str, Enum):
    """The only topics that exist in the knowledge base."""

    langgraph = "langgraph"
    qdrant = "qdrant"
    company = "company"


class SearchDocsArgs(BaseModel):
    query: str = Field(..., description="Natural-language search query.")
    topic: Topic | None = Field(
        default=None,
        description="Optional filter. Must be one of: langgraph, qdrant, company. "
        "Omit it to search across all topics.",
    )


class CalculatorArgs(BaseModel):
    expression: str = Field(..., description="Arithmetic expression, e.g. '21 * 2'.")

    @field_validator("expression")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("expression must not be empty")
        return v


def _search_docs_impl(query: str, topic: Topic | None, ctx: SecurityCtx) -> str:
    topic_value = topic.value if isinstance(topic, Topic) else topic
    tenant_filter = DEFAULT_POLICY.lower(ctx, "documents")
    hits = qdrant_store.search(
        embed_text(query), topic=topic_value, k=3, tenant_filter=tenant_filter
    )
    if not hits:
        return "No relevant documents found."
    return "\n".join(f"- {h.payload['text']}" for h in hits)


@tool(args_schema=SearchDocsArgs)
def search_docs(
    query: str, config: RunnableConfig, topic: Topic | None = None
) -> str:
    """Search the knowledge base for relevant documents."""
    ctx = _ctx_or_refuse(config, "search")
    if ctx is None:
        return _NO_CTX_REFUSAL
    return _run_with_timeout(_search_docs_impl, query, topic, ctx)


def _calculator_impl(expression: str) -> str:
    try:
        return str(_safe_eval(ast.parse(expression, mode="eval").body))
    except Exception:  # noqa: BLE001
        return f"Could not evaluate: {expression!r}"


@tool(args_schema=CalculatorArgs)
def calculator(expression: str) -> str:
    """Evaluate a basic arithmetic expression, e.g. '21 * 2'."""
    return _run_with_timeout(_calculator_impl, expression)


class AddNoteArgs(BaseModel):
    title: str = Field(..., description="Short title for the note.")
    content: str = Field(..., description="The note's text.")
    topic: Topic = Field(
        ..., description="Must be one of: langgraph, qdrant, company."
    )

    @field_validator("title", "content")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must not be empty")
        return v


def _add_note_impl(title: str, content: str, topic: Topic, ctx: SecurityCtx) -> str:
    """Embed and upsert one new point into the knowledge base.

    A fixed, single-purpose write: the only variables are the three typed
    fields above (plus `ctx`, which the model never sees or sets — see
    module docstring), so there's no query/filter/target the model
    constructs itself. A fresh UUID id (never a caller-supplied one) means
    this can only ever *add* a point, never overwrite or target an existing
    one by guessing its id — the write surface this tool exposes is exactly
    "append one note to my tenant's knowledge base," nothing broader.
    """
    text = f"{title}: {content}"
    vector = embed_text(text)
    point = PointStruct(
        id=str(uuid.uuid4()),
        vector=vector,
        payload={
            "text": text,
            "topic": topic.value,
            "title": title,
            "kind": "document",
            "tenant": ctx["tenant"],
        },
    )
    qdrant_store.upsert([point])
    return f"Note '{title}' added to the {topic.value} knowledge base."


@tool(args_schema=AddNoteArgs)
def add_note(
    title: str, content: str, topic: Topic, config: RunnableConfig
) -> str:
    """Add a new note to the knowledge base so future searches can find it.

    This WRITES to the knowledge base — unlike search_docs/calculator, this
    changes what other users and future turns will see. It is declared
    "mutating" in TOOL_CAPABILITIES, which means app/graph.py's
    should_continue routes it through human_approval every time, regardless
    of whether the caller opted into require_approval.
    """
    ctx = _ctx_or_refuse(config, "write_note")
    if ctx is None:
        return _NO_CTX_REFUSAL
    return _run_with_timeout(_add_note_impl, title, content, topic, ctx)


class RememberArgs(BaseModel):
    content: str = Field(
        ..., description="The fact to remember about this conversation/user."
    )

    @field_validator("content")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("content must not be empty")
        return v


def _remember_impl(content: str, ctx: SecurityCtx) -> str:
    """Embed and upsert one memory, owned by ctx["principal"] within
    ctx["tenant"] — see the module docstring's "Cross-session memory"
    section for why this is the *only* place a memory gets written."""
    vector = embed_text(content)
    point = PointStruct(
        id=str(uuid.uuid4()),
        vector=vector,
        payload={
            "text": content,
            "kind": "memory",
            "tenant": ctx["tenant"],
            "owner": ctx["principal"],
        },
    )
    qdrant_store.upsert([point])
    return "Remembered."


@tool(args_schema=RememberArgs)
def remember(content: str, config: RunnableConfig) -> str:
    """Save a fact about this user/conversation for future turns and
    sessions to recall — e.g. a stated preference or a piece of context
    they'll likely reference again.

    This WRITES a persistent, cross-session memory — declared "mutating"
    in TOOL_CAPABILITIES, so it's gated behind human_approval every time,
    same as add_note. Only use this for something worth remembering
    long-term, not for information already answerable from the current
    conversation.
    """
    ctx = _ctx_or_refuse(config, "write_memory")
    if ctx is None:
        return _NO_CTX_REFUSAL
    return _run_with_timeout(_remember_impl, content, ctx)


def recall_memories(ctx: SecurityCtx | None, query: str) -> str:
    """Fetch this principal's own memories, re-filtered against CURRENT
    ctx on every call — called automatically from app/graph.py's
    _default_search alongside document retrieval, never on the model's
    initiative (contrast with search_docs/add_note/remember, which the
    model chooses to call). Re-filtering every call, rather than trusting
    a cached/prior-turn result, is what makes a clearance change (if this
    app ever grows one) take effect on the very next turn instead of
    persisting until something invalidates a stale snapshot.

    Returns "" on missing ctx or no results — recall is enrichment, same
    reliability posture as retrieve_context itself (degrade, never fail
    the turn just because memory came back empty or ctx wasn't set).
    """
    if not valid_ctx(ctx) or not DEFAULT_POLICY.permit("recall_memory", ctx):
        return ""
    tenant_filter = DEFAULT_POLICY.lower(ctx, "memories")
    hits = qdrant_store.search(embed_text(query), k=5, tenant_filter=tenant_filter)
    if not hits:
        return ""
    return "\n".join(f"- {h.payload['text']}" for h in hits)


TOOLS = [search_docs, calculator, add_note, remember]

# --- Tool capability declarations -------------------------------------------
# Every tool declares which "leg" of exposure it adds: read_only (safe to run
# immediately), mutating (writes/changes persisted state), or outward
# (reaches outside the corpus — sends, calls an external service, etc.).
# app/graph.py's should_continue enforces this: a tool_call batch containing
# ANY non-read_only tool is routed through human_approval unconditionally —
# "mandatory," not the opt-in require_approval gate — because a retrieval-
# augmented agent's context is untrusted content on essentially every turn
# (see GRAPH_PATTERNS.md pattern 12): once a run already carries "exposure to
# untrusted content," adding "ability to mutate state" is the second of the
# two legs a run may hold unsupervised, and the access-to-private-data leg
# (app/security.py's tenant/owner isolation) is never worth gambling the
# third on too. A tool absent from this mapping defaults to "outward" — fail
# closed, so a new tool added to TOOLS without a capability entry is gated
# rather than silently trusted.
ToolCapability = Literal["read_only", "mutating", "outward"]

TOOL_CAPABILITIES: dict[str, ToolCapability] = {
    "search_docs": "read_only",
    "calculator": "read_only",
    "add_note": "mutating",
    "remember": "mutating",
}
