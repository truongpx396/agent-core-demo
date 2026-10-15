"""`build_subagent_graph()` — the leaner graph assembly for a NESTED
subagent run (`app/agent/tools/builtin.py::run_subagent`, pattern 46). Split out
of `app/agent/graph/core.py` (which still holds `State`, every node function/factory, and
the shared assembly helper) purely for file size — see app/agent/graph/core.py's module
docstring. Sibling splits: `graph/routing.py` (should_continue/
check_output) and `graph/build.py` (build_graph(), the main-turn
counterpart). No behavior change from the pre-split file.
"""
from typing import TYPE_CHECKING

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from app.agent.graph.core import (
    AGENT_RETRY_POLICY,
    GraphDeps,
    State,
    assemble_shared_graph_parts,
    moderate_input,
    reject_context,
    reject_input,
    reject_moderation,
    route_after_moderation,
    route_after_validation,
    validate_input,
)
from app.agent.graph.hitl import human_approval, route_after_approval
from app.agent.graph.retry import (
    make_no_answer_fallback_node,
    make_retry_exhausted_node,
    retry_output,
)
from app.agent.graph.routing import route_after_check
from app.agent.graph.skill_guards import use_skill_without_search
from app.agent.graph.tool_guards import too_many_tool_calls
from app.agent.graph.utils import friendly_tool_error, instrumented

if TYPE_CHECKING:
    from app.agent.manifest import AgentManifest, DomainPlugin


