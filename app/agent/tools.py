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
- `skill_search`/`use_skill`: progressive disclosure over a bundled catalog
  of `SKILL.md` packages (`app/agent/skills.py`, GRAPH_PATTERNS.md pattern
  45) — `skill_search` hybrid-searches a small Qdrant collection of skill
  name/description metadata, `use_skill` loads one matched skill's full
  instruction body from disk by exact name. Like `calculator`, these are
  bundled app capabilities, not tenant data — no `SecurityCtx` involved.
- `run_subagent`: delegates a self-contained task to a fresh, ISOLATED
  nested agent run — a genuinely separate `graph.invoke()`, unlike
  `use_skill`, which loads more instructions into THIS agent's own context.
  Backed by a bundled catalog of `AGENT.md` packages
  (`app/agent/subagents.py`, GRAPH_PATTERNS.md pattern 46). Every subagent is
  restricted to `read_only` tools (enforced at catalog-build time below, see
  `_resolve_subagent_tools`), so `run_subagent` itself needs no mandatory
  `human_approval` gating — it's exactly as safe as `search_docs`.

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

## Tenant isolation (app/core/security.py)

`search_docs`, `add_note`, and `remember` all receive `config:
RunnableConfig` — a LangChain-standard parameter that's auto-injected by
`ToolNode` and, critically, auto-*excluded* from the schema the LLM sees
(verified: `tool.args` never lists it) — to read `SecurityCtx` from
`config["configurable"]["ctx"]`. Every one of them calls `_ctx_or_refuse`
first and refuses (fails closed) rather than running an unscoped query or
an untenanted write if `ctx` is missing or malformed. This is the same
"config carries what the LLM must never see or set" channel app/agent/graph.py's
nodes already use for `deps` — ctx just travels one level further, into
the tools themselves.

## Cross-session memory

A memory is written *only* by the `remember` tool — nothing in this app
extracts facts from turn text automatically. That's deliberate: whatever
writes memory decides what gets replayed into every future prompt, which
makes autonomous extraction a privileged, unaudited side channel. Recall,
by contrast, is automatic — folded into `_default_search` (app/agent/graph.py)
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
import logging
import operator
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Any, Literal

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, tool
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field, SecretStr, field_validator

from app.agent import skills as skills_module
from app.agent import subagents as subagents_module
from app.core import metrics
from app.core.config import (
    CHAT_MODEL,
    MAX_SUBAGENT_COST_USD_PER_RUN,
    OPENAI_API_BASE,
    OPENAI_API_KEY,
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

# Safety budget: bound how long any single tool call can block the graph
# (a hung Qdrant/embedding call, or a pathological expression like
# `2**99999999999`, would otherwise stall — or in the exponent case,
# potentially exhaust memory in — the whole turn indefinitely).
TOOL_TIMEOUT_SECONDS = 15
_TOOL_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=8, thread_name_prefix="tool-timeout"
)


def _run_with_timeout(func, *args, _timeout_seconds: float | None = None, **kwargs):
    """Run `func` in a worker thread and stop waiting after
    `_timeout_seconds` (TOOL_TIMEOUT_SECONDS by default). The raised
    TimeoutError is caught by ToolNode's `handle_tool_errors=_friendly_tool_error`
    in app/agent/graph.py and turned into a message the agent sees on its next
    turn, same as any other tool exception.

    `_timeout_seconds` is keyword-only with a leading underscore so it can
    never collide with a wrapped function's own keyword argument — every
    existing call site omits it (`None`) and gets TOOL_TIMEOUT_SECONDS,
    resolved fresh on every call rather than baked in as an ordinary default
    value: a plain `= TOOL_TIMEOUT_SECONDS` default is evaluated exactly
    once, at function-DEFINITION time, so it would silently stop honoring
    `monkeypatch.setattr(tools, "TOOL_TIMEOUT_SECONDS", ...)` — a real
    regression this caught against tests/agent/test_safety_budgets.py's
    existing TestToolTimeout, which relies on that global being re-read
    per call. `run_subagent` (GRAPH_PATTERNS.md pattern 46) is the one
    caller that overrides it, to SUBAGENT_TIMEOUT_SECONDS: a nested
    multi-step agent run legitimately needs more wall-clock time than a
    single Qdrant query or arithmetic eval.

    Soft timeout: Python can't forcibly kill the worker thread, so this
    bounds how long the *graph* waits, not how long the call actually runs
    in the background.

    The result is scrubbed (app/core/scrubbing.py, GRAPH_PATTERNS.md pattern
    32) before it reaches the caller — the one chokepoint every read/write
    tool impl in this module funnels through, so a credential-shaped or
    actually-bound-secret value in a tool's raw result (a database row, a
    fetched document) never reaches the model's next prompt or a trace.
    `run_subagent` returns a state dict here (scrubbing skips non-str
    results, same as always) and applies its own explicit scrub() to the
    extracted answer text afterward instead — see its own docstring.
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
    chunk of a larger parent (app/ingestion/chunking.py, app/ingestion/ingestor.py — the small
    child is what was *embedded and matched*, but the model gets the
    richer surrounding passage), falling back to the point's own `text`
    for anything not chunked this way (add_note/remember/pre-chunking
    sample docs, and memories, which are never split into parent/child)."""
    payload = hit.payload or {}
    return payload.get("parent_text") or payload.get("text", "")


