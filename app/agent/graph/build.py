"""The graph builder. One function, `build_graph`, compiles every graph this app runs; a
`GraphProfile` says which optional stages that graph includes.

- `FULL_PROFILE` — the main conversation: semantic cache, history compaction, follow-up
  suggestions, and citation enforcement.
- `SUBAGENT_PROFILE` — a nested `run_subagent` run (pattern 46): none of those. `build_subagent_graph`
  is `build_graph` with that profile.

Topology, in order: `validate_input -> moderate_input -> [check_semantic_cache -> (miss)
[compact_history ->]] retrieve_context -> agent <-> tools`, then `check_output`, which retries,
gives up, or ends (through `[suggest_followups] || [write_semantic_cache]`, in parallel). Moderation comes
before anything that spends: a refused or cache-hit turn pays for no compaction (pattern 25).
GRAPH_PATTERNS.md "Graph Flow" draws it.

`GraphDeps` bundles the swappable external clients, built once here and threaded into the node
factories that need them. Unset fields fall back to the real clients; tests set individual fields to
inject fakes instead of monkeypatching module globals.

`manifest` and `domain` are required: the same function serves any domain (system prompt, tools,
capability mapping, policy) with no branching, and none is the default. Ecorp is one entry in
`app/domains/registry.py`.

This is the composition root, so it is the one module that imports every node. Nothing a node
imports imports it back, which is why none of the graph modules needs a deferred import.
"""
import functools
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode
from langgraph.types import interrupt

from app.agent.graph.agent_node import make_agent_node
from app.agent.graph.cache import (
    make_check_semantic_cache_node,
    make_write_semantic_cache_node,
    route_after_cache,
)
from app.agent.graph.compaction import make_compact_history_node
from app.agent.graph.core import (
    AGENT_RETRY_POLICY,
    context_window_exceeded,
    moderate_input,
    reject_context,
    reject_input,
    reject_moderation,
    route_after_compaction,
    route_after_moderation,
    route_after_validation,
    validate_input,
)
from app.agent.graph.followups import make_suggest_followups_node
from app.agent.graph.hitl import make_human_approval_node, route_after_approval
from app.agent.graph.retrieval import make_retrieve_context_node
from app.agent.graph.retry import (
    make_no_answer_fallback_node,
    make_retry_exhausted_node,
    retry_output,
)
from app.agent.graph.routing import check_output, route_after_check, should_continue
from app.agent.graph.skill_guards import SKILL_REQUIRED_TOOL_MARKERS
from app.agent.graph.state import (
    HISTORY_TOKEN_CEILING,
    HISTORY_TOKEN_FLOOR,
    MAX_ITERATIONS,
    MAX_TOKENS_PER_TURN,
    State,
)
from app.agent.graph.tool_guards import tool_batch_guard
from app.agent.graph.utils import friendly_tool_error, instrumented, make_llm
from app.agent.tools import builtin
from app.core.config import CHAT_MODEL, MAX_COST_USD_PER_TURN
from app.core.security import SecurityCtx
from app.retrieval import semantic_cache

if TYPE_CHECKING:
    from app.agent.manifest import AgentManifest, DomainPlugin


async def default_cache_get(ctx: SecurityCtx | None, query: str) -> tuple[str, list[dict]] | None:
    return await semantic_cache.get(ctx, query)


async def default_cache_set(ctx: SecurityCtx | None, query: str, answer: str, citations: list[dict]) -> None:
    await semantic_cache.set(ctx, query, answer, citations)


