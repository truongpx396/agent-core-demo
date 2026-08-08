"""Runnable demo of LangGraph's human-in-the-loop `interrupt()` pattern.

app/chat.py and app/api.py never set `require_approval`, so tool calls run
immediately for them — the human_approval gate in app/graph.py is opt-in and
doesn't change their behavior. This script is what a front-end has to do
*when it does* opt in: drive the pause/resume cycle by hand.

Needs the same live backends as the rest of the app (LiteLLM + Qdrant), since
it calls the real build_graph().

Run with: `python -m app.hitl_demo "what is 12 * 7?"`
"""
import sys
import uuid

from langchain_core.messages import HumanMessage
from langgraph.types import Command

from app.graph import build_graph


def run(question: str) -> None:
    graph = build_graph()
    config = {"configurable": {"thread_id": str(uuid.uuid4())}}

    result = graph.invoke(
        {"messages": [HumanMessage(content=question)], "require_approval": True},
        config=config,
    )

    # `state.next` is non-empty while the graph is paused at an interrupt().
    state = graph.get_state(config)
    while state.next:
        pending = state.tasks[0].interrupts[0].value
        print(f"\nPending tool calls: {pending['tool_calls']}")
        decision = input("Approve? [y/N] ").strip().lower() == "y"
        result = graph.invoke(Command(resume=decision), config=config)
        state = graph.get_state(config)

    print(f"\nFinal answer: {result['messages'][-1].content}")


if __name__ == "__main__":
    run(" ".join(sys.argv[1:]) or "What is 12 * 7?")