def _dedupe_by_parent(hits: list) -> list:
    """Multiple child chunks from the SAME parent (app/ingestion/chunking.py) can
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


def _sparse_vector_or_none(text: str) -> tuple[list[int], list[float]] | None:
    """Best-effort BM25 sparse vector for a write path (add_note, remember):
    if the local sparse model fails to load/embed, the write still
    succeeds with a dense-only point (qdrant_store.build_point already
    treats `sparse_vector=None` as "dense-only") rather than blocking a
    human-approved write on a hybrid-search quality concern — the same
    degrade-don't-block posture app/retrieval/qdrant_store.py's hybrid_search takes
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
    # ANDed on regardless (app/retrieval/qdrant_store.py::_build_filter).
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
    "mutating" in TOOL_CAPABILITIES, which means app/agent/graph.py's
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
    recall range filter (app/core/security.py, GRAPH_PATTERNS.md pattern 33)
    and `app/agent/memory.py::delete_memories`'s age-based selector both read —
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
    ctx on every call — called automatically from app/agent/graph.py's
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
    app/agent/graph.py's `_default_search` calls (the automatic pre-fetch path
    that runs every turn), NOT the same call `recall_memories`/
    `_search_docs_impl` make on their own, though all three share the same
    retrieval/Policy machinery underneath.

    Degrades to `("", [])` on a missing ctx or a retrieval failure —
    enrichment, never fails the turn (see retrieve_context's docstring in
    app/agent/graph.py, which wraps this in the actual try/except).
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
    from app.agent import sql_store

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
    visible everywhere — the default every SKILL.md had before this field
    existed. A tagged one is visible only to the domains it names, INCLUDING
    Acme: `domains: [support]` hides a skill from Acme's own catalog too,
    not just from the other two example domains."""
    return record.domains is None or domain in record.domains


_SKILL_SEARCH_FETCH_K = max(SKILLS_SEARCH_TOP_K * 4, 10)  # over-fetch before
# filtering by domain, then truncate back to SKILLS_SEARCH_TOP_K — see
# make_skill_tools's own docstring for why.


def _filter_skill_hits_by_domain(hits: list, domain: str, catalog: dict) -> list:
    """Keeps only hits visible to `domain`. A hit whose `name` isn't in
    `catalog` at all (a stale Qdrant entry for a SKILL.md since removed or
    renamed on disk) is dropped too — disk stays authoritative, same
    posture app/agent/skills.py's own docstring already establishes for a
    skill's body."""
    kept = []
    for h in hits:
        name = (h.payload or {}).get("name")
        record = catalog.get(name)
        if record is not None and _skill_visible_to_domain(record, domain):
            kept.append(h)
    return kept


