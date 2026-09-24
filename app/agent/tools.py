"""LangGraph tools the agent can call.

- `search_docs`: tenant-scoped retrieval from Qdrant, optional topic filter.
- `calculator`: a third tool so the agent must *choose* which to call.
- `add_note`: writes a new note into the Qdrant knowledge base — a
  *mutating* tool (see `TOOL_CAPABILITIES` below).
- `remember`: the only way a memory gets written — also mutating. Recall
  is automatic (folded into retrieve_context); writing never is.
- `skill_search`/`use_skill`: progressive disclosure over a bundled catalog
  of `SKILL.md` packages (`app/agent/skills.py`, GRAPH_PATTERNS.md pattern
  45). `skill_search` hybrid-searches a small Qdrant collection of skill
  name/description metadata; `use_skill` loads one matched skill's full
  body from disk by exact name. Bundled app capability, not tenant data —
  no `SecurityCtx` involved.
- `run_subagent`: delegates a self-contained task to a fresh, ISOLATED
  nested agent run — a genuinely separate `graph.invoke()`, unlike
  `use_skill` which loads more instructions into THIS agent's context.
  Backed by `AGENT.md` packages (`app/agent/subagents.py`, GRAPH_PATTERNS.md
  pattern 46). Every subagent is restricted to `read_only` tools (enforced
  at catalog-build time, see `_resolve_subagent_tools`), so `run_subagent`
  needs no mandatory `human_approval` gating.

All tools declare an explicit Pydantic `args_schema` — the JSON schema the
LLM sees, constraining what it can send and rejecting bad arguments loudly.
`add_note`/`remember` lean on this harder: their args are a fixed, closed
set, never a free-form or model-generated write target (see
GRAPH_PATTERNS.md's "fixed tools, never generated queries").

## Tenant isolation (app/core/security.py)

`search_docs`, `add_note`, and `remember` receive `config: RunnableConfig`
(auto-injected by `ToolNode`, auto-excluded from the LLM-visible schema) to
read `SecurityCtx` from `config["configurable"]["ctx"]`. Each calls
`_ctx_or_refuse` first and fails closed if `ctx` is missing/malformed,
rather than running an unscoped query or untenanted write.

## Cross-session memory

Memory is written only by `remember` — nothing extracts facts from turn
text automatically, since whatever writes memory decides what gets
replayed into every future prompt. Recall is automatic (`_default_search`
in app/agent/graph.py, alongside document retrieval) and re-filtered
against *current* ctx every call (`Policy.lower(ctx, "memories")`, scoped
to `tenant` AND `owner`), never trusted from a prior snapshot. A recalled
memory is framed like a retrieved document — untrusted, delimited content
— because a poisoned memory keeps recurring every turn until removed
(`qdrant_store.delete_by_filter`; deliberately not agent-facing).
"""
import ast
import asyncio
import concurrent.futures
import logging
import operator
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from enum import Enum
from typing import Literal

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, tool

# `ChatOpenAI` isn't constructed in THIS file anymore (moved to
# subagent_tools.py's `_run_subagent_impl` fallback) — kept as a deliberate
# re-export: subagent_tools.py reads it live as `tools_module.ChatOpenAI`
# rather than importing it fresh, so tests/agent/test_concurrent_turns.py's
# `monkeypatch.setattr(tools_module, "ChatOpenAI", ...)` keeps working. Don't
# "clean up" this import.
from langchain_openai import ChatOpenAI  # noqa: F401
from pydantic import BaseModel, Field, field_validator

from app.agent import skills as skills_module
from app.core.config import (
    SKILLS_COLLECTION,
    SKILLS_SEARCH_TOP_K,
)
from app.core.scrubbing import scrub
from app.core.security import DEFAULT_POLICY, SecurityCtx, valid_ctx
from app.retrieval import qdrant_store
from app.retrieval.embeddings import embed_sparse, embed_text