# --- Build a leaner graph for a NESTED subagent run (app/agent/tools/builtin.py::
# run_subagent, GRAPH_PATTERNS.md pattern 46) ---
def build_subagent_graph(
    deps: GraphDeps | None = None,
    checkpointer=None,
    manifest: "AgentManifest | None" = None,
    domain: "DomainPlugin | None" = None,
    max_iterations: int | None = None,
    max_tokens_per_turn: int | None = None,
    max_cost_usd_per_turn: float | None = None,
    emit_no_answer_message: bool = True,
):
    """`build_graph()`'s topology, minus five nodes that are pure overhead
    (or worse) for a nested, one-shot subagent run (pattern 46 has the full
    reasoning):

    - `check_semantic_cache`/`write_semantic_cache`: dropped entirely —
      closes a disclosed gap where a subagent run shared the top-level
      conversation's own cache (keyed by tenant+principal), risking a
      cross-contaminated cache hit in either direction.
    - `suggest_followups`: dropped — closes another disclosed gap, a full
      extra LLM call whose result `run_subagent_impl` simply discarded.
    - `compact_history`/`context_window_exceeded`: dropped because they're
      PROVABLY inert here, not just unlikely — compact_history only fires
      once history crosses HISTORY_TOKEN_CEILING (24000), but a subagent
      run is hard-capped at MAX_SUBAGENT_TOKENS_PER_RUN (4000) —
      mathematically unreachable.

    Deliberately KEPT despite looking like overhead: `moderate_input`
    (cheap, and the delegated `task` string came from the PARENT model, not
    hand-vetted); `retrieve_context` (a lookup-focused subagent is usually
    well served by the same RAG pre-fetch); every other node is safety- or
    correctness-critical and already cheap — `human_approval` is currently
    unreachable here (read_only-only tools never trip the mandatory gate)
    but stays as defense-in-depth (same "no flag turns this off" posture as
    pattern 15 for the main graph).

    Reuses `assemble_shared_graph_parts` for everything shared with
    `build_graph()`, and reuses `route_after_validation`/
    `route_after_moderation`/`route_after_check` unmodified via LangGraph's
    `path_map` to remap their full-topology branches onto whichever node
    actually comes next here.

    Parameters mirror `build_graph()`'s (see its docstring), minus
    `history_token_ceiling`/`history_token_floor` — meaningless since
    `compact_history` isn't part of this topology.
    """
    # enforce_citations=False: a subagent's retrieval cites ITS sources, which mean nothing to
    # the parent, so its prompt says to use no `[n]` markers; the graph must not demand them
    # (tail reminder) or insert them (check_output) either.
    parts = assemble_shared_graph_parts(
        deps,
        manifest,
        domain,
        max_iterations,
        max_tokens_per_turn,
        max_cost_usd_per_turn,
        enforce_citations=False,
    )
    manifest = parts.manifest

    builder = StateGraph(State)

    builder.add_node("validate_input", instrumented("validate_input")(validate_input))
    builder.add_node("reject_input", instrumented("reject_input")(reject_input))
    builder.add_node("reject_context", instrumented("reject_context")(reject_context))
    builder.add_node("moderate_input", instrumented("moderate_input")(moderate_input))
    builder.add_node(
        "reject_moderation", instrumented("reject_moderation")(reject_moderation)
    )
    builder.add_node(
        "retrieve_context", instrumented("retrieve_context")(parts.retrieve_context)
    )
    builder.add_node("agent", instrumented("agent")(parts.agent), retry=AGENT_RETRY_POLICY)
    builder.add_node(
        "tools", ToolNode(parts.domain_tools, handle_tool_errors=friendly_tool_error)
    )
    builder.add_node(
        "human_approval", instrumented("human_approval")(human_approval)
    )
    builder.add_node(
        "too_many_tool_calls",
        instrumented("too_many_tool_calls")(too_many_tool_calls),
    )
    builder.add_node(
        "invalid_tool_call",
        instrumented("invalid_tool_call")(parts.domain_invalid_tool_call),
    )
    builder.add_node(
        "use_skill_without_search",
        instrumented("use_skill_without_search")(use_skill_without_search),
    )
    builder.add_node("check_output", instrumented("check_output")(parts.domain_check_output))
    builder.add_node("retry_output", instrumented("retry_output")(retry_output))
    builder.add_node(
        "retry_exhausted",
        instrumented("retry_exhausted")(make_retry_exhausted_node(emit_no_answer_message)),
    )
    builder.add_node(
        "no_answer",
        instrumented("no_answer")(
            make_no_answer_fallback_node(
                emit_no_answer_message, system_prompt=manifest.system_prompt, enforce_citations=False
            )
        ),
    )

    builder.add_edge(START, "validate_input")
    builder.add_conditional_edges(
        "validate_input",
        route_after_validation,
        {
            "moderate_input": "moderate_input",
            "reject_input": "reject_input",
            "reject_context": "reject_context",
        },
    )
    builder.add_edge("reject_input", END)
    builder.add_edge("reject_context", END)

    builder.add_conditional_edges(
        "moderate_input",
        route_after_moderation,
        # Same remap idea: "check_semantic_cache" goes straight to
        # retrieve_context — not a node here either.
        {"reject_moderation": "reject_moderation", "check_semantic_cache": "retrieve_context"},
    )
    builder.add_edge("reject_moderation", END)

    builder.add_edge("retrieve_context", "agent")
    builder.add_conditional_edges("agent", parts.domain_should_continue)
    builder.add_conditional_edges("human_approval", route_after_approval)
    builder.add_edge("tools", "agent")
    builder.add_edge("too_many_tool_calls", "agent")
    builder.add_edge("invalid_tool_call", "agent")
    builder.add_edge("use_skill_without_search", "agent")

    builder.add_conditional_edges(
        "check_output",
        route_after_check,
        # "suggest_followups" ends the run directly — that node and
        # write_semantic_cache aren't part of this topology.
        {"retry_output": "retry_output", "retry_exhausted": "retry_exhausted", "suggest_followups": END},
    )
    builder.add_edge("retry_output", "agent")
    builder.add_edge("retry_exhausted", END)
    builder.add_edge("no_answer", END)

    compiled = builder.compile(checkpointer=checkpointer or MemorySaver())
    compiled.manifest = manifest  # type: ignore[attr-defined]  # same stash build_graph() does, see its own comment
    return compiled