def make_skill_tools(domain: str) -> tuple[BaseTool, BaseTool]:
    """Builds a `(skill_search, use_skill)` pair scoped to `domain`. Acme's
    own module-level pair below is `make_skill_tools("acme")`; each of
    app/domains/support|sales|ops/domain.py builds its own via this same
    factory instead of reusing Acme's literal tool objects, so a
    domain-tagged skill (`domains: [...]` in its SKILL.md frontmatter,
    app/agent/skills.py) never leaks into a domain it wasn't written for —
    enforced in BOTH tools here, not just skill_search's results: use_skill
    loads by exact name with no search step, so it has to apply the same
    filter itself or a model that somehow guessed/hallucinated a
    foreign-domain skill's name could load it anyway.

    The filter runs in PYTHON, over app/agent/skills.py::get_skills()'s own
    disk-backed catalog — not as a Qdrant payload filter — for the same
    reason that module keeps a skill's full body off Qdrant entirely:
    domain eligibility is a fact about the SKILL.md on disk, and duplicating
    it into the Qdrant payload just to filter there would be a second place
    it could drift out of sync with what's actually on disk. To keep
    results correct even when several of Qdrant's top semantic matches fall
    outside `domain`, this over-fetches (`_SKILL_SEARCH_FETCH_K`) before
    filtering, then truncates back to `SKILLS_SEARCH_TOP_K` — cheap, since
    this app's own bundled catalog is demo-scale, never more than a
    handful of skills.
    """

    def _skill_search_impl(query: str) -> str:
        try:
            hits = qdrant_store.hybrid_search(
                query, collection=SKILLS_COLLECTION, k=_SKILL_SEARCH_FETCH_K
            )
        except Exception as exc:  # noqa: BLE001 - the skills collection may not exist
            # yet (before `make index-skills` has ever run) — hybrid_search's own
            # two degrade layers (app/retrieval/qdrant_store.py) only cover a missing
            # SPARSE leg or a failed RERANK, not a wholly missing collection, so a
            # fresh environment gets a clear, actionable message here instead of a
            # raw Qdrant 404 bubbling up as a generic tool error.
            logger.warning(
                "skill search unavailable", extra={"error_class": type(exc).__name__}
            )
            return "No skills catalog is available right now (has `make index-skills` been run?)."
        visible = _filter_skill_hits_by_domain(hits, domain, skills_module.get_skills())
        if not visible:
            return "No matching skills found. Proceed using your other tools directly."
        return _format_skill_hits(visible[:SKILLS_SEARCH_TOP_K])

    @tool(args_schema=SkillSearchArgs)
    def skill_search(query: str) -> str:
        """Search the catalog of available skills — packaged, multi-step
        instructions for specific kinds of tasks (e.g. producing a particular
        report format). Returns candidate skill names and descriptions; call
        use_skill with the best match's exact name to load its full
        instructions before proceeding. If nothing matches well, just proceed
        with your other tools directly — not every task has a packaged skill."""
        return _run_with_timeout(_skill_search_impl, query)

    def _use_skill_impl(name: str) -> str:
        record = skills_module.get_skills().get(name)
        if record is None or not _skill_visible_to_domain(record, domain):
            return (
                f"No skill named {name!r} found. Call skill_search first to find "
                "the exact name of an available skill."
            )
        return record.body

    @tool(args_schema=UseSkillArgs)
    def use_skill(name: str) -> str:
        """Load one skill's full instructions by its exact name (from
        skill_search's results). Follow the returned instructions using your
        other tools to complete the task."""
        return _run_with_timeout(_use_skill_impl, name)

    return skill_search, use_skill


skill_search, use_skill = make_skill_tools("acme")


TOOLS = [
    search_docs,
    calculator,
    add_note,
    remember,
    query_employees,
    ask_clarification,
    skill_search,
    use_skill,
]

# --- Tool capability declarations -------------------------------------------
# Every tool declares which "leg" of exposure it adds: read_only (safe to run
# immediately), mutating (writes/changes persisted state), or outward
# (reaches outside the corpus — sends, calls an external service, etc.).
# app/agent/graph.py's should_continue enforces this: a tool_call batch containing
# ANY non-read_only tool is routed through human_approval unconditionally —
# "mandatory," not the opt-in require_approval gate — because a retrieval-
# augmented agent's context is untrusted content on essentially every turn
# (see GRAPH_PATTERNS.md pattern 12): once a run already carries "exposure to
# untrusted content," adding "ability to mutate state" is the second of the
# two legs a run may hold unsupervised, and the access-to-private-data leg
# (app/core/security.py's tenant/owner isolation) is never worth gambling the
# third on too. A tool absent from this mapping defaults to "outward" — fail
# closed, so a new tool added to TOOLS without a capability entry is gated
# rather than silently trusted.
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
    # A plain, static, honest declaration — NOT a fallback subject to some
    # dynamic per-call override. run_subagent (GRAPH_PATTERNS.md pattern 46)
    # can only ever delegate to a subagent whose OWN tool subset is entirely
    # read_only — enforced structurally at catalog-build time, below, not
    # computed per call — so should_continue's mandatory human_approval gate
    # needs zero special-casing for it: a run_subagent call really is exactly
    # as safe as calling search_docs directly.
    "run_subagent": "read_only",
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

# --- Subagents (app/agent/subagents.py, GRAPH_PATTERNS.md pattern 46) -------
# run_subagent delegates a bounded, self-contained task to a fresh, ISOLATED
# nested agent run (a genuinely separate graph.invoke(), reusing the exact
# same build_graph() topology this app's own top-level agent runs on) — not
# more instructions loaded into THIS agent's own context, which is what
# skill_search/use_skill do instead. Every piece of domain-specific
# validation below (which declared tools are safe to hand a subagent, the
# recursion block) lives HERE, in the module that already owns
# TOOL_CAPABILITIES — app/agent/subagents.py stays a pure, domain-agnostic
# disk parser, same scope as app/agent/skills.py, so there's no import-order
# coupling between the two modules.
SUBAGENT_TIMEOUT_SECONDS = 45  # safety budget: wall-clock cap on one nested
# subagent run — same shared-worker-pool "soft timeout" mechanism as
# TOOL_TIMEOUT_SECONDS (_run_with_timeout's `_timeout_seconds` override),
# just longer, since a nested multi-step agent loop legitimately needs more
# time than a single Qdrant query or arithmetic eval.