logger = logging.getLogger(__name__)

# Whitelisted operators for the safe calculator.
_OPS: dict[type, Callable[..., float]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Pow: operator.pow,
    ast.USub: operator.neg,
}

# Safety budget: bounds how long one tool call can block the graph (a hung
# Qdrant call, or a pathological expression like `2**99999999999`, could
# otherwise stall — or exhaust memory — the turn indefinitely).
TOOL_TIMEOUT_SECONDS = 15

# Length budget for add_note/remember: bounds the BM25 sparse-embedding cost
# per write (embed_sparse has no internal truncation, unlike the ml-service
# ONNX models — see _sparse_vector_or_none) and keeps a note/memory citable
# in a prompt without ballooning it.
_MAX_NOTE_TITLE_CHARS = 200
_MAX_NOTE_CONTENT_CHARS = 4000
_MAX_MEMORY_CONTENT_CHARS = 2000
_TOOL_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=8, thread_name_prefix="tool-timeout"
)

# Real bug (Langfuse trace ed435567): the model cited [3] on every sentence
# of an answer unrelated to what [3] said. hybrid_search's RRF/dense
# ordering only says "most relevant of what came back," never "relevant
# enough" — so this is a floor on the cross-encoder's raw logit score
# (app/retrieval/embeddings.py's rerank(), unbounded, NOT a 0-1 similarity).
# Calibrated against live scores: irrelevant ~-11, on-topic +6.7,
# same-topic-but-not-quite -5.9. -8.0 sits between "wrong" and "plausibly
# related." Re-verified after switching reranker backends (TEI/bge-reranker-
# base -> ml-service's Xenova/ms-marco-MiniLM-L-6-v2): same shape held
# (+6.3 / -5.8..-6.8 / -11.3), so left unchanged.
MIN_RERANK_SCORE = -8.0


async def _arun_with_timeout(func, *args, _timeout_seconds: float | None = None, **kwargs):
    """Await `func(*args, **kwargs)` and stop waiting after `_timeout_seconds`
    (default TOOL_TIMEOUT_SECONDS). Raised TimeoutError is caught by
    ToolNode's `handle_tool_errors=_friendly_tool_error` in app/agent/graph.py
    and surfaced to the agent like any other tool exception.

    Every tool in this app uses this now (not `_run_with_timeout` below),
    wrapping a real coroutine with `asyncio.wait_for` since every tool impl
    now awaits an async client. `_run_with_timeout` still exists for the one
    caller with no running event loop to await on (see its own docstring).

    `_timeout_seconds` is keyword-only, resolved fresh per call rather than
    a plain default — a `= TOOL_TIMEOUT_SECONDS` default is bound once at
    definition time and would stop honoring
    `monkeypatch.setattr(tools, "TOOL_TIMEOUT_SECONDS", ...)` (caught by
    tests/agent/test_safety_budgets.py). `run_subagent` is the one caller
    that overrides it, to SUBAGENT_TIMEOUT_SECONDS.

    Soft timeout: `asyncio.wait_for` cancels the awaiting task, not
    necessarily whatever `func` is awaiting underneath — cancellation only
    takes effect at `func`'s next `await` point, so a call stuck in a
    non-cancellable operation can outlive this timeout in the background.

    Result is scrubbed (app/core/scrubbing.py, GRAPH_PATTERNS.md pattern 32)
    before returning — the one chokepoint every tool impl funnels through,
    so secrets in a raw result never reach the model or a trace.
    `run_subagent` returns a state dict here (scrubbing skips non-str
    results) and scrubs its extracted answer text separately.
    """
    timeout = _timeout_seconds if _timeout_seconds is not None else TOOL_TIMEOUT_SECONDS
    try:
        result = await asyncio.wait_for(func(*args, **kwargs), timeout=timeout)
    except TimeoutError as exc:
        raise TimeoutError(f"Tool call exceeded the {timeout}s timeout.") from exc
    return scrub(result) if isinstance(result, str) else result


