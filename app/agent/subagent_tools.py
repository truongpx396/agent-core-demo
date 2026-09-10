"""Everything behind `run_subagent` (GRAPH_PATTERNS.md pattern 46): the
bundled-catalog resolution (`_resolve_subagent_tools`/`_build_subagent_registry`/
`_subagent_declared_for_domain`), the compiled-graph cache, `_run_subagent_impl`
itself, and both `run_subagent` tool constructions (the Ecorp-level
singleton and `make_domain_subagent_tool`'s per-domain factory). Split out
of `app/agent/tools.py` purely for file size — see that module's own
docstring, and `app/agent/graph_hitl.py`/`app/agent/graph_utils.py`'s for
the analogous splits on the graph.py side. No behavior change from the
pre-split single-file version.

`_run_subagent_impl` reads `ChatOpenAI` through `tools_module.ChatOpenAI`
rather than a plain statically-imported bare name — same real bug/fix as
`app/agent/graph_hitl.py`'s own `graph_module.interrupt` (see its
docstring): `tests/agent/test_concurrent_turns.py` does
`monkeypatch.setattr(tools_module, "ChatOpenAI", ...)`, patching an
attribute on the live `app.agent.tools` module object. A statically-
imported bare name here would bind to the ORIGINAL class once, at this
module's own import time, permanently, so the monkeypatch would silently
never take effect and construct a real `ChatOpenAI` client instead of the
test's fake.

**A second, distinct structural issue this split had to handle** (not a
monkeypatch trap): the Ecorp-level `run_subagent` tool used to be appended
to `app.agent.tools.TOOLS` as a guaranteed module-load-time side effect,
simply by being physically inside `tools.py`, executed after `TOOLS`'s own
base-tool list was built. Now that construction lives here instead — see
`tools.py`'s own trailing `from app.agent import subagent_tools` import
(added at the very end of that file, after `TOOLS`/`TOOL_CAPABILITIES` are
fully defined) for how that guarantee is preserved: importing `tools.py`
still deterministically triggers this module to load and append
`run_subagent` onto the SAME shared `TOOLS` list object, regardless of
which module a caller (chiefly `graph.py`) imports `TOOLS` from.
"""
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Annotated, Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, InjectedToolCallId, tool
from langgraph.types import Command
from pydantic import BaseModel, Field, SecretStr, field_validator

from app.agent import subagents as subagents_module
from app.agent import tools as tools_module
from app.agent.tools import (
    _NO_CTX_REFUSAL,
    TOOL_CAPABILITIES,
    TOOLS,
    _ctx_from_config,
    _run_with_timeout,
    logger,
)
from app.core import metrics
from app.core.config import (
    CHAT_MODEL,
    MAX_SUBAGENT_COST_USD_PER_RUN,
    OPENAI_API_BASE,
    OPENAI_API_KEY,
)
from app.core.scrubbing import scrub
from app.core.security import DEFAULT_POLICY, valid_ctx

# --- Subagents (app/agent/subagents.py, GRAPH_PATTERNS.md pattern 46) -------
# run_subagent delegates a bounded, self-contained task to a fresh, ISOLATED
# nested agent run (a genuinely separate graph.invoke(), against
# build_subagent_graph() — the SAME node functions/factories the top-level
# agent runs on, but a leaner topology purpose-built for a nested one-shot
# call, see that function's own docstring) — not more instructions loaded
# into THIS agent's own context, which is what skill_search/use_skill do
# instead. Every piece of domain-specific
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

# Compiled nested graph + bound LLM client, reused across calls instead of
# rebuilt from scratch on every single run_subagent invocation — the
# topology/LLM/tools/manifest are all static per (domain, subagent_name),
# so rebuilding per-call was pure waste (a whole StateGraph compile + a
# fresh ChatOpenAI().bind_tools() every time). Same lazy, unlocked,
# process-wide cache shape as app/agent/subagents.py's own
# `_subagents_cache`/`get_subagents()`/`reload_subagents()` — a benign
# first-use race (two concurrent misses both build) is already accepted
# there and accepted here for the same reason. Only ever populated/read
# when a caller opts in via `_run_subagent_impl(..., use_cache=True)` — see
# that function's own docstring for why this is opt-in, not inferred from
# whether `registry`/`tools_by_name`/`llm` were passed.
_subagent_graph_cache: dict[tuple[str, str], Any] = {}


