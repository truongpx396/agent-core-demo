"""Interactive CLI for the LangGraph agent.

Demonstrates:
- streaming graph execution (`graph.stream`),
- conversation memory via a per-session thread_id (MemorySaver),
- Langfuse tracing via the LangGraph callback handler + `@observe`,
  with thread_id used as the Langfuse session id.

Run with: `make chat`
"""
import uuid

from langchain_core.messages import HumanMessage, SystemMessage
from langfuse.decorators import langfuse_context, observe

from app.graph import SYSTEM_PROMPT, build_graph

try:
    from langfuse.callback import CallbackHandler
except Exception:  # noqa: BLE001 - Langfuse optional if keys unset
    CallbackHandler = None


def _callbacks(session_id: str):
    if CallbackHandler is None:
        return []
    try:
        return [CallbackHandler(session_id=session_id)]
    except Exception:  # noqa: BLE001
        return []


@observe(name="chat-turn")
def run_turn(graph, text: str, thread_id: str) -> None:
    """Run one user turn, streaming the assistant's latest message."""
    langfuse_context.update_current_trace(session_id=thread_id)
    config = {
        "configurable": {"thread_id": thread_id},
        "callbacks": _callbacks(thread_id),
        "recursion_limit": 12,  # safety net so a weak model can't loop forever
    }
    printed = ""
    for state in graph.stream(
        {"messages": [HumanMessage(content=text)]},
        config=config,
        stream_mode="values",
    ):
        msg = state["messages"][-1]
        # Print assistant text as it grows; skip tool-call bookkeeping messages.
        if msg.type == "ai" and isinstance(msg.content, str) and msg.content:
            delta = msg.content[len(printed):]
            print(delta, end="", flush=True)
            printed = msg.content
    print()


def main() -> None:
    graph = build_graph()
    thread_id = str(uuid.uuid4())

    # Seed the conversation (stored in memory under this thread_id).
    graph.update_state(
        {"configurable": {"thread_id": thread_id}},
        {"messages": [SystemMessage(content=SYSTEM_PROMPT)]},
    )

    print("Local RAG agent ready. Type 'exit' to quit.\n")
    while True:
        try:
            text = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if text.lower() in {"exit", "quit"}:
            break
        if not text:
            continue
        print("bot> ", end="", flush=True)
        run_turn(graph, text, thread_id)

    try:
        langfuse_context.flush()
    except Exception:  # noqa: BLE001
        pass
    print("Bye!")


if __name__ == "__main__":
    main()
