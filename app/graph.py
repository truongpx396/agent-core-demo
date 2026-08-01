"""The LangGraph agent.

Demonstrates the core LangGraph features:
- a typed State with a message reducer,
- an LLM node with tools bound (ReAct-style),
- a conditional edge that loops LLM <-> tools,
- a MemorySaver checkpointer for per-thread conversation memory.
"""
from typing import Annotated, TypedDict

from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode, tools_condition

from app.config import CHAT_MODEL, OPENAI_API_BASE, OPENAI_API_KEY
from app.tools import TOOLS

SYSTEM_PROMPT = (
    "You are a helpful assistant. Use the search_docs tool to answer questions "
    "about LangGraph, Qdrant, or Acme Corp. Use the calculator tool for math. "
    "If no documents are relevant, answer from general knowledge."
)


class State(TypedDict):
    messages: Annotated[list, add_messages]


def _make_llm():
    return ChatOpenAI(
        model=CHAT_MODEL,
        base_url=OPENAI_API_BASE,
        api_key=OPENAI_API_KEY,
        temperature=0,
    ).bind_tools(TOOLS)


def build_graph():
    llm = _make_llm()

    def agent(state: State) -> dict:
        return {"messages": [llm.invoke(state["messages"])]}

    builder = StateGraph(State)
    builder.add_node("agent", agent)
    builder.add_node("tools", ToolNode(TOOLS))
    builder.add_edge(START, "agent")
    # If the LLM asked for a tool -> "tools", else -> END.
    builder.add_conditional_edges("agent", tools_condition)
    builder.add_edge("tools", "agent")
    builder.add_edge("agent", END)

    return builder.compile(checkpointer=MemorySaver())