def _resolve_subagent_tools(
    subagent_name: str,
    declared_tools: tuple[str, ...] | None,
    all_tool_names: frozenset[str],
    tool_capabilities: Mapping[str, str],
) -> tuple[str, ...]:
    """A subagent's effective, SAFE tool subset — a pure function, unit-tested
    directly (see tests/agent/test_tools.py).

    `declared_tools` is `None` when an AGENT.md omits `tools:` entirely,
    meaning "every read_only tool the domain exposes" — mirrors
    `AgentManifest.allowed_tools`'s existing "empty means everything"
    convention (app/agent/manifest.py), narrowed here to read_only-only
    (see GRAPH_PATTERNS.md pattern 46 for why v1 subagents are read_only-only
    at all). Any declared name that doesn't exist, or exists but isn't
    read_only, is DROPPED with a warning — never silently trusted or
    upgraded, the same fail-toward-caution posture `_tool_capability`'s
    "undeclared defaults to outward" already takes, applied at a different
    point. A subagent left with an empty resolved set is still valid — it
    can reason/answer from general knowledge, same as the main agent can
    with zero relevant tools for a given question.

    `run_subagent` itself is ALWAYS stripped, unconditionally, regardless of
    what an AGENT.md's frontmatter says — the actual, structural recursion
    block. Read_only-ness alone would NOT exclude it (it's declared
    read_only itself, by design: a subagent invocation carries no more
    exposure than any other read_only tool call), so this has to be an
    explicit, separate check.
    """
    if declared_tools is not None:
        candidates = declared_tools
    else:
        candidates = tuple(
            name for name in all_tool_names if tool_capabilities.get(name, "outward") == "read_only"
        )
    resolved: list[str] = []
    for name in candidates:
        if name == "run_subagent":
            continue
        if name not in all_tool_names:
            logger.warning(
                "subagent declares an unknown tool; dropping it",
                extra={"subagent_name": subagent_name, "tool_name": name},
            )
            continue
        if tool_capabilities.get(name, "outward") != "read_only":
            logger.warning(
                "subagent declares a non-read_only tool; dropping it",
                extra={"subagent_name": subagent_name, "tool_name": name},
            )
            continue
        resolved.append(name)
    return tuple(resolved)


_ALL_TOOL_NAMES = frozenset(t.name for t in TOOLS)  # computed before run_subagent
# itself might be appended below — deliberately: run_subagent is never a
# candidate tool for another subagent regardless (see the explicit strip
# above), so this ordering doesn't matter for correctness, just clarity.


def _subagent_declared_for_domain(record: "subagents_module.SubagentRecord", domain: str) -> bool:
    """An AGENT.md with no `domains:` frontmatter (`record.domains is None`)
    stays exactly where every subagent has always lived — visible only to
    `domain="acme"` — rather than silently becoming available to every new
    domain this app ever grows. See app/agent/subagents.py's own docstring
    for why that default differs from app/agent/skills.py's SkillRecord
    (there, `None` means "every domain"): a subagent's declared `tools:`
    are only ever meaningful against ONE specific tool universe, so an
    untagged subagent handed to a domain its tools were never written for
    would typically just resolve to nothing useful anyway."""
    if record.domains is None:
        return domain == "acme"
    return domain in record.domains


def _build_subagent_registry(
    all_tool_names: frozenset[str], tool_capabilities: Mapping[str, str], *, domain: str
) -> dict[str, tuple["subagents_module.SubagentRecord", tuple[str, ...]]]:
    """Every bundled AGENT.md declared for `domain` (see
    `_subagent_declared_for_domain`), each resolved to its safe, read_only
    tool subset within `all_tool_names`/`tool_capabilities` — that pair is
    itself domain-specific (a nested subagent run can only ever call tools
    that exist, and are read_only, WITHIN the calling domain's own tool
    universe, not Acme's)."""
    registry = {}
    for record in subagents_module.get_subagents().values():
        if not _subagent_declared_for_domain(record, domain):
            continue
        resolved = _resolve_subagent_tools(record.name, record.tools, all_tool_names, tool_capabilities)
        registry[record.name] = (record, resolved)
    return registry


_SUBAGENT_REGISTRY = _build_subagent_registry(_ALL_TOOL_NAMES, TOOL_CAPABILITIES, domain="acme")

