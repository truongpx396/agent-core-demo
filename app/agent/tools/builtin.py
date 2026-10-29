"""The tools more than one domain shares, and the retrieval machinery behind them.

- `search_docs`: tenant-scoped retrieval from Qdrant, optional topic filter.
- `calculator`: a second tool so the agent must *choose* which to call.
- `ask_clarification`: an ordinary read-only tool whose result the model relays verbatim (GRAPH_PATTERNS.md
  pattern 27).
- `skill_search`/`use_skill` (`make_skill_tools(domain)`): progressive disclosure over a bundled catalog of
  `SKILL.md` packages (`app/agent/tools/skill_catalog.py`, pattern 45). `skill_search` hybrid-searches a small
  Qdrant collection of skill name/description metadata; `use_skill` loads one matched skill's full body from
  disk by exact name. A bundled app capability, not tenant data: no `SecurityCtx`.
- `gather_context`: the automatic pre-fetch (documents plus this principal's memories, one continuously
  numbered citation sequence).
- `skill_tools_first`: the bound-tool ordering every domain uses (pattern 50).

`run_subagent` (`app/agent/tools/run_subagent_domain.py`) delegates a self-contained task to a fresh, ISOLATED
nested agent run, unlike `use_skill`, which loads more instructions into THIS agent's context. Every subagent
is restricted to `read_only` tools, so it needs no mandatory `human_approval` (pattern 46).

A domain's own tools live in `app/domains/<name>/tools.py` (Ecorp's `add_note`, `remember` and
`query_employees` in `app/domains/ecorp/`). All tools declare an explicit Pydantic `args_schema`: the JSON
schema the LLM sees, which constrains what it can send and rejects bad arguments loudly.

## Tenant isolation (app/core/security.py)

`search_docs` receives `config: RunnableConfig` (auto-injected by `ToolNode`, auto-excluded from the
LLM-visible schema) to read `SecurityCtx` from `config["configurable"]["ctx"]`. It calls `ctx_or_refuse` first
and fails closed if `ctx` is missing or malformed, rather than running an unscoped query.

## Cross-session memory

Recall is automatic: `gather_context` folds this principal's memories into the pre-fetch, re-filtered against
*current* ctx every call (`Policy.lower(ctx, "memories")`, scoped to `tenant` AND `owner`), never trusted from
a prior snapshot. The only writer is Ecorp's `remember` (`app/domains/ecorp/`).
"""
import ast
import asyncio
import logging
import operator
from collections.abc import Callable, Sequence
from enum import Enum
from typing import Literal

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, tool
from pydantic import BaseModel, Field, field_validator

from app.agent.tools import skill_catalog as skills_module
from app.core.config import (
    SKILLS_COLLECTION,
    SKILLS_SEARCH_TOP_K,
)
from app.core.security import DEFAULT_POLICY, SecurityCtx, valid_ctx
from app.core.tool_runtime import NO_CTX_REFUSAL, arun_with_timeout, ctx_or_refuse
from app.core.tool_schema import ToolArgs
from app.retrieval import qdrant_store

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


class SearchDocsArgs(ToolArgs):
    query: str = Field(..., description="Natural-language search query.")
    # Required but nullable, never optional (ToolArgs says why, and why a call that leaves one out still runs).
    topic: Topic | None = Field(
        ...,
        description="Filter: one of langgraph, qdrant, company. Pass null to search across all topics.",
    )
    doc_ids: list[str] | None = Field(
        ...,
        description="Scope the search to these specific document ids only "
        "(e.g. ones the caller already knows about/is cleared to read). Narrows the "
        "search — never a way to reach documents outside the caller's tenant. Pass null for no scoping.",
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
    `_format_cited_context`. app/agent/graph/core.py stores these in
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
    ctx = ctx_or_refuse(config, "search")
    if ctx is None:
        return NO_CTX_REFUSAL
    return await arun_with_timeout(_search_docs_impl, query, topic, ctx, doc_ids)


async def _calculator_impl(expression: str) -> str:
    try:
        return str(_safe_eval(ast.parse(expression, mode="eval").body))
    except Exception:  # noqa: BLE001
        return f"Could not evaluate: {expression!r}"


@tool(args_schema=CalculatorArgs)
async def calculator(expression: str) -> str:
    """Evaluate a basic arithmetic expression, e.g. '21 * 2'."""
    return await arun_with_timeout(_calculator_impl, expression)


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
    return await arun_with_timeout(_ask_clarification_impl, question, options)


def _unwrap(result):
    """A `gather(..., return_exceptions=True)` result, re-raised if it is an exception."""
    if isinstance(result, BaseException):
        raise result
    return result


async def gather_context(ctx: SecurityCtx | None, query: str) -> tuple[str, list[dict]]:
    """Documents + this principal's memories, hybrid-searched and combined
    into ONE continuously-numbered citation sequence — called by
    app/agent/graph/core.py's `default_search` (the automatic pre-fetch every
    turn), not the same call `_search_docs_impl` makes when the model asks,
    though both share the same retrieval/Policy machinery. This is the only
    reader of this principal's memories; `remember` is the only writer.

    Degrades to `("", [])` on missing ctx or a retrieval failure —
    enrichment, never fails the turn (see retrieve_context's try/except in
    app/agent/graph/core.py).

    The document and memory searches are independent, so they run concurrently: the
    pre-fetch costs the slower of the two instead of their sum. Both always run to the end, even
    if one fails (no search is left running unawaited), and the first failure is re-raised as is,
    so the caller's degrade path sees the same exception it always did.
    """
    if not valid_ctx(ctx):
        return "", []

    async def nothing() -> list:
        return []

    results = await asyncio.gather(
        _document_hits(ctx, query) if DEFAULT_POLICY.permit("search", ctx) else nothing(),
        _memory_hits(ctx, query) if DEFAULT_POLICY.permit("recall_memory", ctx) else nothing(),
        return_exceptions=True,
    )
    doc_hits, memory_hits = _unwrap(results[0]), _unwrap(results[1])
    all_hits = list(doc_hits) + list(memory_hits)
    if not all_hits:
        return "", []
    return _format_cited_context(all_hits), _citation_records(all_hits)


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
    app/agent/tools/skill_catalog.py) never leaks into a domain it wasn't written for.
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
        return await arun_with_timeout(_skill_search_impl, query)

    async def _use_skill_impl(name: str) -> str:
        record = skills_module.get_skills().get(name)
        if record is None or not _skill_visible_to_domain(record, domain):
            return (
                f"No skill named {name!r} found. Call skill_search first to find "
                "the exact name of an available skill."
            )
        # No tool reminder is appended here: the agent node already adds a recency-weighted one
        # while a tool the skill named is still pending (`pending_skill_required_tool`), and no
        # SKILL.md names run_command_in_sandbox any more.
        return record.body

    @tool(args_schema=UseSkillArgs)
    async def use_skill(name: str) -> str:
        """Load one skill's full instructions by its exact name (from
        skill_search's results). Follow the returned instructions using your
        other tools to complete the task."""
        return await arun_with_timeout(_use_skill_impl, name)

    return skill_search, use_skill