def _run_with_timeout(func, *args, _timeout_seconds: float | None = None, **kwargs):
    """The sync survivor of `_arun_with_timeout`'s predecessor: still runs
    `func` in a worker thread via `concurrent.futures`, not `asyncio.wait_for`
    — its one caller, `app/domains/sandbox_tools.py::load_sandbox_tools`, runs
    at plain Python import time (eager domain composition), before any event
    loop exists. `mcp_client.load_remote_tools` (what it wraps) opens its own
    throwaway loop via `asyncio.run(...)` internally; this dispatches that
    whole call to a worker thread bounded by `future.result(timeout=...)`.
    Every other caller has moved to `_arun_with_timeout` — keep this one only
    for callers with no running loop, not as a general alternative.

    Same soft-timeout caveat (Python can't forcibly kill the worker thread)
    and same scrub() chokepoint as `_arun_with_timeout`.
    """
    timeout = _timeout_seconds if _timeout_seconds is not None else TOOL_TIMEOUT_SECONDS
    future = _TOOL_EXECUTOR.submit(func, *args, **kwargs)
    try:
        result = future.result(timeout=timeout)
    except concurrent.futures.TimeoutError as exc:
        raise TimeoutError(f"Tool call exceeded the {timeout}s timeout.") from exc
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
    """Fail-closed check every ctx-aware tool makes first: missing/malformed
    ctx, or a ctx Policy doesn't `permit` for `action`, returns None (caller
    returns _NO_CTX_REFUSAL) instead of running an unscoped query or
    untenanted write. Never raises — a refusal is a normal ToolMessage, not
    an exception path (handle_tool_errors is for actual failures)."""
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
    chunk of a larger parent (app/ingestion/chunking.py) — the child is what
    was embedded/matched, but the model gets the richer passage — falling
    back to the point's own `text` otherwise (add_note/remember/memories,
    never split into parent/child)."""
    payload = hit.payload or {}
    return payload.get("parent_text") or payload.get("text", "")


def _dedupe_by_parent(hits: list) -> list:
    """Multiple child chunks from the same parent (app/ingestion/chunking.py)
    can all score highly for one query — without this they'd show up as
    redundant citations. Keeps the highest-ranked hit per `parent_id` (hits
    arrive pre-sorted) and drops the rest. Hits with no `parent_id`
    (memories, add_note/remember points) are never deduped against each
    other."""
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
    """`[n] chunk text`, one per line — shared convention across
    search_docs/gather_context so the model sees the same shape whether
    context was pre-fetched or fetched via a tool call. `offset` lets
    callers combining several hit lists keep one continuous numbering."""
    return "\n".join(
        f"[{i + offset + 1}] {_display_text(h)}" for i, h in enumerate(hits)
    )


def _citation_records(hits: list, offset: int = 0) -> list[dict]:
    """Structured citation metadata — marker, source id, title, text,
    relevance score — one per hit, same numbering as
    `_format_cited_context`. app/agent/graph.py stores these in
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


async def _sparse_vector_or_none(text: str) -> tuple[list[int], list[float]] | None:
    """Best-effort BM25 sparse vector for a write path: if the sparse model
    fails to load/embed, the write still succeeds dense-only
    (`qdrant_store.build_point` treats `sparse_vector=None` as dense-only)
    rather than blocking a write on a hybrid-search quality concern — same
    degrade-don't-block posture as hybrid_search's read side.

    `embed_sparse` is local ONNX/CPU compute (`app/retrieval/embeddings.py`),
    so it's dispatched via `asyncio.to_thread` here too, same as
    `qdrant_store.py::hybrid_search`'s read-side call — a bare sync call
    would otherwise block every OTHER turn sharing this process's event
    loop for the embed's duration, and (worse) a `_arun_with_timeout`
    caller has no `await` point during a bare sync call for its
    `asyncio.wait_for` timeout to actually cancel."""
    try:
        return await asyncio.to_thread(embed_sparse, text)
    except Exception:  # noqa: BLE001 - degrade to dense-only, never block the write
        return None


