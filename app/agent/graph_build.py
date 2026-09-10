"""`build_graph()` — compiles the main conversation graph. Split out of
`app/agent/graph.py` (which still holds `State`, every node function/
factory, and the shared assembly helper this function calls) purely for
file size — see `app/agent/graph.py`'s own module docstring and
`app/agent/graph_routing.py`'s for the sibling split (`should_continue`/
`check_output`) and `app/agent/graph_build_subagent.py` (the nested-run
counterpart, `build_subagent_graph()`). No behavior change from the
pre-split single-file version.
"""
from typing import TYPE_CHECKING

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from app.agent import graph as graph_module
from app.agent.graph import (
    AGENT_RETRY_POLICY,
    HISTORY_TOKEN_CEILING,
    HISTORY_TOKEN_FLOOR,
    GraphDeps,
    State,
    _assemble_shared_graph_parts,
    context_window_exceeded,
    make_check_semantic_cache_node,
    make_compact_history_node,
    make_no_answer_fallback_node,
    make_retry_exhausted_node,
    make_suggest_followups_node,
    make_write_semantic_cache_node,
    moderate_input,
    reject_context,
    reject_input,
    reject_moderation,
    retry_output,
    route_after_cache,
    route_after_compaction,
    route_after_moderation,
    route_after_validation,
    validate_input,
)
from app.agent.graph_hitl import human_approval, route_after_approval
from app.agent.graph_routing import route_after_check
from app.agent.graph_skills import use_skill_without_search
from app.agent.graph_tools import invalid_tool_call, too_many_tool_calls
from app.agent.graph_utils import _friendly_tool_error, _instrumented

if TYPE_CHECKING:
    from app.agent.manifest import AgentManifest, DomainPlugin


