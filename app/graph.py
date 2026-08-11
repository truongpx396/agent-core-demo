"""Enhanced LangGraph agent demonstrating practical patterns.

Shows real-world scenarios beyond basic "LLM + tools" loop:
- Input validation: a real conditional exit for bad input, not just a
  message appended and hoped for the best.
- Context enrichment: fetch relevant docs *before* reasoning (multi-step),
  and actually pass that context to the LLM.
- State tracking: iterations, context, enriched messages.
- Loop control: max iterations to prevent infinite loops.
- Output quality gate: a conditional node that can send the answer back to
  the agent for a retry, not a pass-through that always ends.
- Error recovery: tool exceptions become a message the agent can react to,
  instead of crashing the whole run.
- Human-in-the-loop: an opt-in `interrupt()` gate before tool execution
  (see app/hitl_demo.py for a runnable end-to-end example).
- Parallel tool execution: ToolNode already runs every tool call from one
  LLM turn concurrently — no extra code needed (see comment at its node).

Nodes and routing functions live at module level (not nested inside
build_graph) specifically so they can be unit-tested directly — imported
and called with a hand-built `state` dict — without compiling a graph or
touching a real LLM. See tests/ for the corresponding test-per-layer
suite (routing functions, nodes, agent node, full graph scenarios).
"""
from typing import Annotated, Literal, TypedDict

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode, tools_condition
from langgraph.types import interrupt

from app.config import CHAT_MODEL, OPENAI_API_BASE, OPENAI_API_KEY
from app.tools import TOOLS, search_docs
from app import metrics

SYSTEM_PROMPT = (
    "You are a helpful assistant. Use the search_docs tool to answer questions "
    "about LangGraph, Qdrant, or Acme Corp. Use the calculator tool for math. "
    "If no documents are relevant, answer from general knowledge. "
    "Be concise and direct."
)

MAX_ITERATIONS = 10  # safety budget: LLM loop iterations, per turn (see validate_input's reset)
MIN_ANSWER_LENGTH = 10
MAX_TOOL_CALLS_PER_TURN = 5  # safety budget: simultaneous tool calls from one LLM turn
MAX_TOKENS_PER_TURN = 8000  # safety budget: cumulative token usage, per turn (0 if the model/proxy doesn't report usage_metadata — fails open, not closed)


class State(TypedDict):
    """Richer state: track messages, context, and flow control."""

    messages: Annotated[list[BaseMessage], add_messages]
    context: str  # Enriched context from search (set by retrieve_context).
    iterations: int  # Track how many agent loops we've done *this turn*.
    total_tokens: int  # Cumulative token usage *this turn* (see agent()).
    require_approval: bool  # Opt-in: gate tool calls behind human_approval.
    approved: bool  # Set by human_approval; read by route_after_approval.


def _last_human_message(messages: list[BaseMessage]) -> HumanMessage | None:
    return next((m for m in reversed(messages) if isinstance(m, HumanMessage)), None)


def _friendly_tool_error(error: Exception) -> str:
    """Turned into a ToolMessage by ToolNode's handle_tool_errors instead of
    propagating and killing the run — the agent sees this on its next turn."""
    return f"Tool failed ({type(error).__name__}: {error}). Try a different approach."


def _make_llm():
    return ChatOpenAI(
        model=CHAT_MODEL,
        base_url=OPENAI_API_BASE,
        api_key=OPENAI_API_KEY,
        temperature=0,
    ).bind_tools(TOOLS)


# --- Node: validate input. Also resets the per-turn safety budgets
# (iterations, total_tokens): validate_input is the fixed entry point for
# every graph.invoke() call (START -> validate_input, always), but is
# *not* re-run when resuming a paused HITL turn via Command(resume=...) —
# that resumes inside human_approval directly. So this runs exactly once
# per conversation turn, which is what "per turn" budgets need: without
# this reset, `iterations`/`total_tokens` persist in the checkpointed
# state and keep climbing turn over turn, so MAX_ITERATIONS would
# eventually end the graph on a random future turn regardless of how much
# work that turn actually did.
def validate_input(state: State) -> dict:
    return {"iterations": 0, "total_tokens": 0}


def route_after_validation(
    state: State,
) -> Literal["retrieve_context", "reject_input"]:
    last_human = _last_human_message(state["messages"])
    if last_human is None or not last_human.content.strip():
        return "reject_input"
    return "retrieve_context"


