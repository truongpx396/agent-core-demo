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
"""
from typing import Annotated, Literal, TypedDict

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
)
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode, tools_condition

from app.config import CHAT_MODEL, OPENAI_API_BASE, OPENAI_API_KEY
from app.tools import TOOLS, search_docs

SYSTEM_PROMPT = (
    "You are a helpful assistant. Use the search_docs tool to answer questions "
    "about LangGraph, Qdrant, or Acme Corp. Use the calculator tool for math. "
    "If no documents are relevant, answer from general knowledge. "
    "Be concise and direct."
)

MAX_ITERATIONS = 10
MIN_ANSWER_LENGTH = 10


class State(TypedDict):
    """Richer state: track messages, context, and flow control."""

    messages: Annotated[list[BaseMessage], add_messages]
    context: str  # Enriched context from search (set by retrieve_context).
    iterations: int  # Track how many agent loops we've done.


def _last_human_message(messages: list[BaseMessage]) -> HumanMessage | None:
    return next((m for m in reversed(messages) if isinstance(m, HumanMessage)), None)


def _make_llm():
    return ChatOpenAI(
        model=CHAT_MODEL,
        base_url=OPENAI_API_BASE,
        api_key=OPENAI_API_KEY,
        temperature=0,
    ).bind_tools(TOOLS)


def build_graph():
    llm = _make_llm()

    # --- Node: validate input (pass-through; the decision lives in the
    # conditional edge below, so this graph can actually terminate early
    # instead of always falling through to retrieve_context/agent). ---
    def validate_input(state: State) -> dict:
        return {}

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
        return {"messages": [response], "iterations": state.get("iterations", 0) + 1}

    # --- Edge fn: after agent, route to tools / output check / abort ---
    def should_continue(state: State) -> Literal["tools", "check_output", "__end__"]:
        """Did the LLM call a tool, give a final answer, or hit the loop cap?"""
        if state.get("iterations", 0) >= MAX_ITERATIONS:
            return "__end__"
        result = tools_condition(state)
        return result if result == "tools" else "check_output"

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
        return {
            "messages": [
                HumanMessage(
                    content="That answer was too short — please give a fuller answer."
                )
            ]
        }

    # --- Build the graph ---
    builder = StateGraph(State)

    builder.add_node("validate_input", validate_input)
    builder.add_node("reject_input", reject_input)
    builder.add_node("retrieve_context", retrieve_context)
    builder.add_node("agent", agent)
    builder.add_node("tools", ToolNode(TOOLS))
    builder.add_node("check_output", check_output)
    builder.add_node("retry_output", retry_output)

    builder.add_edge(START, "validate_input")
    builder.add_conditional_edges("validate_input", route_after_validation)
    builder.add_edge("reject_input", END)

    builder.add_edge("retrieve_context", "agent")
    builder.add_conditional_edges("agent", should_continue)
    builder.add_edge("tools", "agent")

    builder.add_conditional_edges("check_output", route_after_check)
    builder.add_edge("retry_output", "agent")

    return builder.compile(checkpointer=MemorySaver())