# --- Build the graph ---
def build_graph(
    deps: GraphDeps | None = None,
    checkpointer=None,
    manifest: "AgentManifest | None" = None,
    domain: "DomainPlugin | None" = None,
    max_iterations: int | None = None,
    max_tokens_per_turn: int | None = None,
    max_cost_usd_per_turn: float | None = None,
    emit_no_answer_message: bool = True,
    history_token_ceiling: int | None = None,
    history_token_floor: int | None = None,
):
    """Compile the graph.

    `deps` bundles the graph's swappable external clients (LLM, search) —
    see GraphDeps; unset fields default to the real clients. Tests pass a
    GraphDeps with fakes to run full graph scenarios — reject path, tool
    loop, HITL approve/reject, iteration cap, retry — without hitting a
    live model or Qdrant. See tests/agent/test_graph_integration.py.

    `checkpointer` defaults to an in-memory MemorySaver — fine for tests
    (nothing needs to survive this process) but never for a real HITL
    pause: a mandatory or opt-in human_approval gate parks the run
    indefinitely, and MemorySaver's "durability" ends the moment the
    process restarts. app/agent/runtime.py's init_graph_async() passes a
    durable AsyncPostgresSaver instead for the CLI/API singleton — see its
    module docstring for why that's not just `checkpointer=PostgresSaver(...)` here.

    `manifest`/`domain` (GRAPH_PATTERNS.md pattern 23, app/agent/manifest.py) are
    what let this SAME function serve a completely different domain — a
    different system prompt, tool set, tool-capability mapping, and Policy
    — without any code in this function branching on which domain it is.
    Both default to `app.agent.manifest`'s `DEFAULT_MANIFEST`/`DEFAULT_DOMAIN_PLUGIN`
    (this app's existing Ecorp setup, unchanged), imported here rather than
    at module level specifically to avoid a circular import — see
    app/agent/manifest.py's module docstring for the full reasoning; don't hoist
    this import without re-reading that. `deps.search_docs`/`cache_get`/
    `cache_set` remain the separate, already-existing override points for
    retrieval/caching (pattern 20/22) — a domain plugin whose tools need a
    different corpus or cache is expected to supply its own `GraphDeps`
    alongside its manifest/domain, the same way a test already does today.

    `max_iterations`/`max_tokens_per_turn`/`max_cost_usd_per_turn` default to
    `None`, which falls back to this module's own MAX_ITERATIONS/
    MAX_TOKENS_PER_TURN/MAX_COST_USD_PER_TURN exactly as before these params
    existed — every existing caller passing none of them is unaffected.
    `app/agent/tools.py::run_subagent` is the one caller that sets them, to
    MAX_SUBAGENT_ITERATIONS/MAX_SUBAGENT_TOKENS_PER_RUN/
    MAX_SUBAGENT_COST_USD_PER_RUN (GRAPH_PATTERNS.md pattern 46), so a nested
    subagent run is bounded by its own ceiling rather than inheriting
    whichever budget the top-level runtime happens to use.

    `emit_no_answer_message` (default True) controls whether the `no_answer`
    node (reached via should_continue's four safety-net exits) fills an
    empty final AIMessage with a user-facing fallback string — see
    make_no_answer_fallback_node's docstring. ALSO controls the separate
    `retry_exhausted` node (reached via route_after_check giving up on a
    stuck retry_output loop — see MAX_CONSECUTIVE_SAME_RETRY_REASON) for
    the identical reason: both are "this run ended without a real answer"
    terminal paths, so both need to stay silent for the SAME caller.
    `run_subagent` is the one caller that sets this False: its nested
    graph needs the SAME empty/unmodified content should_continue's (or
    route_after_check's) routing already produces, since it does its own,
    differently-worded "did not produce a final answer" substitution and
    outcome="budget_exceeded" tagging on the raw result — a real, non-empty
    apology message from either node would be wrongly read as the
    subagent's own genuine answer otherwise.

    `history_token_ceiling`/`history_token_floor` default to `None`, falling
    back to HISTORY_TOKEN_CEILING/HISTORY_TOKEN_FLOOR — the same
    None-means-module-default shape as max_iterations/max_tokens_per_turn
    above. No production caller overrides these; they exist purely so
    tests can exercise compact_history's hysteresis behavior with small,
    controlled token budgets instead of needing thousands of tokens of
    placeholder conversation content to trip the real ones.
    """
    parts = _assemble_shared_graph_parts(
        deps, manifest, domain, max_iterations, max_tokens_per_turn, max_cost_usd_per_turn
    )
    deps = parts.deps
    manifest = parts.manifest
    domain_tools = parts.domain_tools
    agent = parts.agent
    retrieve_context = parts.retrieve_context
    domain_should_continue = parts.domain_should_continue
    domain_check_output = parts.domain_check_output

    # Reuses the SAME llm client as `agent` — a second, separately
    # configured client for one follow-up-suggestion call per turn would
    # be a second thing to keep in sync with GraphDeps for no real
    # benefit; a tool-bound client asked a plain question just answers it.
    suggest_followups = make_suggest_followups_node(parts.llm_client)
    # Reuses the SAME llm client too — same reasoning as suggest_followups
    # above: a summarization call doesn't need tools bound, and a
    # tool-bound client asked a plain summarization prompt just answers it.
    compact_history = make_compact_history_node(
        parts.llm_client,
        ceiling=history_token_ceiling if history_token_ceiling is not None else HISTORY_TOKEN_CEILING,
        floor=history_token_floor if history_token_floor is not None else HISTORY_TOKEN_FLOOR,
    )
    # Read as graph_module.X, not a bare `from app.agent.graph import
    # _default_cache_get` name — tests/conftest.py's autouse
    # mock_semantic_cache fixture monkeypatches THESE EXACT attributes on
    # the live `app.agent.graph` module object (`monkeypatch.setattr(graph,
    # "_default_cache_get", ...)`) so every test gets a hardcoded miss/no-op
    # instead of touching a real cache. A statically-imported bare name
    # would bind to the ORIGINAL function once, at this module's own import
    # time — permanently, since Python's `from X import Y` copies the
    # reference rather than tracking X's attribute — so the monkeypatch
    # would silently never take effect here. Verified directly: this was a
    # real bug caught by the existing test suite (a fake-LLM test got back
    # a stale cached answer from an unrelated test's real cache write)
    # before switching to this module-qualified form.
    check_semantic_cache = make_check_semantic_cache_node(
        deps.cache_get or graph_module._default_cache_get
    )
    write_semantic_cache = make_write_semantic_cache_node(
        deps.cache_set or graph_module._default_cache_set
    )

    builder = StateGraph(State)

    # Every node below is wrapped in _instrumented(name) at registration
    # time, not by editing the node functions themselves — see its
    # docstring and GRAPH_PATTERNS.md pattern 14. The plain module-level
    # functions (e.g. `graph.reject_input`) stay undecorated, which is what
    # keeps them directly callable from tests exactly as before; `agent`
    # and `retrieve_context` are the two exceptions built above via a
    # factory, since they need an injected client.
    builder.add_node("validate_input", _instrumented("validate_input")(validate_input))
    builder.add_node("reject_input", _instrumented("reject_input")(reject_input))
    builder.add_node("reject_context", _instrumented("reject_context")(reject_context))
    builder.add_node(
        "compact_history", _instrumented("compact_history")(compact_history)
    )
    builder.add_node(
        "context_window_exceeded",
        _instrumented("context_window_exceeded")(context_window_exceeded),
    )
    builder.add_node("moderate_input", _instrumented("moderate_input")(moderate_input))
    builder.add_node(
        "reject_moderation", _instrumented("reject_moderation")(reject_moderation)
    )
    builder.add_node(
        "check_semantic_cache",
        _instrumented("check_semantic_cache")(check_semantic_cache),
    )
    builder.add_node(
        "retrieve_context", _instrumented("retrieve_context")(retrieve_context)
    )
    # Reliability policy: retry a transient LLM-endpoint failure (connection
    # error, 5xx) a few times before giving up — see AGENT_RETRY_POLICY.
    # Nothing else here gets a retry policy: `tools` already recovers via
    # handle_tool_errors below (no exception ever escapes it to retry), and
    # every other node is a pure, deterministic function of state where a
    # retry would just repeat the same bug (GRAPH_PATTERNS.md pattern 7).
    builder.add_node("agent", _instrumented("agent")(agent), retry=AGENT_RETRY_POLICY)
    # Error recovery: a failing tool (e.g. Qdrant unreachable) doesn't crash
    # the run — handle_tool_errors turns the exception into a ToolMessage so
    # the agent node sees it on the next turn and can react (apologize, fall
    # back to general knowledge, etc.) instead of the graph blowing up.
    #
    # Parallel tool execution: if the LLM returns multiple tool_calls in one
    # AIMessage (e.g. "search docs AND compute 12*7"), ToolNode already runs
    # them concurrently — that's built in, no extra graph wiring required.
    builder.add_node(
        "tools", ToolNode(domain_tools, handle_tool_errors=_friendly_tool_error)
    )
    builder.add_node(
        "human_approval", _instrumented("human_approval")(human_approval)
    )
    # Safety budget node: rejects an over-large batch of tool calls the
    # same way human_approval rejects a disapproved one, then loops back
    # to agent — see MAX_TOOL_CALLS_PER_TURN in should_continue.
    builder.add_node(
        "too_many_tool_calls",
        _instrumented("too_many_tool_calls")(too_many_tool_calls),
    )
    builder.add_node(
        "invalid_tool_call",
        _instrumented("invalid_tool_call")(invalid_tool_call),
    )
    builder.add_node(
        "use_skill_without_search",
        _instrumented("use_skill_without_search")(use_skill_without_search),
    )
    builder.add_node("check_output", _instrumented("check_output")(domain_check_output))
    builder.add_node("retry_output", _instrumented("retry_output")(retry_output))
    builder.add_node(
        "retry_exhausted",
        _instrumented("retry_exhausted")(make_retry_exhausted_node(emit_no_answer_message)),
    )
    builder.add_node(
        "no_answer",
        _instrumented("no_answer")(
            make_no_answer_fallback_node(emit_no_answer_message, system_prompt=manifest.system_prompt)
        ),
    )
    builder.add_node(
        "suggest_followups", _instrumented("suggest_followups")(suggest_followups)
    )
    builder.add_node(
        "write_semantic_cache",
        _instrumented("write_semantic_cache")(write_semantic_cache),
    )

    builder.add_edge(START, "validate_input")
    builder.add_conditional_edges("validate_input", route_after_validation)
    builder.add_edge("reject_input", END)
    builder.add_edge("reject_context", END)
    builder.add_conditional_edges("compact_history", route_after_compaction)
    builder.add_edge("context_window_exceeded", END)

    builder.add_conditional_edges("moderate_input", route_after_moderation)
    builder.add_edge("reject_moderation", END)

    builder.add_conditional_edges("check_semantic_cache", route_after_cache)
    builder.add_edge("retrieve_context", "agent")
    builder.add_conditional_edges("agent", domain_should_continue)
    builder.add_conditional_edges("human_approval", route_after_approval)
    builder.add_edge("tools", "agent")
    builder.add_edge("too_many_tool_calls", "agent")
    builder.add_edge("invalid_tool_call", "agent")
    builder.add_edge("use_skill_without_search", "agent")

    builder.add_conditional_edges("check_output", route_after_check)
    builder.add_edge("retry_output", "agent")
    builder.add_edge("retry_exhausted", END)
    builder.add_edge("no_answer", END)
    builder.add_edge("suggest_followups", "write_semantic_cache")
    builder.add_edge("write_semantic_cache", END)

    compiled = builder.compile(checkpointer=checkpointer or MemorySaver())
    # Not LangGraph API — a plain attribute stash so a caller holding the
    # compiled graph (chiefly app/agent/runtime.py's _ensure_seeded_async) can recover
    # which domain built it, and seed the CORRECT system prompt, without
    # this function's return type changing for every existing call site.
    compiled.manifest = manifest  # type: ignore[attr-defined]  # deliberate stash, see comment above
    return compiled