def reject_input(state: State) -> dict:
    # AIMessage, not HumanMessage: the *system* is producing this text,
    # not the user.
    return {
        "messages": [
            AIMessage(content="I didn't receive a question — please try again.")
        ]
    }


# --- Node: enrich context (multi-step pattern) ---
def retrieve_context(state: State) -> dict:
    """Fetch relevant docs *before* the agent reasons.

    In production, you might fetch from a database, call an API, etc.
    """
    last_human = _last_human_message(state["messages"])
    if last_human is None:
        return {"context": ""}
    return {"context": search_docs(last_human.content)}


# --- Node: agent ---
def make_agent_node(llm):
    """Factory, not a plain function, because `agent` needs an LLM client.

    Tests build it with a fake (e.g. GenericFakeChatModel) via
    `make_agent_node(fake_llm)(state)` — see tests/test_agent_node.py — so the
    node's message-assembly logic (context injection, iteration bump) is
    covered without a network call to a real model.
    """

    def agent(state: State) -> dict:
        """Call the LLM, injecting retrieved context as an extra SystemMessage.

        The *base* SYSTEM_PROMPT is seeded once per thread by
        app/agent.py::_ensure_seeded before the graph ever runs, so we don't
        repeat it here — we only add the per-turn retrieved context, which
        actually needs to reach the model on every agent step.
        """
        messages = list(state["messages"])
        context = state.get("context", "")
        if context:
            messages.append(SystemMessage(content=f"Relevant documents:\n{context}"))

        response = llm.invoke(messages)

        # Token budget bookkeeping: usage_metadata is populated when the
        # underlying model/proxy reports it (not guaranteed — e.g. depends
        # on Ollama/LiteLLM passing usage through). Missing usage just
        # means the token budget never trips, not an error.
        usage = getattr(response, "usage_metadata", None) or {}
        total_tokens = state.get("total_tokens", 0) + usage.get("total_tokens", 0)

        return {
            "messages": [response],
            "iterations": state.get("iterations", 0) + 1,
            "total_tokens": total_tokens,
        }

    return agent


def _reject_tool_calls(tool_calls: list, reason: str) -> list[ToolMessage]:
    """Every pending tool_call needs a matching ToolMessage, or the next LLM
    call fails OpenAI's tool-response validation — shared by
    human_approval's rejection path and too_many_tool_calls below, which
    both need to abort a batch of tool calls without running them."""
    return [ToolMessage(content=reason, tool_call_id=tc["id"]) for tc in tool_calls]


# --- Edge fn: after agent, route to tools / output check / abort ---
def should_continue(
    state: State,
) -> Literal[
    "tools", "human_approval", "too_many_tool_calls", "check_output", "__end__"
]:
    """Did the LLM call a tool, give a final answer, or hit a safety budget?

    Checked in order: the iteration cap and token cap are turn-wide safety
    nets (checked first, regardless of what the LLM just said); then
    whether this is a tool call at all; then whether it's *too many* tool
    calls at once; then whether it needs human approval before running.

    When `require_approval` is set on the input state, tool calls are
    routed through `human_approval` first instead of running directly.
    It's opt-in (default False) so the existing CLI/API behavior is
    unchanged unless a caller asks for it — see app/hitl_demo.py.
    """
    if state.get("iterations", 0) >= MAX_ITERATIONS:
        return "__end__"
    if state.get("total_tokens", 0) >= MAX_TOKENS_PER_TURN:
        metrics.agent_token_budget_exceeded_total.inc()
        return "__end__"
    result = tools_condition(state)
    if result != "tools":
        return "check_output"
    tool_calls = state["messages"][-1].tool_calls or []
    if len(tool_calls) > MAX_TOOL_CALLS_PER_TURN:
        return "too_many_tool_calls"
    return "human_approval" if state.get("require_approval") else "tools"


# --- Node: safety budget — abort a turn where the LLM asked for more tool
# calls at once than MAX_TOOL_CALLS_PER_TURN allows (e.g. a confused model
# fanning out into dozens of searches), instead of running all of them. ---
def too_many_tool_calls(state: State) -> dict:
    last_ai = state["messages"][-1]
    tool_calls = last_ai.tool_calls or []
    metrics.agent_tool_budget_exceeded_total.inc()
    rejections = _reject_tool_calls(
        tool_calls,
        f"Too many tool calls requested at once (limit: {MAX_TOOL_CALLS_PER_TURN}). "
        "Please make fewer, more targeted tool calls.",
    )
    return {"messages": rejections}


