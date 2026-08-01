"""Shared agent runtime used by BOTH the CLI and the FastAPI service.

Centralises: the compiled graph (singleton), per-thread system-prompt seeding,
Langfuse callbacks, and the two entry points `stream_turn` (CLI, streaming) and
`answer` (API, single response). Keeping this in one place means memory and
tracing behave identically no matter which front-end is used.
"""
from langchain_core.messages import HumanMessage, SystemMessage
from langfuse.decorators import langfuse_context, observe

from app.graph import SYSTEM_PROMPT, build_graph

try:
    from langfuse.callback import CallbackHandler
except Exception:  # noqa: BLE001 - Langfuse optional if keys unset
    CallbackHandler = None

_graph = None
_seeded: set[str] = set()


def get_graph():
    """Compile the LangGraph agent once and reuse it (shares MemorySaver)."""
    global _graph
    if _graph is None:
        _graph = build_graph()
    return _graph


def _callbacks(session_id: str):
    if CallbackHandler is None:
        return []
    try:
        return [CallbackHandler(session_id=session_id)]
    except Exception:  # noqa: BLE001
        return []


def _ensure_seeded(graph, thread_id: str) -> None:
    """Seed a new conversation thread with the system prompt exactly once."""
    if thread_id in _seeded:
        return
    graph.update_state(
        {"configurable": {"thread_id": thread_id}},
        {"messages": [SystemMessage(content=SYSTEM_PROMPT)]},
    )
    _seeded.add(thread_id)


def _config(thread_id: str) -> dict:
    return {
        "configurable": {"thread_id": thread_id},
        "callbacks": _callbacks(thread_id),
        "recursion_limit": 12,  # safety net so a weak model can't loop forever
    }


@observe(name="chat-turn-stream")
def stream_turn(text: str, thread_id: str):
    """Yield the assistant's answer incrementally (used by the CLI)."""
    langfuse_context.update_current_trace(session_id=thread_id)
    graph = get_graph()
    _ensure_seeded(graph, thread_id)
    printed = ""
    for state in graph.stream(
        {"messages": [HumanMessage(content=text)]},
        config=_config(thread_id),
        stream_mode="values",
    ):
        msg = state["messages"][-1]
        if msg.type == "ai" and isinstance(msg.content, str) and msg.content:
            delta = msg.content[len(printed):]
            printed = msg.content
            yield delta


@observe(name="chat-turn")
def answer(text: str, thread_id: str) -> str:
    """Run one turn and return the final assistant text (used by the API)."""
    langfuse_context.update_current_trace(session_id=thread_id)
    graph = get_graph()
    _ensure_seeded(graph, thread_id)
    result = graph.invoke(
        {"messages": [HumanMessage(content=text)]},
        config=_config(thread_id),
    )
    return result["messages"][-1].content