_CITATION_MARKER_WARNING = (
    "Do not use '[n]'-style bracket citation markers in your final answer — "
    "that citation convention is reserved for the orchestrating agent's own "
    "retrieved context, not yours."
)
# ^ Necessary, not decorative: the PARENT's check_output/_ungrounded_claims_count
# (GRAPH_PATTERNS.md pattern 39) cross-checks [n] markers in the final answer
# against state["citations"], which a subagent's own internal retrieval never
# populates on the parent. Without this instruction, a subagent's own
# genuinely-grounded answer could get flagged as an "ungrounded claim" once
# folded into the parent's reply.


@dataclass
class _SubagentDomainPlugin:
    """A throwaway DomainPlugin scoping a nested subagent run to EXACTLY its
    pre-resolved, pre-validated (read_only-only) tool subset.

    Used instead of `AgentManifest.allowed_tools` specifically because that
    field treats an EMPTY tuple as "no filter — expose everything the domain
    offers" (see app/agent/manifest.py's `AgentManifest`/`build_graph`
    docstrings: `if manifest.allowed_tools:` is falsy-skipped for an empty
    tuple). A subagent legitimately left with zero usable tools (e.g. every
    declared tool got dropped by `_resolve_subagent_tools`) must actually
    run with zero tools, not silently fall back to the full Acme tool set —
    including `add_note`/`remember` — which would be exactly the kind of
    privilege-escalation-by-omission bug pattern 17's fail-closed discipline
    exists to rule out. Passing the already-narrowed tool list as this
    plugin's `tools()` sidesteps the empty-tuple ambiguity entirely: an
    empty `domain.tools()` is unambiguous, no special-casing anywhere in
    `build_graph()` interprets it as "everything."

    `tool_capabilities()` reports every one of `_tools` as `read_only` —
    NOT a lookup into the caller's own `TOOL_CAPABILITIES` dict, which for
    a domain-specific subagent (e.g. one resolved from a support-domain
    registry) may not even contain that tool's name at all. That gap
    matters: `_tool_capability()` (app/agent/graph.py) defaults an
    UNDECLARED name to `"outward"` — fail-closed and correct for the
    top-level graph, where a human is present to approve a pause, but
    silently wrong here, where a nested one-shot `graph.invoke()` has no
    resume path and would just look like the subagent "failed" for no
    visible reason. Restating read_only-ness locally is correct BY
    CONSTRUCTION, not a guess: every name in `_tools` already passed
    `_resolve_subagent_tools`'s own read_only-only filter to get here.
    """

    _tools: list

    def tools(self) -> list:
        return list(self._tools)

    def tool_capabilities(self) -> dict[str, str]:
        return {t.name: "read_only" for t in self._tools}

    def policy(self):
        return DEFAULT_POLICY