async def default_search(query: str, ctx: SecurityCtx | None) -> tuple[str, list[dict]]:
    """Thin wrapper over `builtin.gather_context` matching the
    `Callable[[str, SecurityCtx | None], Awaitable[tuple[str, list[dict]]]]`
    shape `make_retrieve_context_node` expects, so tests can pass a plain
    fake function to the factory.

    `ctx` flows straight through — the same value search_docs/remember read
    from config when the *model* calls them as tools, so pre-fetched and
    on-demand retrieval are policy-enforced identically. Hybrid search
    (dense+sparse RRF, cross-encoder reranked) and cross-session memory
    recall both live inside `gather_context` (see app/agent/tools/builtin.py, pattern 20).

    async: gather_context awaits real I/O (the reranker is an HTTP call to
    ml-service, not local ONNX compute).
    """
    return await builtin.gather_context(ctx, query)


@dataclass
class GraphDeps:
    """Bundle of the graph's swappable external clients, built once at the
    composition root (build_graph) and threaded into the node factories
    that need them — see make_agent_node, make_retrieve_context_node.
    Unset fields fall back to the real clients inside build_graph(); tests
    set individual fields to inject fakes instead of monkeypatching module
    globals (see tests/agent/graph/test_agent_node.py, tests/agent/graph/test_nodes.py).
    """

    llm: Any = None
    # The LiteLLM alias `llm` is bound to, used only to price its calls
    # (app/agent/cost/pricing.py). None = the global chat alias; a delegated run whose
    # specialist declares its own model sets it so its calls are priced by that
    # model, not by the parent's.
    model_alias: str | None = None
    # The usage-event `kind` this client's calls are recorded under (app/agent/cost/usage_events.py).
    # "chat" for the main loop; a delegated run sets "subagent" so its spend is separable.
    meter_kind: str = "chat"
    search_docs: (
        Callable[[str, "SecurityCtx | None"], Awaitable[tuple[str, list[dict]]]] | None
    ) = None
    cache_get: (
        Callable[["SecurityCtx | None", str], Awaitable[tuple[str, list[dict]] | None]] | None
    ) = None
    cache_set: (
        Callable[["SecurityCtx | None", str, str, list[dict]], Awaitable[None]] | None
    ) = None
    # Builds the chat client from the domain's tools when `llm` is unset (default: `make_llm`, the
    # configured endpoint). A test pointing the graph at a local Ollama passes
    # `functools.partial(make_llm, model=..., base_url=...)` here instead of patching a module.
    llm_factory: Callable[[list], Any] | None = None
    # The function that pauses the graph at `human_approval` (default: langgraph's `interrupt`). A
    # test that wants an approval decision without driving a real pause passes a stand-in.
    interrupt: Callable[[Any], Any] | None = None


@dataclass(frozen=True)
class GraphProfile:
    """Which optional stages a graph includes. A stage that is absent is not a no-op node: it is
    not in the graph, and its neighbours are wired straight to the next one.

    `enforce_citations=False` drops the two places a graph pushes `[n]` markers on the model: the
    tail "every sentence must end with its marker" reminder in `agent`, and `check_output`'s
    uncited/misattributed findings and marker insertion. A nested run's sources are not the
    parent's, so its prompt says to use none.
    """

    include_cache: bool = True
    include_followups: bool = True
    include_compaction: bool = True
    enforce_citations: bool = True


FULL_PROFILE = GraphProfile()

# A nested, one-shot subagent run (pattern 46). The five omitted nodes are pure overhead (or
# worse) here:
# - check_semantic_cache / write_semantic_cache: a subagent run shared the top-level
#   conversation's own cache (keyed by tenant+principal), risking a cross-contaminated hit in
#   either direction.
# - suggest_followups: a full extra LLM call whose result the caller simply discarded.
# - compact_history / context_window_exceeded: provably inert, not just unlikely. Compaction only
#   fires past HISTORY_TOKEN_CEILING (24000), but a subagent run is hard-capped at
#   MAX_SUBAGENT_TOKENS_PER_RUN (4000).
# Deliberately KEPT despite looking like overhead: moderate_input (cheap, and the delegated `task`
# came from the PARENT model, not a person), retrieve_context (a lookup-focused subagent is usually
# well served by the same pre-fetch), and human_approval (currently unreachable here since only
# read_only tools are allowed, but it stays as defense in depth: no flag turns the mandatory gate off).
SUBAGENT_PROFILE = GraphProfile(
    include_cache=False,
    include_followups=False,
    include_compaction=False,
    enforce_citations=False,
)


