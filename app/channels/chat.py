"""Interactive CLI for the LangGraph agent.

A front door onto the queue, like the HTTP API and the Telegram channel: each turn is published to the
domain's requests stream and the events an `agent_worker.py` publishes back are rendered
(`app/job_queue/producer.py`, pattern 43). This process never runs the graph, so `make chat` needs `make up`'s
Redis and a worker for the domain (`make agent-worker`); without one the turn ends, after
`CHAT_FIRST_RESPONSE_DEADLINE_SECONDS`, with an error that says so. What the worker does for every turn (the
per-thread lock, crash reclaim, tracing, the spend meter) it does for this one.

`AGENT_DOMAIN` (default `ecorp`) picks which domain's worker pool gets the turns: `AGENT_DOMAIN=support make
chat` talks to the support copilot.

Raw tokens print as they arrive, with a visible indicator whenever a tool is called, so you can watch the
agent work; the event stream is the one `astream_events` v2 yields, relayed through Redis.

`--hitl` gates every tool call behind an approve/reject prompt (app/agent/graph/hitl.py's human_approval): a
runnable example of the `interrupt()` pause/resume cycle through the queue, where the turn ends at
`approval_required` and the answer is a `resume` job.

Ctrl-C or end-of-input mid-turn asks the worker to stop it (`cancel_turn`). The turn is no longer this
process's to kill, so without that a worker would run it to the end, including a tool call the person was
about to be asked to approve.

Run with: `make chat`, `make chat-hitl`, or `python -m app.channels.chat --hitl`
"""
import asyncio
import getpass
import sys
import uuid
from contextlib import aclosing

from app.core.config import AGENT_DOMAIN, DEFAULT_TENANT
from app.core.security import SecurityCtx
from app.domains.registry import resolve_domain
from app.job_queue.producer import cancel_turn, stream_resume, stream_turn

# Local dev ctx: this process IS the trusted boundary (no network hop, no
# untrusted client to spoof it), unlike app/api/deps.py's header extraction.
# `tenant` matches DEFAULT_TENANT so the CLI sees what `make ingest` seeded;
# `principal` is the OS user, so multiple people on one machine get separate
# memories (app/agent/tools/builtin.py's `remember`).
_LOCAL_CTX: SecurityCtx = {
    "tenant": DEFAULT_TENANT,
    "principal": f"local:{getpass.getuser()}",
    "claims": {},
}

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
    """Render one turn's or resume's events (`stream_turn`/`stream_resume`).

    Returns True if the run paused awaiting tool-call approval (the caller
    should prompt and call `stream_resume`), False once the turn has
    actually finished (or errored)."""
    # `aclosing`: `approval_required` returns before the stream is exhausted (it is the stream's
    # last event, but its generators are still open), and a bare `async for` would leave the
    # closing of what that return abandoned to the event loop's finalizer.
    async with aclosing(events) as stream:
        async for event in stream:
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


async def _stop_turn(thread_id: str) -> None:
    """Best effort, on the way out of an interrupted turn: the person is leaving, and a Redis
    that is also down is not worth burying the interrupt under."""
    try:
        await cancel_turn(thread_id, _LOCAL_CTX, domain=AGENT_DOMAIN)
    except Exception as exc:  # noqa: BLE001 - cleanup on the way out must not replace the interrupt
        print(f"\n{_DIM}  [could not ask the worker to stop the turn: {type(exc).__name__}]{_RESET}")


async def _async_turn(text: str, thread_id: str, hitl: bool = False) -> None:
    print("bot> ", end="", flush=True)
    try:
        needs_approval = await _render_stream(
            stream_turn(text, thread_id, _LOCAL_CTX, domain=AGENT_DOMAIN, require_approval=hitl)
        )
        while needs_approval:
            decision = input("Approve? [y/N] ").strip().lower() == "y"
            needs_approval = await _render_stream(
                stream_resume(thread_id, decision, _LOCAL_CTX, domain=AGENT_DOMAIN)
            )
    except (asyncio.CancelledError, KeyboardInterrupt, EOFError):
        await _stop_turn(thread_id)
        raise


async def async_main(hitl: bool = False) -> None:
    """Streaming CLI: token-level latency + visible tool indicators.

    `hitl=True` gates every tool call behind an approve/reject prompt (see
    graph/hitl.py's human_approval) — the streaming event protocol's own
    opt-in HITL mechanism.
    """
    # Fails loud on a typo'd AGENT_DOMAIN: a stream no pool reads would only
    # show up as a 30 second wait and an error.
    manifest, _ = resolve_domain(AGENT_DOMAIN)
    thread_id = str(uuid.uuid4())
    mode = " with HITL tool approval" if hitl else ""
    print(
        f"Local RAG agent ready{mode} ({manifest.name} domain, through the queue: "
        "needs a running `make agent-worker`). Type 'exit' to quit.\n"
    )
    while True:
        try:
            text = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if text.lower() in {"exit", "quit"}:
            break
        if not text:
            continue
        try:
            await _async_turn(text, thread_id, hitl=hitl)
        except (EOFError, KeyboardInterrupt):
            break  # input ended at an approval prompt; `_async_turn` already stopped the turn
    print("Bye!")


if __name__ == "__main__":
    asyncio.run(async_main(hitl="--hitl" in sys.argv))