def _run_subagent_impl(
    subagent_name: str,
    task: str,
    config: RunnableConfig,
    registry: dict[str, tuple] | None = None,
    tools_by_name: Mapping[str, Any] | None = None,
    llm: Any = None,
) -> str:
    """Build and run one nested, isolated agent turn, then return its final
    answer text. See GRAPH_PATTERNS.md pattern 46 for the full design;
    `registry`/`tools_by_name`/`llm` are DI for tests (mirror
    `build_graph(deps=...)`'s own override shape) — `registry` defaults to
    the real, process-wide `_SUBAGENT_REGISTRY`, `tools_by_name` defaults to
    every Acme tool by name (`{t.name: t for t in TOOLS}`), `llm` defaults
    to `None`, meaning "construct a real ChatOpenAI client for this
    subagent's own model alias." A test passing a fake chat model here
    bypasses that construction entirely, the same way `GraphDeps(llm=fake)`
    already bypasses `_make_llm` at the top level — real tools.py tools
    have no other network-touching construction to fake.

    `tools_by_name` matters for the same reason `registry` itself does: a
    domain-scoped subagent's `registry` entry can resolve to a tool name
    like `check_ticket_status` that simply isn't IN Acme's own `TOOLS` —
    looking it up there would silently drop it. `make_domain_subagent_tool`
    passes the calling domain's own tool objects here; the Acme-level
    construction below relies on the default, since Acme's own registry
    only ever resolves to names already in Acme's own `TOOLS`.

    Isolation, in one place: a FRESH `messages` list (the subagent's own
    system prompt + exactly the delegated `task` as its sole HumanMessage —
    NOT this conversation's history); the subagent's OWN tool subset and
    model alias; SecurityCtx INHERITED unconditionally from `config`, never
    re-derived; its own, smaller, fixed budget ceiling
    (MAX_SUBAGENT_ITERATIONS/MAX_SUBAGENT_TOKENS_PER_RUN/
    MAX_SUBAGENT_COST_USD_PER_RUN); a throwaway MemorySaver, never the
    durable checkpointer — this run is bounded to complete within this one
    tool call, never independently resumable later.

    Reuses build_graph()'s full, unmodified topology rather than a stripped-
    down bespoke pipeline — "one pipeline, not two that can drift" (same
    reasoning GRAPH_PATTERNS.md pattern 24 already applies to ingestion). One
    disclosed side effect: the nested run also pays for a suggest_followups
    call (its result is discarded here) and checks/writes the same semantic
    cache the top-level conversation uses, keyed by tenant+principal (shared
    with the parent's own ctx) — a subagent's cached answer could in theory
    be served back for a top-level query with near-identical phrasing, or
    vice versa. Narrow and non-security-relevant (still scoped to the same
    tenant+principal), not fixed here — a real gap, named rather than hidden.
    """
    ctx = _ctx_from_config(config)
    if not valid_ctx(ctx):
        return _NO_CTX_REFUSAL

    reg = registry if registry is not None else _SUBAGENT_REGISTRY
    entry = reg.get(subagent_name)
    if entry is None:
        return f"No subagent named {subagent_name!r} is registered."
    record, resolved_tool_names = entry
    all_tools_by_name = tools_by_name if tools_by_name is not None else {t.name: t for t in TOOLS}
    nested_tools = [all_tools_by_name[name] for name in resolved_tool_names if name in all_tools_by_name]

    # Deferred imports: app/agent/graph.py imports THIS module
    # (app/agent/tools.py) at its own module level for TOOL_CAPABILITIES/
    # TOOLS, and app/agent/manifest.py imports app/agent/graph.py at ITS
    # module level too — importing either back at tools.py's own module
    # level would close a real import cycle. Both are only ever needed here,
    # at call time, long after every module has finished loading — same
    # deferred-import fix app/agent/manifest.py's own docstring documents
    # for its reverse-direction version of this problem.
    from app.agent.graph import (
        MAX_SUBAGENT_ITERATIONS,
        MAX_SUBAGENT_TOKENS_PER_RUN,
        GraphDeps,
        build_graph,
    )
    from app.agent.manifest import AgentManifest
    from app.agent.meter import record_usage

    nested_llm = llm if llm is not None else ChatOpenAI(
        model=record.model or CHAT_MODEL,
        base_url=OPENAI_API_BASE,
        api_key=SecretStr(OPENAI_API_KEY),
        temperature=0,
        stream_usage=True,
    ).bind_tools(nested_tools)

    nested_system_prompt = f"{record.system_prompt}\n\n{_CITATION_MARKER_WARNING}"
    nested_manifest = AgentManifest(name=record.name, system_prompt=nested_system_prompt)
    nested_domain = _SubagentDomainPlugin(nested_tools)

    nested_graph = build_graph(
        deps=GraphDeps(llm=nested_llm),
        manifest=nested_manifest,
        domain=nested_domain,
        max_iterations=MAX_SUBAGENT_ITERATIONS,
        max_tokens_per_turn=MAX_SUBAGENT_TOKENS_PER_RUN,
        max_cost_usd_per_turn=MAX_SUBAGENT_COST_USD_PER_RUN,
    )

    parent_thread_id = (config or {}).get("configurable", {}).get("thread_id", "unknown")
    nested_thread_id = f"{parent_thread_id}:subagent:{record.name}:{uuid.uuid4().hex[:8]}"
    nested_config = {
        "configurable": {"thread_id": nested_thread_id, "ctx": ctx},
        # LangGraph's OWN graph-step cap — a different, coarser unit than
        # MAX_SUBAGENT_ITERATIONS (an agent-node-invocation count): every
        # turn also runs ~5 fixed pre-loop nodes (validate_input,
        # compact_history, moderate_input, check_semantic_cache,
        # retrieve_context) plus ~2 more post-loop (check_output,
        # write_semantic_cache), and each agent<->tools round trip is 2
        # steps — so naively reusing app/agent/runtime.py's own flat "12"
        # (sized for ITS context) undercounts here and trips
        # GraphRecursionError before MAX_SUBAGENT_ITERATIONS ever does,
        # verified empirically via tests/agent/test_tools.py's
        # test_respects_its_own_smaller_iteration_ceiling_not_the_parents.
        # Derived from MAX_SUBAGENT_ITERATIONS, with real margin, so it can
        # never silently fall out of sync if that constant changes later.
        "recursion_limit": MAX_SUBAGENT_ITERATIONS * 2 + 15,
    }

    def _invoke():
        # The base system prompt isn't auto-seeded the way
        # app/agent/runtime.py::_ensure_seeded_async seeds it for a durable,
        # multi-turn thread — that machinery exists to avoid RE-seeding on
        # every subsequent turn, which doesn't apply here: this graph is
        # invoked exactly once, so the system prompt is just the first
        # message in this one-shot call.
        return nested_graph.invoke(
            {
                "messages": [
                    SystemMessage(content=nested_system_prompt),
                    HumanMessage(content=task),
                ],
                "require_approval": False,
            },
            config=nested_config,
        )

    started = time.monotonic()
    try:
        result_state = _run_with_timeout(_invoke, _timeout_seconds=SUBAGENT_TIMEOUT_SECONDS)
    except TimeoutError:
        metrics.agent_subagent_run_total.labels(subagent=record.name, outcome="timeout").inc()
        metrics.agent_subagent_duration_seconds.labels(subagent=record.name).observe(
            time.monotonic() - started
        )
        raise
    except Exception:
        metrics.agent_subagent_run_total.labels(subagent=record.name, outcome="error").inc()
        metrics.agent_subagent_duration_seconds.labels(subagent=record.name).observe(
            time.monotonic() - started
        )
        raise
    duration = time.monotonic() - started

    total_tokens = result_state.get("total_tokens", 0)
    messages = result_state.get("messages", [])
    final_ai = next((m for m in reversed(messages) if isinstance(m, AIMessage)), None)
    content = final_ai.content if final_ai is not None else ""
    if not isinstance(content, str):
        content = str(content)
    content = content.strip()
    # A missing/empty final answer means should_continue's "__end__" fired
    # from one of its OWN several safety-net checks (max iterations, max
    # tokens, max cost, or no-progress/repeated-action detection — pattern
    # 10's "layered budgets," several distinct routes, all ending the same
    # way) before the nested run ever reached check_output. Rather than
    # re-deriving WHICH specific budget tripped (fragile — should_continue
    # already has four such paths and could grow more), treat "no answer" as
    # the one robust, catch-all signal: this run got safety-net-terminated.
    if content:
        answer = scrub(content)
        outcome = "completed"
    else:
        answer = (
            f"Subagent {record.name!r} did not produce a final answer before "
            "hitting one of its own safety budgets."
        )
        outcome = "budget_exceeded"

    record_usage(ctx, nested_thread_id, record.model or CHAT_MODEL, total_tokens)

    metrics.agent_subagent_run_total.labels(subagent=record.name, outcome=outcome).inc()
    metrics.agent_subagent_duration_seconds.labels(subagent=record.name).observe(duration)
    logger.info(
        "subagent_completed",
        extra={
            "subagent_name": record.name,
            "thread_id": nested_thread_id,
            "outcome": outcome,
            "iterations": result_state.get("iterations", 0),
            "total_tokens": total_tokens,
            "duration_ms": round(duration * 1000, 1),
        },
    )
    return answer