def build_graph(
    manifest: "AgentManifest",
    domain: "DomainPlugin",
    deps: GraphDeps | None = None,
    checkpointer=None,
    max_iterations: int | None = None,
    max_tokens_per_turn: int | None = None,
    max_cost_usd_per_turn: float | None = None,
    emit_no_answer_message: bool = True,
    history_token_ceiling: int | None = None,
    history_token_floor: int | None = None,
    *,
    profile: GraphProfile = FULL_PROFILE,
):
    """Compile the graph.

    `deps` bundles swappable external clients (LLM, search) — see GraphDeps; tests inject fakes
    via a GraphDeps to run full scenarios (reject path, tool loop, HITL, iteration cap, retry)
    without a live model or Qdrant (tests/agent/graph/test_graph_integration.py).

    `checkpointer` defaults to an in-memory MemorySaver — fine for tests, never for a real HITL
    pause: a paused human_approval gate parks the run indefinitely, and MemorySaver doesn't survive
    a process restart. app/agent/runtime/core.py's init_graph_async() passes a durable
    AsyncPostgresSaver instead for the CLI/API singleton.

    `max_iterations`/`max_tokens_per_turn`/`max_cost_usd_per_turn` default to `None` (the
    MAX_ITERATIONS/MAX_TOKENS_PER_TURN/MAX_COST_USD_PER_TURN constants). `run_subagent` is the one
    caller that sets them, to the MAX_SUBAGENT_* ceilings (pattern 46), so a nested run is bounded
    by its own.

    `emit_no_answer_message` (default True) controls whether `no_answer` AND `retry_exhausted`
    (both "ended without a real answer" terminal paths) fill an empty final AIMessage with a
    user-facing fallback. `run_subagent` sets this False: it substitutes its own
    differently-worded message and outcome tag, so the real fallback text would be wrongly read as
    the subagent's own genuine answer.

    `history_token_ceiling`/`history_token_floor` default to `None` (HISTORY_TOKEN_CEILING/FLOOR).
    No production caller overrides these; they let tests exercise compact_history's hysteresis
    with small, controlled budgets instead of thousands of tokens of placeholder text.
    """
    deps = deps or GraphDeps()

    domain_tools = domain.tools()
    if manifest.allowed_tools:
        allowed = set(manifest.allowed_tools)
        domain_tools = [t for t in domain_tools if t.name in allowed]
    tool_capabilities = domain.tool_capabilities()
    valid_tool_names = frozenset(t.name for t in domain_tools)
    # A skill may name a tool as required; only ones THIS domain has can ever be demanded.
    skill_required_tools = tuple(m for m in SKILL_REQUIRED_TOOL_MARKERS if m in valid_tool_names)

    llm_client = deps.llm or (deps.llm_factory or make_llm)(domain_tools)
    model_alias = deps.model_alias or CHAT_MODEL

    # Plain module-level functions bound to THIS domain with functools.partial, not factories:
    # they stay directly callable from tests with just `state` (and the domain's values), and
    # each is checked against the capability mapping / tool set / prompt the graph was ACTUALLY
    # built with (pattern 23).
    domain_should_continue = functools.partial(
        should_continue,
        tool_capabilities=tool_capabilities,
        valid_tool_names=valid_tool_names,
        max_iterations=max_iterations if max_iterations is not None else MAX_ITERATIONS,
        max_tokens=max_tokens_per_turn if max_tokens_per_turn is not None else MAX_TOKENS_PER_TURN,
        max_cost_usd=max_cost_usd_per_turn if max_cost_usd_per_turn is not None else MAX_COST_USD_PER_TURN,
    )
    domain_check_output = functools.partial(
        check_output,
        system_prompt=manifest.system_prompt,
        enforce_citations=profile.enforce_citations,
        skill_required_tools=skill_required_tools,
    )
    domain_tool_batch_guard = functools.partial(tool_batch_guard, valid_tool_names=valid_tool_names)

    agent = make_agent_node(
        llm_client,
        model_alias=model_alias,
        kind=deps.meter_kind,
        citation_reminder=profile.enforce_citations,
        skill_required_tools=skill_required_tools,
        system_prompt=manifest.system_prompt,
    )
    retrieve_context = make_retrieve_context_node(deps.search_docs or default_search)

    # Error recovery: a failing tool (e.g. Qdrant unreachable) doesn't crash the run —
    # handle_tool_errors turns the exception into a ToolMessage so `agent` can react on the next
    # turn instead of the graph blowing up. Parallel: ToolNode already runs multiple tool_calls
    # from one AIMessage concurrently — no extra wiring needed. Wrapped in a function so it gets
    # the same lifecycle logging as every other node (pattern 14); the ToolNode does the work and
    # its result (messages, or Commands from a tool like run_subagent) is returned unchanged.
    tool_node = ToolNode(domain_tools, handle_tool_errors=friendly_tool_error)

    async def run_tools(state: State, config: RunnableConfig):
        return await tool_node.ainvoke(state, config)

    builder = StateGraph(State)

    # Every node below is wrapped in instrumented(name) at registration time, not by editing the
    # node functions themselves (pattern 14). Plain module-level functions stay undecorated so
    # they're directly callable from tests.
    def add(name: str, fn, **kwargs) -> None:
        builder.add_node(name, instrumented(name)(fn), **kwargs)

    add("validate_input", validate_input)
    add("reject_input", reject_input)
    add("reject_context", reject_context)
    add("moderate_input", moderate_input)
    add("reject_moderation", reject_moderation)
    add("retrieve_context", retrieve_context)
    # Reliability policy: retry a transient LLM-endpoint failure a few times before giving up —
    # see AGENT_RETRY_POLICY. Nothing else here gets one: `tools` recovers via handle_tool_errors
    # above, and every other node is a pure function of state where a retry would just repeat the
    # same bug (pattern 7).
    add("agent", agent, retry=AGENT_RETRY_POLICY)
    add("tools", run_tools)
    add("human_approval", make_human_approval_node(deps.interrupt or interrupt))
    add("tool_batch_guard", domain_tool_batch_guard)
    add("check_output", domain_check_output)
    add("retry_output", retry_output)
    add("retry_exhausted", make_retry_exhausted_node(emit_no_answer_message))
    add(
        "no_answer",
        make_no_answer_fallback_node(
            emit_no_answer_message,
            system_prompt=manifest.system_prompt,
            enforce_citations=profile.enforce_citations,
            skill_required_tools=skill_required_tools,
        ),
    )

    if profile.include_cache:
        # Read as bare module globals, resolved when build_graph runs: tests/conftest.py's autouse
        # fixtures replace default_cache_get/default_cache_set on THIS module.
        add("check_semantic_cache", make_check_semantic_cache_node(deps.cache_get or default_cache_get))
        add(
            "write_semantic_cache",
            make_write_semantic_cache_node(deps.cache_set or default_cache_set, tool_capabilities),
        )
    if profile.include_compaction:
        # Reuses the SAME llm client as `agent` (a summarization call doesn't need tools bound; a
        # tool-bound client asked a plain prompt just answers it).
        add(
            "compact_history",
            make_compact_history_node(
                llm_client,
                ceiling=history_token_ceiling if history_token_ceiling is not None else HISTORY_TOKEN_CEILING,
                floor=history_token_floor if history_token_floor is not None else HISTORY_TOKEN_FLOOR,
                model_alias=model_alias,
            ),
        )
        add("context_window_exceeded", context_window_exceeded)
    if profile.include_followups:
        # Same: a tool-bound client asked a plain question just answers it.
        add("suggest_followups", make_suggest_followups_node(llm_client, model_alias=model_alias))

    # Where each stage hands off when the optional ones after it are absent.
    after_cache_miss = "compact_history" if profile.include_compaction else "retrieve_context"
    after_moderation = "check_semantic_cache" if profile.include_cache else after_cache_miss
    # What runs once an answer stands. Neither step needs the other (the cache write reads the
    # answer and its citations; the follow-ups only add `followups`), so they are one parallel
    # superstep: `done` no longer waits for the follow-up LLM call and THEN the cache write's
    # embedding and Redis round trip.
    finish_steps = [
        name
        for name, present in (
            ("suggest_followups", profile.include_followups),
            ("write_semantic_cache", profile.include_cache),
        )
        if present
    ]

    def route_after_check_bound(state: State):
        route = route_after_check(state)
        if route != "finish":
            return route
        return finish_steps or END

    builder.add_edge(START, "validate_input")
    builder.add_conditional_edges("validate_input", route_after_validation)
    builder.add_edge("reject_input", END)
    builder.add_edge("reject_context", END)

    builder.add_conditional_edges(
        "moderate_input",
        route_after_moderation,
        {"reject_moderation": "reject_moderation", "check_semantic_cache": after_moderation},
    )
    builder.add_edge("reject_moderation", END)

    if profile.include_cache:
        builder.add_conditional_edges(
            "check_semantic_cache",
            route_after_cache,
            {"check_output": "check_output", "compact_history": after_cache_miss},
        )
    if profile.include_compaction:
        builder.add_conditional_edges("compact_history", route_after_compaction)
        builder.add_edge("context_window_exceeded", END)

    builder.add_edge("retrieve_context", "agent")
    builder.add_conditional_edges("agent", domain_should_continue)
    builder.add_conditional_edges("human_approval", route_after_approval)
    builder.add_edge("tools", "agent")
    builder.add_edge("tool_batch_guard", "agent")

    builder.add_conditional_edges(
        "check_output",
        route_after_check_bound,
        {
            "retry_output": "retry_output",
            "retry_exhausted": "retry_exhausted",
            **{name: name for name in finish_steps},
            END: END,
        },
    )
    builder.add_edge("retry_output", "agent")
    builder.add_edge("retry_exhausted", END)
    builder.add_edge("no_answer", END)
    for name in finish_steps:
        builder.add_edge(name, END)

    compiled = builder.compile(checkpointer=checkpointer or MemorySaver())
    # Not LangGraph API — plain attribute stashes so a caller holding the compiled graph (chiefly
    # tests and scripts) can recover which domain built
    # it and which tools it was given, without changing this function's return type.
    compiled.manifest = manifest  # type: ignore[attr-defined]  # deliberate stash, see comment above
    compiled.tools_by_name = {t.name: t for t in domain_tools}  # type: ignore[attr-defined]  # same
    return compiled


def build_subagent_graph(
    manifest: "AgentManifest",
    domain: "DomainPlugin",
    deps: GraphDeps | None = None,
    checkpointer=None,
    max_iterations: int | None = None,
    max_tokens_per_turn: int | None = None,
    max_cost_usd_per_turn: float | None = None,
    emit_no_answer_message: bool = True,
):
    """`build_graph` with `SUBAGENT_PROFILE`, for a NESTED subagent run
    (`app/agent/tools/run_subagent.py`, pattern 46). Parameters mirror `build_graph`'s, minus
    `history_token_ceiling`/`history_token_floor`: there is no `compact_history` to size."""
    return build_graph(
        manifest,
        domain,
        deps,
        checkpointer,
        max_iterations,
        max_tokens_per_turn,
        max_cost_usd_per_turn,
        emit_no_answer_message,
        profile=SUBAGENT_PROFILE,
    )
