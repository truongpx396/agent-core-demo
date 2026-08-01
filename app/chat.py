"""Interactive CLI for the LangGraph agent.

Thin front-end over `app.agent` — demonstrates streaming, memory (thread_id),
and Langfuse tracing. The same runtime backs the FastAPI service (`app.api`).

Run with: `make chat`
"""
import uuid

from langfuse.decorators import langfuse_context

from app.agent import stream_turn


def main() -> None:
    thread_id = str(uuid.uuid4())
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
        for delta in stream_turn(text, thread_id):
            print(delta, end="", flush=True)
        print()

    try:
        langfuse_context.flush()
    except Exception:  # noqa: BLE001
        pass
    print("Bye!")


if __name__ == "__main__":
    main()
