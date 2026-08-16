"""Runnable demo of LangGraph's human-in-the-loop `interrupt()` pattern.

app/chat.py and app/api.py never set `require_approval`, so a read-only
tool call runs immediately for them — human_approval is opt-in for those
(graph.py's should_continue). This script is what a front-end has to do
*when it does* opt in, or whenever a mutating tool (e.g. add_note) forces
the gate unconditionally: drive the pause/resume cycle by hand.

Uses app.agent's shared, DURABLE graph (app.agent.get_graph(), backed by
AsyncSqliteSaver — see its module docstring) rather than calling
build_graph() directly, so a pause created here survives a restart exactly
as it would from the CLI or API, and resumability_error's cross-restart
check below is actually exercising something real: pause this script (^C
after the approval prompt appears, or answer with an unrecognized decision
and re-run pointed at the same thread_id), and the pending approval is
still there.

Needs the same live backends as the rest of the app (LiteLLM + Qdrant), since
it calls the real graph.

Run with: `python -m app.hitl_demo "what is 12 * 7?"`
"""
import sys
import uuid

from langchain_core.messages import HumanMessage
from langgraph.types import Command

from app.agent import get_graph
from app.graph import resumability_error


def run(question: str, thread_id: str | None = None) -> None:
    graph = get_graph()
    config = {"configurable": {"thread_id": thread_id or str(uuid.uuid4())}}

    result = graph.invoke(
        {"messages": [HumanMessage(content=question)], "require_approval": True},
        config=config,
    )

    # `state.next` is non-empty while the graph is paused at an interrupt().
    state = graph.get_state(config)
    while state.next:
        pending = state.tasks[0].interrupts[0].value
        print(f"\nPending tool calls: {pending['tool_calls']}")
        print(f"(thread_id: {config['configurable']['thread_id']})")

        # Never resume blindly (see resumability_error's docstring) — the
        # checkpoint could belong to a build that no longer matches this
        # one if enough time passed between the pause above and this line.
        error = resumability_error(graph, config)
        if error:
            print(f"\nCannot resume: {error}")
            return

        decision = input("Approve? [y/N] ").strip().lower() == "y"
        result = graph.invoke(Command(resume=decision), config=config)
        state = graph.get_state(config)

    print(f"\nFinal answer: {result['messages'][-1].content}")


if __name__ == "__main__":
    run(" ".join(sys.argv[1:]) or "What is 12 * 7?")
