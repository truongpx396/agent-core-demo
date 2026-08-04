"""Interactive CLI for the LangGraph agent.

Thin front-end over `app.agent` — demonstrates streaming, memory (thread_id),
and Langfuse tracing. The same runtime backs the FastAPI service (`app.api`).

Two modes:
  make chat         → sync streaming  (simple, dev-friendly)
  make chat-stream  → async astream_events  (production pattern, shows tools)

Run with: `make chat` or `make chat-stream`
"""
import asyncio
import sys
import uuid

from langfuse.decorators import langfuse_context

from app.agent import astream_events_turn, stream_turn


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


# ---------------------------------------------------------------------------
# Production streaming CLI  (`make chat-stream`)
# ---------------------------------------------------------------------------
# Uses astream_events v2: prints raw tokens as they arrive AND shows a visible
# indicator whenever a tool is called, so you can see the agent "thinking".
#
# Event rendering:
#   token      → printed inline, no newline (typing effect)
#   tool_start → "[▶ search_docs({args})]" in dim colour
#   tool_end   → "[ search_docs done]"
#   done       → final newline
#   error      → printed in red

_DIM   = "\033[2m"
_RESET = "\033[0m"
_RED   = "\033[31m"


async def _async_turn(text: str, thread_id: str) -> None:
    print("bot> ", end="", flush=True)
    async for event in astream_events_turn(text, thread_id):
        t = event["type"]
        if t == "token":
            print(event["content"], end="", flush=True)
        elif t == "tool_start":
            args_str = ", ".join(f"{k}={v!r}" for k, v in event.get("args", {}).items())
            print(f"\n{_DIM}  [▶ {event['tool']}({args_str})]{_RESET}", flush=True)
        elif t == "tool_end":
            print(f"{_DIM}  [✓ {event['tool']} done]{_RESET}", flush=True)
        elif t == "error":
            print(f"\n{_RED}  [error: {event['content']}]{_RESET}", flush=True)
        elif t == "done":
            print()


async def async_main() -> None:
    """Production streaming CLI: token-level latency + visible tool indicators."""
    thread_id = str(uuid.uuid4())
    print("Production streaming agent ready  (astream_events v2). Type 'exit' to quit.\n")
    while True:
        try:
            text = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if text.lower() in {"exit", "quit"}:
            break
        if not text:
            continue
        await _async_turn(text, thread_id)

    try:
        langfuse_context.flush()
    except Exception:  # noqa: BLE001
        pass
    print("Bye!")


if __name__ == "__main__":
    if "--stream" in sys.argv:
        asyncio.run(async_main())
    else:
        main()
