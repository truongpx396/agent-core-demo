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
from collections.abc import Callable
from datetime import UTC, datetime
from enum import Enum
from typing import Literal

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from pydantic import BaseModel, Field, field_validator

from app import qdrant_store
from app.embeddings import embed_sparse, embed_text
from app.scrubbing import scrub
from app.security import DEFAULT_POLICY, SecurityCtx, valid_ctx

# Whitelisted operators for the safe calculator.
_OPS: dict[type, Callable[..., float]] = {
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

    The result is scrubbed (app/scrubbing.py, GRAPH_PATTERNS.md pattern
    32) before it reaches the caller — the one chokepoint every read/write
    tool impl in this module funnels through, so a credential-shaped or
    actually-bound-secret value in a tool's raw result (a database row, a
    fetched document) never reaches the model's next prompt or a trace.
    """
    future = _TOOL_EXECUTOR.submit(func, *args, **kwargs)
    try:
        result = future.result(timeout=TOOL_TIMEOUT_SECONDS)
    except concurrent.futures.TimeoutError as exc:
        raise TimeoutError(
            f"Tool call exceeded the {TOOL_TIMEOUT_SECONDS}s timeout."
        ) from exc
    return scrub(result) if isinstance(result, str) else result


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
        if not isinstance(node.value, int | float):
            raise ValueError("Unsupported expression")
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
    doc_ids: list[str] | None = Field(
        default=None,
        description="Optional: scope the search to these specific document ids only "
        "(e.g. ones the caller already knows about/is cleared to read). Narrows the "
        "search — never a way to reach documents outside the caller's tenant.",
    )


class CalculatorArgs(BaseModel):
    expression: str = Field(..., description="Arithmetic expression, e.g. '21 * 2'.")

    @field_validator("expression")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("expression must not be empty")
        return v


def _display_text(hit) -> str:
    """The text a citation shows: `parent_text` when the hit is a child
    chunk of a larger parent (app/chunking.py, app/ingestor.py — the small
    child is what was *embedded and matched*, but the model gets the
    richer surrounding passage), falling back to the point's own `text`
    for anything not chunked this way (add_note/remember/pre-chunking
    sample docs, and memories, which are never split into parent/child)."""
    payload = hit.payload or {}
    return payload.get("parent_text") or payload.get("text", "")


def _dedupe_by_parent(hits: list) -> list:
    """Multiple child chunks from the SAME parent (app/chunking.py) can
    all score highly for one query — without this, the same parent
    passage would show up as two or three separate, redundant citations.
    Keeps the highest-ranked hit per `parent_id` (hits arrive pre-sorted
    by relevance) and drops the rest. Hits with no `parent_id` — memories,
    add_note/remember points, anything ingested before chunking existed —
    are never deduped against EACH OTHER: each is its own independent
    point, not a fragment of some larger shared unit."""
    seen_parents: set[str] = set()
    deduped = []
    for h in hits:
        parent_id = (h.payload or {}).get("parent_id")
        if parent_id:
            if parent_id in seen_parents:
                continue
            seen_parents.add(parent_id)
        deduped.append(h)
    return deduped


def _format_cited_context(hits: list, offset: int = 0) -> str:
    """`[n] chunk text`, one per line — the citation-marker convention
    every retrieval surface (search_docs, gather_context) uses
    consistently, so the model sees the same shape whether context
    arrived pre-fetched (retrieve_context) or via an on-demand tool call.
    `offset` lets callers combining several hit lists (documents, then
    memories) keep one continuously-numbered sequence across all of them.
    """
    return "\n".join(
        f"[{i + offset + 1}] {_display_text(h)}" for i, h in enumerate(hits)
    )


def _citation_records(hits: list, offset: int = 0) -> list[dict]:
    """Structured citation metadata — marker, source id, title, text,
    relevance score — one per hit, same numbering as
    `_format_cited_context`. app/graph.py stores these in
    State["citations"]; check_output filters them down to the ones the
    final answer actually referenced (GRAPH_PATTERNS.md pattern 20)."""
    records = []
    for i, h in enumerate(hits):
        payload = h.payload or {}
        records.append(
            {
                "marker": f"[{i + offset + 1}]",
                "doc_id": str(h.id),
                "title": payload.get("title") or payload.get("topic") or payload.get("kind", "source"),
                "text": _display_text(h),
                "score": float(h.score),
            }
        )
    return records


def _sparse_vector_or_none(text: str) -> tuple[list[int], list[float]] | None:
    """Best-effort BM25 sparse vector for a write path (add_note, remember):
    if the local sparse model fails to load/embed, the write still
    succeeds with a dense-only point (qdrant_store.build_point already
    treats `sparse_vector=None` as "dense-only") rather than blocking a
    human-approved write on a hybrid-search quality concern — the same
    degrade-don't-block posture app/qdrant_store.py's hybrid_search takes
    on the read side."""
    try:
        return embed_sparse(text)
    except Exception:  # noqa: BLE001 - degrade to dense-only, never block the write
        return None


def _document_hits(
    ctx: SecurityCtx,
    query: str,
    topic: Topic | str | None = None,
    doc_ids: list[str] | None = None,
):
    topic_value = topic.value if isinstance(topic, Topic) else topic
    # Policy.lower is computed from ctx FIRST and applied inside the same
    # query as doc_ids — doc_ids narrows this already-scoped filter, it
    # never substitutes for it; a caller can't pass doc_ids to reach a
    # point outside their own tenant, since the tenant predicate is
    # ANDed on regardless (app/qdrant_store.py::_build_filter).
    tenant_filter = DEFAULT_POLICY.lower(ctx, "documents")
    hits = qdrant_store.hybrid_search(
        query, topic=topic_value, tenant_filter=tenant_filter, doc_ids=doc_ids
    )
    return _dedupe_by_parent(hits)


def _memory_hits(ctx: SecurityCtx, query: str):
    # Reranking is skipped for memories: a principal's own (typically
    # small) memory set doesn't need cross-encoder precision, and skipping
    # it avoids paying for a second reranker call on every single turn —
    # recall runs automatically, unlike a one-off search_docs tool call.
    tenant_filter = DEFAULT_POLICY.lower(ctx, "memories")
    return qdrant_store.hybrid_search(query, tenant_filter=tenant_filter, rerank_results=False)


def _search_docs_impl(
    query: str, topic: Topic | None, ctx: SecurityCtx, doc_ids: list[str] | None = None
) -> str:
    hits = _document_hits(ctx, query, topic, doc_ids)
    if not hits:
        return "No relevant documents found."
    return _format_cited_context(hits)


@tool(args_schema=SearchDocsArgs)
def search_docs(
    query: str,
    config: RunnableConfig,
    topic: Topic | None = None,
    doc_ids: list[str] | None = None,
) -> str:
    """Search the knowledge base for relevant documents."""
    ctx = _ctx_or_refuse(config, "search")
    if ctx is None:
        return _NO_CTX_REFUSAL
    return _run_with_timeout(_search_docs_impl, query, topic, ctx, doc_ids)


def _calculator_impl(expression: str) -> str:
    try:
        return str(_safe_eval(ast.parse(expression, mode="eval").body))
    except Exception:  # noqa: BLE001
        return f"Could not evaluate: {expression!r}"


@tool(args_schema=CalculatorArgs)
def calculator(expression: str) -> str:
    """Evaluate a basic arithmetic expression, e.g. '21 * 2'."""
    return _run_with_timeout(_calculator_impl, expression)


class AskClarificationArgs(BaseModel):
    question: str = Field(
        ..., description="A brief restatement of what's ambiguous about the request."
    )
    options: list[str] = Field(
        ...,
        min_length=2,
        max_length=4,
        description="2-4 concrete interpretations to choose from — never a single "
        "option, and never more than 4.",
    )


def _ask_clarification_impl(question: str, options: list[str]) -> str:
    numbered = "\n".join(f"{i + 1}. {opt}" for i, opt in enumerate(options))
    return (
        f"{question}\n{numbered}\n\n"
        "(Or describe what you meant in your own words.)"
    )


@tool(args_schema=AskClarificationArgs)
def ask_clarification(question: str, options: list[str]) -> str:
    """Use this ONLY when a question is ambiguous in a way that would
    materially change the answer — offer 2-4 concrete interpretations
    instead of guessing. Deliberately NOT special-cased in the graph
    (GRAPH_PATTERNS.md pattern 27): this is an ordinary read_only tool —
    its result is a formatted options list that becomes a ToolMessage,
    and the SYSTEM_PROMPT instructs the model to relay that list back to
    the user verbatim as its final answer on the very next turn, rather
    than trying to answer the original (still-ambiguous) question. No new
    node, no new routing — the existing agent -> tools -> agent loop
    already does exactly what this needs.
    """
    return _run_with_timeout(_ask_clarification_impl, question, options)


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
    point = qdrant_store.build_point(
        point_id=str(uuid.uuid4()),
        dense_vector=embed_text(text),
        sparse_vector=_sparse_vector_or_none(text),
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
    section for why this is the *only* place a memory gets written.

    `created_at` (UTC, ISO 8601) is what `Policy.lower`'s retention-at-
    recall range filter (app/security.py, GRAPH_PATTERNS.md pattern 33)
    and `app/memory.py::delete_memories`'s age-based selector both read —
    stamped once, here, never derived from anything caller-supplied.
    """
    point = qdrant_store.build_point(
        point_id=str(uuid.uuid4()),
        dense_vector=embed_text(content),
        sparse_vector=_sparse_vector_or_none(content),
        payload={
            "text": content,
            "kind": "memory",
            "tenant": ctx["tenant"],
            "owner": ctx["principal"],
            "created_at": datetime.now(UTC).isoformat(),
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
    hits = _memory_hits(ctx, query)
    if not hits:
        return ""
    return _format_cited_context(hits)


def gather_context(ctx: SecurityCtx | None, query: str) -> tuple[str, list[dict]]:
    """Documents + this principal's memories, hybrid-searched and combined
    into ONE continuously-numbered citation sequence — this is what
    app/graph.py's `_default_search` calls (the automatic pre-fetch path
    that runs every turn), NOT the same call `recall_memories`/
    `_search_docs_impl` make on their own, though all three share the same
    retrieval/Policy machinery underneath.

    Degrades to `("", [])` on a missing ctx or a retrieval failure —
    enrichment, never fails the turn (see retrieve_context's docstring in
    app/graph.py, which wraps this in the actual try/except).
    """
    if not valid_ctx(ctx):
        return "", []
    doc_hits = _document_hits(ctx, query) if DEFAULT_POLICY.permit("search", ctx) else []
    memory_hits = _memory_hits(ctx, query) if DEFAULT_POLICY.permit("recall_memory", ctx) else []
    all_hits = list(doc_hits) + list(memory_hits)
    if not all_hits:
        return "", []
    return _format_cited_context(all_hits), _citation_records(all_hits)


class Department(str, Enum):
    """The only departments that exist — same closed-vocabulary approach
    as `Topic`, and for the same reason: `query_employees` narrows to
    exactly what's askable, never a free-form filter the model writes."""

    engineering = "Engineering"
    support = "Support"
    sales = "Sales"


class QueryEmployeesArgs(BaseModel):
    department: Department | None = Field(
        default=None, description="Optional filter. One of: Engineering, Support, Sales."
    )
    name_contains: str | None = Field(
        default=None, description="Optional case-insensitive substring match on name."
    )


def _query_employees_impl(
    department: Department | None, name_contains: str | None, ctx: SecurityCtx
) -> str:
    from app import sql_store

    cap = TOOL_RESULT_CAPS["query_employees"]
    rows = sql_store.query_employees(
        tenant=ctx["tenant"],
        department=department.value if isinstance(department, Department) else department,
        name_contains=name_contains,
        limit=cap + 1,  # +1: enough to detect "more than cap exist" without a second query
    )
    if not rows:
        return "No matching employees found."

    truncated = len(rows) > cap
    rows = rows[:cap]
    lines = [
        f"- {r['name']} — {r['title']}, {r['department']} (hired {r['hired_on']})"
        for r in rows
    ]
    if truncated:
        # Marked, never silently shortened — "these are the first {cap}
        # matches" and "these are all the matches" read very differently,
        # and a caller/model acting on the count needs to know which one
        # this is (see TOOL_RESULT_CAPS's docstring).
        lines.append(f"[truncated: showing the first {cap} matches; more exist]")
    return "\n".join(lines)


@tool(args_schema=QueryEmployeesArgs)
def query_employees(
    config: RunnableConfig,
    department: Department | None = None,
    name_contains: str | None = None,
) -> str:
    """Look up Acme Corp employees, optionally filtered by department or
    name. A fixed, structured-data query — not a database the model can
    ask arbitrary questions of; department and name_contains are the only
    two ways to narrow the result set."""
    ctx = _ctx_or_refuse(config, "query_structured_data")
    if ctx is None:
        return _NO_CTX_REFUSAL
    return _run_with_timeout(_query_employees_impl, department, name_contains, ctx)


TOOLS = [search_docs, calculator, add_note, remember, query_employees, ask_clarification]

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
    "query_employees": "read_only",
    "ask_clarification": "read_only",
    "add_note": "mutating",
    "remember": "mutating",
}

# --- Tool result-size declarations -------------------------------------------
# A structured tool with no inherent row limit (query_employees's filters can
# match arbitrarily many rows) pays for a broad match in full at the store,
# then again formatting it, unless something bounds it. Declared per-tool
# (like TOOL_CAPABILITIES above) rather than one global constant, since the
# right cap is a property of the tool, not the app. A capped result is always
# MARKED as truncated (see _query_employees_impl) — "these are the first 20
# matches" and "these are all the matches" read very differently, and neither
# a caller nor the model acting on the count should have to guess which one
# it got.
TOOL_RESULT_CAPS: dict[str, int] = {
    "query_employees": 20,
}