def reset_subagent_graph_cache() -> None:
    """Test/ops hook — mirrors app/agent/subagents.py::reload_subagents()."""
    global _subagent_graph_cache
    _subagent_graph_cache = {}


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
    `domain="ecorp"` — rather than silently becoming available to every new
    domain this app ever grows. See app/agent/subagents.py's own docstring
    for why that default differs from app/agent/skills.py's SkillRecord
    (there, `None` means "every domain"): a subagent's declared `tools:`
    are only ever meaningful against ONE specific tool universe, so an
    untagged subagent handed to a domain its tools were never written for
    would typically just resolve to nothing useful anyway."""
    if record.domains is None:
        return domain == "ecorp"
    return domain in record.domains


def _build_subagent_registry(
    all_tool_names: frozenset[str], tool_capabilities: Mapping[str, str], *, domain: str
) -> dict[str, tuple["subagents_module.SubagentRecord", tuple[str, ...]]]:
    """Every bundled AGENT.md declared for `domain` (see
    `_subagent_declared_for_domain`), each resolved to its safe, read_only
    tool subset within `all_tool_names`/`tool_capabilities` — that pair is
    itself domain-specific (a nested subagent run can only ever call tools
    that exist, and are read_only, WITHIN the calling domain's own tool
    universe, not Ecorp's)."""
    registry = {}
    for record in subagents_module.get_subagents().values():
        if not _subagent_declared_for_domain(record, domain):
            continue
        resolved = _resolve_subagent_tools(record.name, record.tools, all_tool_names, tool_capabilities)
        registry[record.name] = (record, resolved)
    return registry


_SUBAGENT_REGISTRY = _build_subagent_registry(_ALL_TOOL_NAMES, TOOL_CAPABILITIES, domain="ecorp")

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
    run with zero tools, not silently fall back to the full Ecorp tool set —
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


@dataclass(frozen=True)
class SubagentResult:
    """`_run_subagent_impl`'s return shape: the scrubbed answer text plus the
    nested run's own `total_tokens`/`total_cost_usd` — needed separately
    from the answer string so the calling `run_subagent` tool can fold them
    into the PARENT turn's live budget via `Command(update={"subagent_spend":
    [(total_tokens, total_cost_usd)]})` (see app/agent/graph.py's `State`
    docstring and `should_continue`, GRAPH_PATTERNS.md pattern 46's disclosed
    "spend isn't live-folded into the parent's own ceiling" gap)."""

    answer: str
    total_tokens: int
    total_cost_usd: float


def _run_subagent_impl(
    subagent_name: str,
    task: str,
    config: RunnableConfig,
    *,
    domain: str = "ecorp",
    registry: dict[str, tuple] | None = None,
    tools_by_name: Mapping[str, Any] | None = None,
    llm: Any = None,
    use_cache: bool = False,
) -> SubagentResult:
    """Build and run one nested, isolated agent turn, then return its final
    answer plus its own usage. See GRAPH_PATTERNS.md pattern 46 for the full
    design; `registry`/`tools_by_name`/`llm` are DI for tests (mirror
    `build_graph(deps=...)`'s own override shape) — `registry` defaults to
    the real, process-wide `_SUBAGENT_REGISTRY`, `tools_by_name` defaults to
    every Ecorp tool by name (`{t.name: t for t in TOOLS}`), `llm` defaults
    to `None`, meaning "construct a real ChatOpenAI client for this
    subagent's own model alias." A test passing a fake chat model here
    bypasses that construction entirely, the same way `GraphDeps(llm=fake)`
    already bypasses `_make_llm` at the top level — real tools.py tools
    have no other network-touching construction to fake.

    `tools_by_name` matters for the same reason `registry` itself does: a
    domain-scoped subagent's `registry` entry can resolve to a tool name
    like `check_ticket_status` that simply isn't IN Ecorp's own `TOOLS` —
    looking it up there would silently drop it. `make_domain_subagent_tool`
    passes the calling domain's own tool objects here; the Ecorp-level
    construction below relies on the default, since Ecorp's own registry
    only ever resolves to names already in Ecorp's own `TOOLS`.

    `domain` is purely a cache-key/tracing-metadata component — it does NOT
    select `registry` (that's still always whatever the caller passes, or
    `_SUBAGENT_REGISTRY` by default); `make_domain_subagent_tool` already
    resolves a domain-scoped `registry`/`tools_by_name` pair itself and
    passes both explicitly, `domain` just labels which one so the compiled-
    graph cache below and the nested run's tracing metadata can tell two
    domains' same-named subagent apart.

    `use_cache` opts into reusing a compiled nested graph across calls
    (see `_subagent_graph_cache` above) — deliberately NOT inferred from
    whether `llm`/`registry`/`tools_by_name` were passed, since
    `make_domain_subagent_tool`'s real production closure always passes its
    own `registry`/`tools_by_name` (there's no sensible domain-agnostic
    default for a non-Ecorp domain), so that inference would silently
    defeat caching for every domain but Ecorp. Only the two real
    `run_subagent` tool closures pass `use_cache=True`; every test calls
    this function directly and never sets it, so tests are automatically
    excluded from the cache with no change needed to their own call shape.

    Isolation, in one place: a FRESH `messages` list (the subagent's own
    system prompt + exactly the delegated `task` as its sole HumanMessage —
    NOT this conversation's history); the subagent's OWN tool subset and
    model alias; SecurityCtx INHERITED unconditionally from `config`, never
    re-derived; its own, smaller, fixed budget ceiling
    (MAX_SUBAGENT_ITERATIONS/MAX_SUBAGENT_TOKENS_PER_RUN/
    MAX_SUBAGENT_COST_USD_PER_RUN); a throwaway MemorySaver, never the
    durable checkpointer — this run is bounded to complete within this one
    tool call, never independently resumable later.

    Uses `build_subagent_graph()` — `build_graph()`'s SAME node functions/
    factories, reused via `_assemble_shared_graph_parts`, but a leaner
    topology purpose-built for a nested one-shot run: no semantic-cache
    read/write (so a subagent's answer can never be cross-served to/from a
    top-level query with near-identical phrasing — a previously-disclosed
    gap, now structurally closed, not just narrowed), no `suggest_followups`
    call (its result was always discarded here anyway), no history
    compaction (provably unreachable given `MAX_SUBAGENT_TOKENS_PER_RUN` is
    far below the ceiling that would ever trigger it). See that function's
    own docstring for the full per-node reasoning.
    """
    ctx = _ctx_from_config(config)
    if not valid_ctx(ctx):
        return SubagentResult(_NO_CTX_REFUSAL, 0, 0.0)

    reg = registry if registry is not None else _SUBAGENT_REGISTRY
    entry = reg.get(subagent_name)
    if entry is None:
        return SubagentResult(f"No subagent named {subagent_name!r} is registered.", 0, 0.0)
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
    )
    from app.agent.graph_build_subagent import build_subagent_graph
    from app.agent.manifest import AgentManifest
    from app.agent.meter import record_usage

    # Computed fresh on every call regardless of caching below — cheap string
    # formatting, fully determined by record.system_prompt (already resolved
    # from the registry above), no need to cache it separately from the
    # compiled graph.
    nested_system_prompt = f"{record.system_prompt}\n\n{_CITATION_MARKER_WARNING}"

    cache_key = (domain, subagent_name)
    if use_cache and cache_key in _subagent_graph_cache:
        nested_graph = _subagent_graph_cache[cache_key]
    else:
        nested_llm = llm if llm is not None else tools_module.ChatOpenAI(
            model=record.model or CHAT_MODEL,
            base_url=OPENAI_API_BASE,
            api_key=SecretStr(OPENAI_API_KEY),
            temperature=0,
            stream_usage=True,
        ).bind_tools(nested_tools)

        nested_manifest = AgentManifest(name=record.name, system_prompt=nested_system_prompt)
        nested_domain = _SubagentDomainPlugin(nested_tools)

        nested_graph = build_subagent_graph(
            deps=GraphDeps(llm=nested_llm),
            manifest=nested_manifest,
            domain=nested_domain,
            max_iterations=MAX_SUBAGENT_ITERATIONS,
            max_tokens_per_turn=MAX_SUBAGENT_TOKENS_PER_RUN,
            max_cost_usd_per_turn=MAX_SUBAGENT_COST_USD_PER_RUN,
            # This function's own post-run check below needs a genuinely empty
            # final AIMessage to detect "some safety net fired" and report its
            # own "did not produce a final answer" message + outcome=
            # "budget_exceeded" — the top-level graph's no_answer node would
            # otherwise fill that content in first (see build_graph's docstring).
            emit_no_answer_message=False,
        )
        if use_cache:
            _subagent_graph_cache[cache_key] = nested_graph

    parent_thread_id = (config or {}).get("configurable", {}).get("thread_id", "unknown")
    nested_thread_id = f"{parent_thread_id}:subagent:{record.name}:{uuid.uuid4().hex[:8]}"
    nested_config = {
        "configurable": {"thread_id": nested_thread_id, "ctx": ctx},
        # Threads the parent run's own tracing callbacks (e.g. Langfuse)
        # through, so this nested run's internal LLM/tool calls show up as
        # child spans instead of being invisible until _invoke() returns.
        "callbacks": config.get("callbacks"),
        # Tags every event this nested run produces (LLM stream chunks, tool
        # start/end) with which subagent it came from — consumed by
        # app/agent/runtime.py::_run_graph_stream to (a) keep this run's own
        # "agent" node token stream from leaking into the client's main
        # answer stream (both graphs use the SAME node name), and (b)
        # surface this run's own tool activity to the client, tagged, instead
        # of a silent black box for the whole call.
        "metadata": {
            "subagent_name": record.name,
            "parent_thread_id": parent_thread_id,
            "domain": domain,
        },
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
    total_cost_usd = result_state.get("total_cost_usd", 0.0)
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
    return SubagentResult(answer, total_tokens, total_cost_usd)


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
        # Injected by ToolNode, never shown to or settable by the LLM. Must be
        # declared here, on the args_schema itself, not just on the wrapper
        # function's own signature below — this codebase's tools all use an
        # explicit args_schema, and LangChain's injected-argument detection
        # for InjectedToolCallId (unlike its `config: RunnableConfig`
        # detection, which scans the raw function) scans args_schema's own
        # fields, verified directly against this repo's pinned
        # langchain-core: omitting it here raises a TypeError at invocation
        # despite the function parameter existing. Needed to build the
        # ToolMessage this tool now returns itself via Command(update=...).
        tool_call_id: Annotated[str, InjectedToolCallId]

        @field_validator("task")
        @classmethod
        def _not_blank(cls, v: str) -> str:
            if not v.strip():
                raise ValueError("task must not be empty")
            return v

    @tool(args_schema=RunSubagentArgs)
    def run_subagent(
        subagent_name: SubagentName,
        task: str,
        config: RunnableConfig,
        tool_call_id: Annotated[str, InjectedToolCallId],
    ) -> Command:
        """Delegate a self-contained task to a specialized subagent running in
        its own isolated context — it does NOT see this conversation's
        history, only the `task` description you give it, so describe
        everything it needs to know. Use this to keep a multi-step lookup's
        intermediate steps out of the main conversation, or when a task
        matches a subagent's specific focus better than doing it yourself.
        Every subagent is restricted to read_only tools, so calling this
        never needs human approval."""
        result = _run_subagent_impl(subagent_name.value, task, config, domain="ecorp", use_cache=True)
        return Command(
            update={
                "messages": [ToolMessage(content=result.answer, tool_call_id=tool_call_id)],
                "subagent_spend": [(result.total_tokens, result.total_cost_usd)],
            }
        )

    TOOLS.append(run_subagent)


def make_domain_subagent_tool(
    domain: str, all_tools: list, tool_capabilities: Mapping[str, str]
) -> BaseTool | None:
    """Builds a `run_subagent` tool scoped to `domain` — the domain
    equivalent of the Ecorp-level construction above (same closed-enum
    menu, same `RunSubagentArgs` shape, same mandatory read_only-only
    resolution via `_resolve_subagent_tools`), but resolved against
    `all_tools`/`tool_capabilities` — the CALLING domain's own tool
    universe, e.g. app/domains/support/domain.py passes its own ticket
    tools plus its own `search_docs`/`skill_search`/`use_skill`/
    `ask_clarification`, not Ecorp's `TOOLS`/`TOOL_CAPABILITIES` — and
    filtered to only the subagents actually declared for `domain`
    (`domains: [...]` frontmatter, see app/agent/subagents.py's docstring
    for the "untagged means Ecorp-only" default this applies).

    Returns `None` if that filtered registry ends up empty — a domain with
    no bundled subagent gets no `run_subagent` tool at all, never one
    offering an empty menu: a `SubagentName`-style enum needs at least one
    real member to be a meaningful closed vocabulary, and a tool a caller
    can never usefully invoke is worse than no tool (same "that omission is
    what sandboxed means" posture app/domains/support/domain.py's own
    docstring already takes for tools this app deliberately doesn't expose).

    Each call builds a genuinely NEW, distinct closure (its own Enum class,
    `Args` schema, and `run_subagent` tool object) — never the Ecorp-level
    `run_subagent` above. Meant to be called ONCE, at each domain module's
    own import time (same "built once, not lazily" reasoning the
    Ecorp-level block's own comment gives — a subagent added to disk after
    the process starts needs a restart to appear here either way).
    """
    all_tool_names = frozenset(t.name for t in all_tools)
    registry = _build_subagent_registry(all_tool_names, tool_capabilities, domain=domain)
    if not registry:
        return None

    tools_by_name = {t.name: t for t in all_tools}

    # A per-domain Enum TYPE (not just distinct member values) — reusing
    # SubagentName here would mix this domain's menu with Ecorp's, and two
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
        # See RunSubagentArgs's identical field above for why this must be
        # declared on the schema itself, not just the wrapper function below.
        tool_call_id: Annotated[str, InjectedToolCallId]

        @field_validator("task")
        @classmethod
        def _not_blank(cls, v: str) -> str:
            if not v.strip():
                raise ValueError("task must not be empty")
            return v

    @tool(args_schema=_RunSubagentArgs)
    def run_subagent(
        subagent_name: domain_subagent_name,
        task: str,
        config: RunnableConfig,
        tool_call_id: Annotated[str, InjectedToolCallId],
    ) -> Command:
        """Delegate a self-contained task to a specialized subagent running in
        its own isolated context — it does NOT see this conversation's
        history, only the `task` description you give it, so describe
        everything it needs to know. Use this to keep a multi-step lookup's
        intermediate steps out of the main conversation, or when a task
        matches a subagent's specific focus better than doing it yourself.
        Every subagent is restricted to read_only tools, so calling this
        never needs human approval."""
        result = _run_subagent_impl(
            subagent_name.value,
            task,
            config,
            domain=domain,
            registry=registry,
            tools_by_name=tools_by_name,
            use_cache=True,
        )
        return Command(
            update={
                "messages": [ToolMessage(content=result.answer, tool_call_id=tool_call_id)],
                "subagent_spend": [(result.total_tokens, result.total_cost_usd)],
            }
        )

    return run_subagent