async def _document_hits(
    ctx: SecurityCtx,
    query: str,
    topic: Topic | str | None = None,
    doc_ids: list[str] | None = None,
):
    topic_value = topic.value if isinstance(topic, Topic) else topic
    # doc_ids narrows the tenant filter, never substitutes for it — the
    # tenant predicate is ANDed on regardless (qdrant_store.py::_build_filter),
    # so a caller can't use doc_ids to reach another tenant's point.
    tenant_filter = DEFAULT_POLICY.lower(ctx, "documents")
    hits = await qdrant_store.hybrid_search(
        query,
        topic=topic_value,
        tenant_filter=tenant_filter,
        doc_ids=doc_ids,
        min_score=MIN_RERANK_SCORE,
    )
    return _dedupe_by_parent(hits)


async def _memory_hits(ctx: SecurityCtx, query: str):
    # Reranking skipped for memories: a principal's own memory set is small
    # and doesn't need cross-encoder precision — and recall runs
    # automatically every turn, unlike a one-off search_docs call.
    tenant_filter = DEFAULT_POLICY.lower(ctx, "memories")
    return await qdrant_store.hybrid_search(
        query, tenant_filter=tenant_filter, rerank_results=False
    )


async def _search_docs_impl(
    query: str, topic: Topic | None, ctx: SecurityCtx, doc_ids: list[str] | None = None
) -> str:
    hits = await _document_hits(ctx, query, topic, doc_ids)
    if not hits:
        return "No relevant documents found."
    return _format_cited_context(hits)


@tool(args_schema=SearchDocsArgs)
async def search_docs(
    query: str,
    config: RunnableConfig,
    topic: Topic | None = None,
    doc_ids: list[str] | None = None,
) -> str:
    """Search the knowledge base for company facts — hours, policies,
    procedures, product/service information, and similar. Use this for
    ANY general company-facts question, including one whose wording
    happens to overlap with a department name (e.g. "support hours" is a
    knowledge-base fact about business hours, not a staff lookup, even
    though "Support" is also a department query_employees can filter by).
    Only use query_employees instead when the question is actually asking
    WHO works somewhere — a specific person, a roster, or headcount."""
    ctx = _ctx_or_refuse(config, "search")
    if ctx is None:
        return _NO_CTX_REFUSAL
    return await _arun_with_timeout(_search_docs_impl, query, topic, ctx, doc_ids)


async def _calculator_impl(expression: str) -> str:
    try:
        return str(_safe_eval(ast.parse(expression, mode="eval").body))
    except Exception:  # noqa: BLE001
        return f"Could not evaluate: {expression!r}"


@tool(args_schema=CalculatorArgs)
async def calculator(expression: str) -> str:
    """Evaluate a basic arithmetic expression, e.g. '21 * 2'."""
    return await _arun_with_timeout(_calculator_impl, expression)


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


async def _ask_clarification_impl(question: str, options: list[str]) -> str:
    numbered = "\n".join(f"{i + 1}. {opt}" for i, opt in enumerate(options))
    return (
        f"{question}\n{numbered}\n\n"
        "(Or describe what you meant in your own words.)"
    )


@tool(args_schema=AskClarificationArgs)
async def ask_clarification(question: str, options: list[str]) -> str:
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
    return await _arun_with_timeout(_ask_clarification_impl, question, options)


class AddNoteArgs(BaseModel):
    title: str = Field(..., max_length=_MAX_NOTE_TITLE_CHARS, description="Short title for the note.")
    content: str = Field(..., max_length=_MAX_NOTE_CONTENT_CHARS, description="The note's text.")
    topic: Topic = Field(
        ..., description="Must be one of: langgraph, qdrant, company."
    )

    @field_validator("title", "content")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must not be empty")
        return v