# --- Node: human-in-the-loop approval gate (opt-in) ---
def human_approval(state: State) -> dict:
    """Pause the graph and ask a human to approve pending tool calls.

    `interrupt()` suspends execution here (LangGraph persists state via
    the checkpointer); the caller resumes with
    `graph.invoke(Command(resume=True_or_False), config)`, at which
    point `interrupt()` returns that value and this node continues.
    """
    last_ai = state["messages"][-1]
    tool_calls = last_ai.tool_calls or []
    approved = interrupt(
        {
            "action": "approve_tool_calls",
            "tool_calls": [
                {"name": tc["name"], "args": tc["args"]} for tc in tool_calls
            ],
        }
    )
    if approved:
        metrics.agent_human_approval_total.labels(decision="approved").inc()
        return {"approved": True}
    metrics.agent_human_approval_total.labels(decision="rejected").inc()
    rejections = _reject_tool_calls(tool_calls, "Rejected by human reviewer.")
    return {"messages": rejections, "approved": False}


def route_after_approval(state: State) -> Literal["tools", "agent"]:
    return "tools" if state.get("approved") else "agent"


# --- Node: check output (pass-through; the decision lives below) ---
def check_output(state: State) -> dict:
    return {}


def route_after_check(state: State) -> Literal["retry_output", "__end__"]:
    last = state["messages"][-1]
    content = getattr(last, "content", "") or ""
    if isinstance(content, str) and len(content) < MIN_ANSWER_LENGTH:
        return "retry_output"
    return "__end__"


def retry_output(state: State) -> dict:
    """Send the agent back with corrective feedback instead of just
    looping on the exact same messages. MAX_ITERATIONS in should_continue
    still bounds the total number of retries."""
    metrics.agent_retry_total.inc()
    return {
        "messages": [
            HumanMessage(
                content="That answer was too short — please give a fuller answer."
            )
        ]
    }


# --- Build the graph ---
def build_graph(llm=None):
    """Compile the graph.

    `llm` defaults to the real ChatOpenAI client (`_make_llm()`), but tests
    pass a fake chat model here to run full graph scenarios — reject path,
    tool loop, HITL approve/reject, iteration cap, retry — without hitting
    a live model. See tests/test_graph_integration.py.
    """
    llm = llm or _make_llm()
    agent = make_agent_node(llm)

    builder = StateGraph(State)

    builder.add_node("validate_input", validate_input)
    builder.add_node("reject_input", reject_input)
    builder.add_node("retrieve_context", retrieve_context)
    builder.add_node("agent", agent)
    # Error recovery: a failing tool (e.g. Qdrant unreachable) doesn't crash
    # the run — handle_tool_errors turns the exception into a ToolMessage so
    # the agent node sees it on the next turn and can react (apologize, fall
    # back to general knowledge, etc.) instead of the graph blowing up.
    #
    # Parallel tool execution: if the LLM returns multiple tool_calls in one
    # AIMessage (e.g. "search docs AND compute 12*7"), ToolNode already runs
    # them concurrently — that's built in, no extra graph wiring required.
    builder.add_node(
        "tools", ToolNode(TOOLS, handle_tool_errors=_friendly_tool_error)
    )
    builder.add_node("human_approval", human_approval)
    # Safety budget node: rejects an over-large batch of tool calls the
    # same way human_approval rejects a disapproved one, then loops back
    # to agent — see MAX_TOOL_CALLS_PER_TURN in should_continue.
    builder.add_node("too_many_tool_calls", too_many_tool_calls)
    builder.add_node("check_output", check_output)
    builder.add_node("retry_output", retry_output)

    builder.add_edge(START, "validate_input")
    builder.add_conditional_edges("validate_input", route_after_validation)
    builder.add_edge("reject_input", END)

    builder.add_edge("retrieve_context", "agent")
    builder.add_conditional_edges("agent", should_continue)
    builder.add_conditional_edges("human_approval", route_after_approval)
    builder.add_edge("tools", "agent")
    builder.add_edge("too_many_tool_calls", "agent")

    builder.add_conditional_edges("check_output", route_after_check)
    builder.add_edge("retry_output", "agent")

    return builder.compile(checkpointer=MemorySaver())
