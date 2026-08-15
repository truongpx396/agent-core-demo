"""Interactive CLI for the LangGraph agent.

Thin front-end over `app.agent` — demonstrates streaming, memory (thread_id),
and Langfuse tracing. The same runtime backs the FastAPI service (`app.api`).

Two modes:
  make chat         → sync streaming  (simple, dev-friendly)
  make chat-stream  → async astream_events  (production pattern, shows tools)

`--stream` combines with `--hitl` to gate every tool call behind an
approve/reject prompt (see graph.py's human_approval / app/hitl_demo.py) —
the streaming counterpart to hitl_demo.py's blocking pause/resume loop.

Run with: `make chat`, `make chat-stream`, or
`python -m app.chat --stream --hitl`
"""
import asyncio
import sys
import uuid

from langfuse.decorators import langfuse_context

from app.agent import astream_events_resume, astream_events_turn, stream_turn


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
#   token             → printed inline, no newline (typing effect)
#   tool_start        → "[▶ search_docs({args})]" in dim colour
#   tool_end          → "[ search_docs done]"
#   approval_required → pending tool calls listed, returns control to the
#                        caller so it can prompt and resume (--hitl only)
#   done              → final newline
#   error             → printed in red

_DIM   = "\033[2m"
_RESET = "\033[0m"
_RED   = "\033[31m"


def _format_call(name: str, args: dict) -> str:
    args_str = ", ".join(f"{k}={v!r}" for k, v in args.items())
    return f"{name}({args_str})"


async def _render_stream(events) -> bool:
    """Render one astream_events_turn/astream_events_resume call's events.

    Returns True if the run paused awaiting tool-call approval (the caller
    should prompt and call astream_events_resume), False once the turn has
    actually finished (or errored)."""
    async for event in events:
        t = event["type"]
        if t == "token":
            print(event["content"], end="", flush=True)
        elif t == "tool_start":
            call = _format_call(event["tool"], event.get("args", {}))
            print(f"\n{_DIM}  [▶ {call}]{_RESET}", flush=True)
        elif t == "tool_end":
            print(f"{_DIM}  [✓ {event['tool']} done]{_RESET}", flush=True)
        elif t == "approval_required":
            print(f"\n{_DIM}  [pending approval:]{_RESET}")
            for tc in event["tool_calls"]:
                print(f"{_DIM}    {_format_call(tc['name'], tc['args'])}{_RESET}")
            return True
        elif t == "error":
            print(f"\n{_RED}  [error: {event['content']}]{_RESET}", flush=True)
        elif t == "done":
            print()
    return False


async def _async_turn(text: str, thread_id: str, hitl: bool = False) -> None:
    print("bot> ", end="", flush=True)
    needs_approval = await _render_stream(
        astream_events_turn(text, thread_id, require_approval=hitl)
    )
    while needs_approval:
        decision = input("Approve? [y/N] ").strip().lower() == "y"
        needs_approval = await _render_stream(astream_events_resume(thread_id, decision))


async def async_main(hitl: bool = False) -> None:
    """Production streaming CLI: token-level latency + visible tool indicators.

    `hitl=True` gates every tool call behind an approve/reject prompt (see
    graph.py's human_approval / app/hitl_demo.py) — same opt-in mechanism,
    driven through the streaming event protocol instead of a blocking
    graph.invoke() call.
    """
    thread_id = str(uuid.uuid4())
    mode = " with HITL tool approval" if hitl else ""
    print(f"Production streaming agent ready{mode} (astream_events v2). Type 'exit' to quit.\n")
    while True:
        try:
            text = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if text.lower() in {"exit", "quit"}:
            break
        if not text:
            continue
        await _async_turn(text, thread_id, hitl=hitl)

    try:
        langfuse_context.flush()
    except Exception:  # noqa: BLE001
        pass
    print("Bye!")


if __name__ == "__main__":
    if "--stream" in sys.argv:
        asyncio.run(async_main(hitl="--hitl" in sys.argv))
    else:
        main()