if _SUBAGENT_REGISTRY:
    # A closed, dynamically-built enum — same closed-vocabulary idiom as
    # Topic/Department above — so the LLM sees the full menu of available
    # subagents (name + description) directly in run_subagent's own JSON
    # schema, with zero extra discovery round trip. Unlike skill_search/
    # use_skill, no separate "list" tool or Qdrant index is needed: a
    # subagent's one-line description is small enough to embed directly,
    # where a skill's full instruction BODY is not (that's what
    # progressive disclosure via search buys for skills specifically).
    #
    # Built once, here, at THIS module's import time — not lazily on first
    # call, unlike app/agent/skills.py's get_skills(). A compile-time Pydantic
    # enum has to exist before any tool call can be validated against it, so
    # eager resolution is required, not just a style choice; a subagent
    # added to disk after the process starts needs a restart to appear —
    # same limitation adding a new entry to TOOLS already has today.
    SubagentName = Enum(  # type: ignore[misc]  # mypy can't infer members from a
        # dict comprehension (needs a literal dict/list) — genuinely dynamic
        # by design here, built from whatever's on disk, so there's no
        # literal to give it; see this block's own docstring above.
        "SubagentName", {name: name for name in sorted(_SUBAGENT_REGISTRY)}, type=str
    )

    _SUBAGENT_MENU = "\n".join(
        f"- {name}: {record.description}"
        for name, (record, _tools) in sorted(_SUBAGENT_REGISTRY.items())
    )

    class RunSubagentArgs(BaseModel):
        subagent_name: SubagentName = Field(
            ..., description=f"Which subagent to delegate to. Options:\n{_SUBAGENT_MENU}"
        )
        task: str = Field(
            ...,
            description="The self-contained task to delegate. The subagent has NO "
            "access to this conversation's history, so include everything it needs "
            "to know in this one description.",
        )

        @field_validator("task")
        @classmethod
        def _not_blank(cls, v: str) -> str:
            if not v.strip():
                raise ValueError("task must not be empty")
            return v

    @tool(args_schema=RunSubagentArgs)
    def run_subagent(subagent_name: SubagentName, task: str, config: RunnableConfig) -> str:
        """Delegate a self-contained task to a specialized subagent running in
        its own isolated context — it does NOT see this conversation's
        history, only the `task` description you give it, so describe
        everything it needs to know. Use this to keep a multi-step lookup's
        intermediate steps out of the main conversation, or when a task
        matches a subagent's specific focus better than doing it yourself.
        Every subagent is restricted to read_only tools, so calling this
        never needs human approval."""
        return _run_subagent_impl(subagent_name.value, task, config)

    TOOLS.append(run_subagent)


