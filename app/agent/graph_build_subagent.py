"""`build_subagent_graph()` — the leaner graph assembly for a NESTED
subagent run (`app/agent/tools.py::run_subagent`, GRAPH_PATTERNS.md pattern
46). Split out of `app/agent/graph.py` (which still holds `State`, every
node function/factory, and the shared assembly helper this function calls)
purely for file size — see `app/agent/graph.py`'s own module docstring and
`app/agent/graph_routing.py`'s for the sibling split (`should_continue`/
`check_output`) and `app/agent/graph_build.py` (the main-turn counterpart,
`build_graph()`). No behavior change from the pre-split single-file version.
"""
from typing import TYPE_CHECKING

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from app.agent.graph import (
    AGENT_RETRY_POLICY,
    GraphDeps,
    State,
    _assemble_shared_graph_parts,
    make_no_answer_fallback_node,
    make_retry_exhausted_node,
    moderate_input,
    reject_context,
    reject_input,
    reject_moderation,
    retry_output,
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


# --- Build a leaner graph for a NESTED subagent run (app/agent/tools.py::
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
    (or worse) for a nested, one-shot subagent run — see GRAPH_PATTERNS.md
    pattern 46 for the full reasoning behind each:

    - `check_semantic_cache`/`write_semantic_cache`: dropped entirely, not
      just skipped at runtime — closes a previously-disclosed gap where a
      subagent run checked/wrote the SAME semantic cache the top-level
      conversation uses (keyed by tenant+principal, shared with the
      parent's own ctx): a subagent's cached answer could in theory have
      been served back for a top-level query with near-identical phrasing,
      or vice versa. A subagent run no longer touches that cache at all.
    - `suggest_followups`: dropped — closes the other previously-disclosed
      gap, a full extra LLM call whose result `_run_subagent_impl` simply
      discarded on every completed run.
    - `compact_history`/`context_window_exceeded`: dropped because they're
      PROVABLY inert for a subagent run, not just unlikely to fire —
      `compact_history` only ever does real work once `state["messages"]`
      crosses `HISTORY_TOKEN_CEILING` (24000), but a subagent's entire run
      is hard-capped at `MAX_SUBAGENT_TOKENS_PER_RUN` (4000, see
      `_run_subagent_impl`'s own `max_tokens_per_turn` argument below) —
      mathematically unreachable.

    Deliberately KEPT despite looking like overhead at a glance:
    `moderate_input` (cheap, no LLM call, and the delegated `task` string
    came from the PARENT model, not hand-vetted, so this isn't a check to
    skip for size); `retrieve_context` (a lookup-focused subagent's job is
    generally well served by the same automatic RAG pre-fetch the
    top-level turn gets; never named as a gap, so left alone rather than
    silently changing behavior nobody flagged); every other node
    (`human_approval`, `too_many_tool_calls`, `invalid_tool_call`,
    `use_skill_without_search`, `check_output`, `retry_output`,
    `retry_exhausted`, `no_answer`) is safety- or correctness-critical and
    already cheap regardless of topology size — `human_approval` is
    currently unreachable here (read_only-only tools never trip the
    mandatory gate) but stays as defense-in-depth, the same "no flag turns
    this off" posture pattern 15 already takes for the main graph.

    Reuses `_assemble_shared_graph_parts` for everything this shares with
    `build_graph()` (LLM client construction, the `agent`/`retrieve_context`
    node closures, the `should_continue`/`check_output` partials) — see
    that function's own docstring. Reuses `route_after_validation`/
    `route_after_moderation`/`route_after_check` completely unmodified via
    LangGraph's `path_map` (`StateGraph.add_conditional_edges(source, path,
    path_map=...)`) to remap a routing function's "the full topology would
    go here" return value onto whichever node actually comes next in THIS
    smaller topology — so none of those shared routing functions need to
    know or care which topology they're wired into.

    Parameters mirror `build_graph()`'s own (see its docstring for each),
    minus `history_token_ceiling`/`history_token_floor` — meaningless here,
    since `compact_history` isn't part of this topology at all.
    """
    parts = _assemble_shared_graph_parts(
        deps, manifest, domain, max_iterations, max_tokens_per_turn, max_cost_usd_per_turn
    )
    manifest = parts.manifest

    builder = StateGraph(State)

    builder.add_node("validate_input", _instrumented("validate_input")(validate_input))
    builder.add_node("reject_input", _instrumented("reject_input")(reject_input))
    builder.add_node("reject_context", _instrumented("reject_context")(reject_context))
    builder.add_node("moderate_input", _instrumented("moderate_input")(moderate_input))
    builder.add_node(
        "reject_moderation", _instrumented("reject_moderation")(reject_moderation)
    )
    builder.add_node(
        "retrieve_context", _instrumented("retrieve_context")(parts.retrieve_context)
    )
    builder.add_node("agent", _instrumented("agent")(parts.agent), retry=AGENT_RETRY_POLICY)
    builder.add_node(
        "tools", ToolNode(parts.domain_tools, handle_tool_errors=_friendly_tool_error)
    )
    builder.add_node(
        "human_approval", _instrumented("human_approval")(human_approval)
    )
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
    builder.add_node("check_output", _instrumented("check_output")(parts.domain_check_output))
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

    builder.add_edge(START, "validate_input")
    # route_after_validation's "compact_history" branch has nowhere to go
    # in this topology — compact_history isn't a node here — so it's
    # remapped straight to moderate_input, the node that would have come
    # right after it anyway.
    builder.add_conditional_edges(
        "validate_input",
        route_after_validation,
        {
            "compact_history": "moderate_input",
            "reject_input": "reject_input",
            "reject_context": "reject_context",
        },
    )
    builder.add_edge("reject_input", END)
    builder.add_edge("reject_context", END)

    builder.add_conditional_edges(
        "moderate_input",
        route_after_moderation,
        # Same remap idea: route_after_moderation's "check_semantic_cache"
        # branch goes straight to retrieve_context — check_semantic_cache
        # isn't a node here either.
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
        # route_after_check's "suggest_followups" branch ends the run
        # directly — suggest_followups/write_semantic_cache aren't nodes
        # here, so there's nothing left to do once check_output is happy.
        {"retry_output": "retry_output", "retry_exhausted": "retry_exhausted", "suggest_followups": END},
    )
    builder.add_edge("retry_output", "agent")
    builder.add_edge("retry_exhausted", END)
    builder.add_edge("no_answer", END)

    compiled = builder.compile(checkpointer=checkpointer or MemorySaver())
    compiled.manifest = manifest  # type: ignore[attr-defined]  # same stash build_graph() does, see its own comment
    return compiled