async def _add_note_impl(title: str, content: str, topic: Topic, ctx: SecurityCtx) -> str:
    """Embed and upsert one new point into the knowledge base.

    Fixed, single-purpose write: the only variables are the three typed
    fields above (plus `ctx`, never model-visible). A fresh UUID id (never
    caller-supplied) means this can only ever *add* a point, never overwrite
    or target an existing one by guessing its id.
    """
    text = f"{title}: {content}"
    point = qdrant_store.build_point(
        point_id=str(uuid.uuid4()),
        dense_vector=await embed_text(text),
        sparse_vector=await _sparse_vector_or_none(text),
        payload={
            "text": text,
            "topic": topic.value,
            "title": title,
            "kind": "document",
            "tenant": ctx["tenant"],
        },
    )
    await qdrant_store.upsert([point])
    return f"Note '{title}' added to the {topic.value} knowledge base."


@tool(args_schema=AddNoteArgs)
async def add_note(
    title: str, content: str, topic: Topic, config: RunnableConfig
) -> str:
    """Add a new note to the knowledge base so future searches can find it.

    This WRITES to the knowledge base — unlike search_docs/calculator, this
    changes what other users and future turns will see. It is declared
    "mutating" in TOOL_CAPABILITIES, which means app/agent/graph.py's
    should_continue routes it through human_approval every time, regardless
    of whether the caller opted into require_approval.
    """
    ctx = _ctx_or_refuse(config, "write_note")
    if ctx is None:
        return _NO_CTX_REFUSAL
    return await _arun_with_timeout(_add_note_impl, title, content, topic, ctx)


class RememberArgs(BaseModel):
    content: str = Field(
        ...,
        max_length=_MAX_MEMORY_CONTENT_CHARS,
        description="The fact to remember about this conversation/user.",
    )

    @field_validator("content")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("content must not be empty")
        return v


async def _remember_impl(content: str, ctx: SecurityCtx) -> str:
    """Embed and upsert one memory, owned by ctx["principal"] within
    ctx["tenant"] — the only place a memory gets written (see module
    docstring's "Cross-session memory" section).

    `created_at` (UTC, ISO 8601) is what `Policy.lower`'s retention filter
    (app/core/security.py, GRAPH_PATTERNS.md pattern 33) and
    `app/agent/memory.py::delete_memories` both read — stamped once, here,
    never caller-supplied.
    """
    point = qdrant_store.build_point(
        point_id=str(uuid.uuid4()),
        dense_vector=await embed_text(content),
        sparse_vector=await _sparse_vector_or_none(content),
        payload={
            "text": content,
            "kind": "memory",
            "tenant": ctx["tenant"],
            "owner": ctx["principal"],
            "created_at": datetime.now(UTC).isoformat(),
        },
    )
    await qdrant_store.upsert([point])
    return "Remembered."


@tool(args_schema=RememberArgs)
async def remember(content: str, config: RunnableConfig) -> str:
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
    return await _arun_with_timeout(_remember_impl, content, ctx)


async def recall_memories(ctx: SecurityCtx | None, query: str) -> str:
    """Fetch this principal's own memories, re-filtered against CURRENT ctx
    every call — same `_memory_hits` machinery `gather_context` calls for
    the automatic pre-fetch path; this function has no production caller
    today, only test coverage. Never called on the model's own initiative.
    Re-filtering every call (rather than trusting a cached result) means a
    clearance change takes effect on the very next turn.

    Returns "" on missing ctx or no results — enrichment, never fails the
    turn.
    """
    if not valid_ctx(ctx) or not DEFAULT_POLICY.permit("recall_memory", ctx):
        return ""
    hits = await _memory_hits(ctx, query)
    if not hits:
        return ""
    return _format_cited_context(hits)


