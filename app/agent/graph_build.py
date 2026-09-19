"""`build_graph()` — compiles the main conversation graph. Split out of
`graph.py` (which still holds `State`, every node function/factory, and
the shared assembly helper) purely for file size — see graph.py's module
docstring. Sibling splits: `graph_routing.py` (should_continue/
check_output) and `graph_build_subagent.py` (build_subagent_graph(), the
nested-run counterpart). No behavior change from the pre-split file.
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
    moderate_input,
    reject_context,
    reject_input,
    reject_moderation,
    route_after_compaction,
    route_after_moderation,
    route_after_validation,
    validate_input,
)
from app.agent.graph_cache import (
    make_check_semantic_cache_node,
    make_write_semantic_cache_node,
    route_after_cache,
)
from app.agent.graph_compaction import make_compact_history_node
from app.agent.graph_followups import make_suggest_followups_node
from app.agent.graph_hitl import human_approval, route_after_approval
from app.agent.graph_retry import (
    make_no_answer_fallback_node,
    make_retry_exhausted_node,
    retry_output,
)
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

    `deps` bundles swappable external clients (LLM, search) — see
    GraphDeps; tests inject fakes via a GraphDeps to run full scenarios
    (reject path, tool loop, HITL, iteration cap, retry) without a live
    model or Qdrant (tests/agent/test_graph_integration.py).

    `checkpointer` defaults to an in-memory MemorySaver — fine for tests,
    never for a real HITL pause: a paused human_approval gate parks the run
    indefinitely, and MemorySaver doesn't survive a process restart.
    runtime.py's init_graph_async() passes a durable AsyncPostgresSaver
    instead for the CLI/API singleton.

    `manifest`/`domain` (pattern 23, app/agent/manifest.py) let this SAME
    function serve a different domain (system prompt, tools, capability
    mapping, policy) with no branching in this function. Default to
    `DEFAULT_MANIFEST`/`DEFAULT_DOMAIN_PLUGIN`, imported here (not at module
    level) to avoid a circular import — see manifest.py's docstring before
    hoisting it. `deps.search_docs`/`cache_get`/`cache_set` remain the
    override points for a domain needing a different corpus or cache.

    `max_iterations`/`max_tokens_per_turn`/`max_cost_usd_per_turn` default
    to `None` (this module's own MAX_ITERATIONS/MAX_TOKENS_PER_TURN/
    MAX_COST_USD_PER_TURN). `tools.py::run_subagent` is the one caller that
    sets them, to MAX_SUBAGENT_ITERATIONS/_TOKENS_PER_RUN/_COST_USD_PER_RUN
    (pattern 46), so a nested run is bounded by its own ceiling.

    `emit_no_answer_message` (default True) controls whether `no_answer`
    AND `retry_exhausted` (both "ended without a real answer" terminal
    paths) fill an empty final AIMessage with a user-facing fallback.
    `run_subagent` sets this False: it substitutes its own
    differently-worded message and outcome tag, so the real fallback text
    would be wrongly read as the subagent's own genuine answer.

    `history_token_ceiling`/`history_token_floor` default to `None`
    (HISTORY_TOKEN_CEILING/FLOOR). No production caller overrides these;
    they let tests exercise compact_history's hysteresis with small,
    controlled budgets instead of thousands of tokens of placeholder text.
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
    # Read as graph_module.X, not a bare imported name — tests/conftest.py's
    # autouse mock_semantic_cache fixture monkeypatches these exact
    # attributes on the live `app.agent.graph` module object; a statically
    # imported bare name binds to the original function once, at import
    # time, so the monkeypatch would silently never take effect. Real bug,
    # caught by the test suite before this fix: a fake-LLM test got back a
    # stale cached answer from an unrelated test's real cache write.
    check_semantic_cache = make_check_semantic_cache_node(
        deps.cache_get or graph_module._default_cache_get
    )
    write_semantic_cache = make_write_semantic_cache_node(
        deps.cache_set or graph_module._default_cache_set
    )

    builder = StateGraph(State)

    # Every node below is wrapped in _instrumented(name) at registration
    # time, not by editing the node functions themselves (pattern 14).
    # Plain module-level functions stay undecorated so they're directly
    # callable from tests; `agent`/`retrieve_context` are the two
    # factory-built exceptions above.
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
    # Reliability policy: retry a transient LLM-endpoint failure a few
    # times before giving up — see AGENT_RETRY_POLICY. Nothing else here
    # gets one: `tools` recovers via handle_tool_errors below, and every
    # other node is a pure function of state where a retry would just
    # repeat the same bug (pattern 7).
    builder.add_node("agent", _instrumented("agent")(agent), retry=AGENT_RETRY_POLICY)
    # Error recovery: a failing tool (e.g. Qdrant unreachable) doesn't
    # crash the run — handle_tool_errors turns the exception into a
    # ToolMessage so `agent` can react on the next turn instead of the
    # graph blowing up. Parallel: ToolNode already runs multiple tool_calls
    # from one AIMessage concurrently — no extra wiring needed.
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
    # compiled graph (chiefly runtime.py's _ensure_seeded_async) can
    # recover which domain built it, without changing this function's
    # return type.
    compiled.manifest = manifest  # type: ignore[attr-defined]  # deliberate stash, see comment above
    return compiled