def make_domain_subagent_tool(
    domain: str, all_tools: list, tool_capabilities: Mapping[str, str]
) -> BaseTool | None:
    """Builds a `run_subagent` tool scoped to `domain` — the domain
    equivalent of the Acme-level construction above (same closed-enum
    menu, same `RunSubagentArgs` shape, same mandatory read_only-only
    resolution via `_resolve_subagent_tools`), but resolved against
    `all_tools`/`tool_capabilities` — the CALLING domain's own tool
    universe, e.g. app/domains/support/domain.py passes its own ticket
    tools plus its own `search_docs`/`skill_search`/`use_skill`/
    `ask_clarification`, not Acme's `TOOLS`/`TOOL_CAPABILITIES` — and
    filtered to only the subagents actually declared for `domain`
    (`domains: [...]` frontmatter, see app/agent/subagents.py's docstring
    for the "untagged means Acme-only" default this applies).

    Returns `None` if that filtered registry ends up empty — a domain with
    no bundled subagent gets no `run_subagent` tool at all, never one
    offering an empty menu: a `SubagentName`-style enum needs at least one
    real member to be a meaningful closed vocabulary, and a tool a caller
    can never usefully invoke is worse than no tool (same "that omission is
    what sandboxed means" posture app/domains/support/domain.py's own
    docstring already takes for tools this app deliberately doesn't expose).

    Each call builds a genuinely NEW, distinct closure (its own Enum class,
    `Args` schema, and `run_subagent` tool object) — never the Acme-level
    `run_subagent` above. Meant to be called ONCE, at each domain module's
    own import time (same "built once, not lazily" reasoning the
    Acme-level block's own comment gives — a subagent added to disk after
    the process starts needs a restart to appear here either way).
    """
    all_tool_names = frozenset(t.name for t in all_tools)
    registry = _build_subagent_registry(all_tool_names, tool_capabilities, domain=domain)
    if not registry:
        return None

    tools_by_name = {t.name: t for t in all_tools}

    # A per-domain Enum TYPE (not just distinct member values) — reusing
    # SubagentName here would mix this domain's menu with Acme's, and two
    # domains both calling this factory would silently share one Enum
    # class between them, wrong the moment their registries diverge.
    domain_subagent_name = Enum(  # type: ignore[misc]
        f"SubagentName_{domain}", {name: name for name in sorted(registry)}, type=str
    )

    menu = "\n".join(
        f"- {name}: {record.description}" for name, (record, _tools) in sorted(registry.items())
    )

    class _RunSubagentArgs(BaseModel):
        subagent_name: domain_subagent_name = Field(  # type: ignore[valid-type]
            ..., description=f"Which subagent to delegate to. Options:\n{menu}"
        )
        task: str = Field(
            ...,
            description="The self-contained task to delegate. The subagent has NO "
            "access to this conversation's history, so include everything it needs "
            "to know in this one description.",
        )

        @field_validator("task")
        @classmethod
        def _not_blank(cls, v: str) -> str:
            if not v.strip():
                raise ValueError("task must not be empty")
            return v

    @tool(args_schema=_RunSubagentArgs)
    def run_subagent(subagent_name: domain_subagent_name, task: str, config: RunnableConfig) -> str:
        """Delegate a self-contained task to a specialized subagent running in
        its own isolated context — it does NOT see this conversation's
        history, only the `task` description you give it, so describe
        everything it needs to know. Use this to keep a multi-step lookup's
        intermediate steps out of the main conversation, or when a task
        matches a subagent's specific focus better than doing it yourself.
        Every subagent is restricted to read_only tools, so calling this
        never needs human approval."""
        return _run_subagent_impl(
            subagent_name.value, task, config, registry=registry, tools_by_name=tools_by_name
        )

    return run_subagent