def skill_tools_first(
    action_tools: Sequence[BaseTool],
    reused_tools: Sequence[BaseTool],
    run_subagent: BaseTool | None = None,
) -> list[BaseTool]:
    """Order a domain's bound tool list with skill_search/use_skill FIRST, ahead of every action tool: a
    live-verified fix, not a guess.

    Every domain used to build `action_tools + reused_tools` (skill_search/use_skill near the end), and a
    sales deal-math question never called skill_search across many runs, even after three escalating
    prompt/docstring rewrites. The cause was list POSITION, not wording: swapping only the order made this
    app's local model (qwen2.5:3b via Ollama, grammar-constrained tool calling) call
    use_skill('deal-economics') first, 3/3 fresh runs, with no prompt change. A small grammar-constrained
    model's tool selection is sensitive to bound-list position as well as description; try that before
    assuming a "won't call X" pattern is an instruction-following ceiling.

    Only skill_search/use_skill move to the front. The rest of `reused_tools` (search_docs, ask_clarification)
    keep their position after the domain's action tools: search_docs doesn't share the problem, and a separate
    finding needed to REDUCE its reflexive use, so promoting it would fight that fix.

    `run_subagent`, if given, joins the same leading tier (it was appended dead last). Unlike the skill_search
    fix, this is a reasoned extension of the same principle and NOT independently live-verified; confirm the
    same way if it is ever questioned.
    """
    by_name = {t.name: t for t in reused_tools}
    promoted = [by_name[name] for name in ("skill_search", "use_skill") if name in by_name]
    if run_subagent is not None:
        promoted.append(run_subagent)
    rest = [t for t in reused_tools if t.name not in ("skill_search", "use_skill")]
    return promoted + list(action_tools) + rest


# --- Tool capability declarations -------------------------------------------
# Each tool declares its exposure "leg": read_only (safe immediately),
# mutating (writes persisted state), or outward (reaches outside the
# corpus). app/agent/graph/routing.py's should_continue routes ANY
# non-read_only tool call through human_approval unconditionally (mandatory,
# not the opt-in require_approval gate) — a RAG agent's context is untrusted
# on essentially every turn (GRAPH_PATTERNS.md pattern 12), so mutating or
# reaching outward on top of that is never worth gambling unsupervised. A
# tool absent from a domain's mapping defaults to "outward" — fail closed.
#
# This mapping covers only the tools shared by more than one domain. A domain
# merges it with the tiers of its own tools (app/domains/<name>/tools.py).
ToolCapability = Literal["read_only", "mutating", "outward"]

SHARED_TOOL_CAPABILITIES: dict[str, ToolCapability] = {
    "search_docs": "read_only",
    "calculator": "read_only",
    "ask_clarification": "read_only",
    "skill_search": "read_only",
    "use_skill": "read_only",
    # Honest, static declaration: run_subagent (GRAPH_PATTERNS.md pattern 46)
    # can only delegate to subagents whose tool subset is entirely read_only
    # (enforced structurally at catalog-build time) — as safe as search_docs.
    "run_subagent": "read_only",
}