async def gather_context(ctx: SecurityCtx | None, query: str) -> tuple[str, list[dict]]:
    """Documents + this principal's memories, hybrid-searched and combined
    into ONE continuously-numbered citation sequence — called by
    app/agent/graph.py's `_default_search` (the automatic pre-fetch every
    turn), not the same call recall_memories/_search_docs_impl make on
    their own, though all three share the same retrieval/Policy machinery.

    Degrades to `("", [])` on missing ctx or a retrieval failure —
    enrichment, never fails the turn (see retrieve_context's try/except in
    app/agent/graph.py).

    Runs the doc/memory searches sequentially, not via `asyncio.gather` —
    they could run concurrently, but that's a separate optimization not
    bundled into this async conversion.
    """
    if not valid_ctx(ctx):
        return "", []
    doc_hits = await _document_hits(ctx, query) if DEFAULT_POLICY.permit("search", ctx) else []
    memory_hits = (
        await _memory_hits(ctx, query) if DEFAULT_POLICY.permit("recall_memory", ctx) else []
    )
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


async def _query_employees_impl(
    department: Department | None, name_contains: str | None, ctx: SecurityCtx
) -> str:
    from app.agent import sql_store

    cap = TOOL_RESULT_CAPS["query_employees"]
    rows = await sql_store.query_employees(
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
        # Marked, never silently shortened — see TOOL_RESULT_CAPS's comment.
        lines.append(f"[truncated: showing the first {cap} matches; more exist]")
    return "\n".join(lines)


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
    ctx = _ctx_or_refuse(config, "query_structured_data")
    if ctx is None:
        return _NO_CTX_REFUSAL
    return await _arun_with_timeout(_query_employees_impl, department, name_contains, ctx)


class SkillSearchArgs(BaseModel):
    query: str = Field(
        ...,
        description="Natural-language description of the task you're trying to do "
        "(not a skill name) — e.g. 'write an onboarding brief for a new hire'.",
    )


class UseSkillArgs(BaseModel):
    name: str = Field(
        ..., description="The exact skill name, as returned by skill_search."
    )


def _format_skill_hits(hits: list) -> str:
    lines = [
        f"- {(h.payload or {}).get('name')}: {(h.payload or {}).get('description')}"
        for h in hits
    ]
    return "\n".join(lines)


def _skill_visible_to_domain(record: "skills_module.SkillRecord", domain: str) -> bool:
    """A skill with no `domains:` frontmatter (`record.domains is None`) is
    visible everywhere, the default before this field existed. A tagged one
    is visible only to the domains it names — INCLUDING Ecorp."""
    return record.domains is None or domain in record.domains


_SKILL_SEARCH_FETCH_K = max(SKILLS_SEARCH_TOP_K * 4, 10)  # over-fetch before
# filtering by domain, then truncate back to SKILLS_SEARCH_TOP_K — see
# make_skill_tools's own docstring for why.


def _filter_skill_hits_by_domain(hits: list, domain: str, catalog: dict) -> list:
    """Keeps only hits visible to `domain`. A hit whose `name` isn't in
    `catalog` (a stale Qdrant entry for a since-removed/renamed SKILL.md) is
    dropped too — disk stays authoritative."""
    kept = []
    for h in hits:
        name = (h.payload or {}).get("name")
        record = catalog.get(name)
        if record is not None and _skill_visible_to_domain(record, domain):
            kept.append(h)
    return kept


def make_skill_tools(domain: str) -> tuple[BaseTool, BaseTool]:
    """Builds a `(skill_search, use_skill)` pair scoped to `domain`. Ecorp's
    own pair below is `make_skill_tools("ecorp")`; each of
    app/domains/support|sales|ops/domain.py builds its own instead of reusing
    Ecorp's tool objects, so a domain-tagged skill (`domains: [...]`,
    app/agent/skills.py) never leaks into a domain it wasn't written for.
    Enforced in BOTH tools: use_skill loads by exact name with no search
    step, so it applies the same filter itself in case a model somehow
    guesses a foreign-domain skill's name.

    Filters in PYTHON over `skills_module.get_skills()`'s disk-backed
    catalog, not as a Qdrant payload filter — domain eligibility is a fact
    about the SKILL.md on disk, and duplicating it into Qdrant would be a
    second place to drift out of sync. Over-fetches (`_SKILL_SEARCH_FETCH_K`)
    before filtering, then truncates to `SKILLS_SEARCH_TOP_K`, so results
    stay correct even when top semantic matches fall outside `domain` —
    cheap, since this app's bundled catalog is demo-scale.
    """

    async def _skill_search_impl(query: str) -> str:
        try:
            hits = await qdrant_store.hybrid_search(
                query, collection=SKILLS_COLLECTION, k=_SKILL_SEARCH_FETCH_K
            )
        except Exception as exc:  # noqa: BLE001 - skills collection may not exist yet
            # hybrid_search's own degrade layers only cover a missing SPARSE leg
            # or failed RERANK, not a wholly missing collection — so a fresh env
            # (before `make index-skills`) gets an actionable message here instead
            # of a raw Qdrant 404.
            logger.warning(
                "skill search unavailable", extra={"error_class": type(exc).__name__}
            )
            return "No skills catalog is available right now (has `make index-skills` been run?)."
        visible = _filter_skill_hits_by_domain(hits, domain, skills_module.get_skills())
        if not visible:
            return "No matching skills found. Proceed using your other tools directly."
        return _format_skill_hits(visible[:SKILLS_SEARCH_TOP_K])

    @tool(args_schema=SkillSearchArgs)
    async def skill_search(query: str) -> str:
        """Search the catalog of available skills — packaged, multi-step
        instructions for specific kinds of tasks (e.g. producing a particular
        report format). Returns candidate skill names and descriptions; call
        use_skill with the best match's exact name to load its full
        instructions before proceeding. If nothing matches well, just proceed
        with your other tools directly — not every task has a packaged skill."""
        return await _arun_with_timeout(_skill_search_impl, query)

    async def _use_skill_impl(name: str) -> str:
        record = skills_module.get_skills().get(name)
        if record is None or not _skill_visible_to_domain(record, domain):
            return (
                f"No skill named {name!r} found. Call skill_search first to find "
                "the exact name of an available skill."
            )
        body = record.body
        if "run_command_in_sandbox" in body:
            # Proactive nudge, not a replacement for the reactive check —
            # app/agent/graph.py::_skipped_required_sandbox_after_skill still
            # catches this after the fact if the model ignores it too (real
            # bug: even a skill body that explicitly says "don't estimate
            # this by hand" got ignored). Placed at the END so it survives a
            # skill author who forgets to write one themselves.
            body += (
                "\n\n---\nReminder: call run_command_in_sandbox now, with a real "
                "script, for the actual computation this skill describes — do not "
                "compute the result yourself."
            )
        return body

    @tool(args_schema=UseSkillArgs)
    async def use_skill(name: str) -> str:
        """Load one skill's full instructions by its exact name (from
        skill_search's results). Follow the returned instructions using your
        other tools to complete the task."""
        return await _arun_with_timeout(_use_skill_impl, name)

    return skill_search, use_skill


skill_search, use_skill = make_skill_tools("ecorp")


def skill_tools_first(
    action_tools: Sequence[BaseTool],
    reused_tools: Sequence[BaseTool],
    run_subagent: BaseTool | None = None,
) -> list[BaseTool]:
    """Orders a domain's bound tool list with skill_search/use_skill FIRST,
    ahead of every action tool — a live-verified fix, not a guess.

    Every domain used to build `action_tools + reused_tools` (skill_search/
    use_skill near the end). A sales deal-math question never called
    skill_search across many runs, even after three escalating prompt/
    docstring rewrites. The actual cause was list POSITION, not wording:
    swapping only the order made this app's local model (qwen2.5:3b via
    Ollama, grammar-constrained tool-calling) call use_skill('deal-economics')
    as its first move, 3/3 fresh runs, zero prompt changes. A small,
    grammar-constrained model's tool selection is sensitive to bound-list
    position, not just description — worth trying before assuming a "won't
    call X" pattern is an instruction-following ceiling.

    Only skill_search/use_skill move to the front; the rest of
    `reused_tools` (search_docs, ask_clarification) keep their position
    after the domain's action tools — search_docs doesn't share this
    problem, and a separate finding needed to REDUCE its reflexive default
    use, so promoting it too would fight that fix.

    `run_subagent`, if given, is promoted into the same leading tier (was
    previously appended dead last). Unlike the skill_search fix above, this
    is a reasoned extension of the same principle, NOT independently
    live-verified — worth confirming the same way if this ever gets
    questioned."""
    by_name = {t.name: t for t in reused_tools}
    promoted = [by_name[name] for name in ("skill_search", "use_skill") if name in by_name]
    if run_subagent is not None:
        promoted.append(run_subagent)
    rest = [t for t in reused_tools if t.name not in ("skill_search", "use_skill")]
    return promoted + list(action_tools) + rest


TOOLS = [
    # skill_search/use_skill lead intentionally — see skill_tools_first's
    # docstring (list position affects tool selection for the small local
    # model). Done inline here since there's no separate "reused tools" list.
    skill_search,
    use_skill,
    search_docs,
    calculator,
    add_note,
    remember,
    query_employees,
    ask_clarification,
]

# --- Tool capability declarations -------------------------------------------
# Each tool declares its exposure "leg": read_only (safe immediately),
# mutating (writes persisted state), or outward (reaches outside the
# corpus). app/agent/graph_routing.py's should_continue routes ANY
# non-read_only tool call through human_approval unconditionally (mandatory,
# not the opt-in require_approval gate) — a RAG agent's context is untrusted
# on essentially every turn (GRAPH_PATTERNS.md pattern 12), so mutating or
# reaching outward on top of that is never worth gambling unsupervised. A
# tool absent from this mapping defaults to "outward" — fail closed.
ToolCapability = Literal["read_only", "mutating", "outward"]

TOOL_CAPABILITIES: dict[str, ToolCapability] = {
    "search_docs": "read_only",
    "calculator": "read_only",
    "query_employees": "read_only",
    "ask_clarification": "read_only",
    "skill_search": "read_only",
    "use_skill": "read_only",
    "add_note": "mutating",
    "remember": "mutating",
    # Honest, static declaration: run_subagent (GRAPH_PATTERNS.md pattern 46)
    # can only delegate to subagents whose tool subset is entirely read_only
    # (enforced structurally at catalog-build time) — as safe as search_docs.
    "run_subagent": "read_only",
}

# --- Tool result-size declarations -------------------------------------------
# Per-tool cap (query_employees has no inherent row limit) rather than one
# global constant, since the right cap is a property of the tool. A capped
# result is always MARKED as truncated (see _query_employees_impl) — never
# silently shortened.
TOOL_RESULT_CAPS: dict[str, int] = {
    "query_employees": 20,
}

# Deliberate, load-bearing side-effecting import — not an unused import to
# clean up. subagent_tools.py builds the Ecorp-level `run_subagent` tool and
# inserts it into THIS module's `TOOLS` list as part of its own top-level
# execution. Placed at the very end, after TOOLS/TOOL_CAPABILITIES are fully
# defined, so importing `app.agent.tools` from anywhere always triggers this
# too, regardless of import order. No import cycle: subagent_tools.py's own
# top-level imports from this module are already defined by this point.
from app.agent import subagent_tools  # noqa: E402, F401

