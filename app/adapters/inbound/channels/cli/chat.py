"""Interactive CLI for the LangGraph agent.

A client of the chat API through the front door, like the web page (specs/020): the person signs in once (a browser opens the authorization
server's page; `app/adapters/inbound/channels/cli/auth.py`), and each turn is a `POST /chat/stream/queued` carrying their token. The door proves who they are,
meters them and hands the API a signed identity; the API publishes the turn and relays the events a worker publishes back
(`app/adapters/inbound/channels/door.py`). This process never touches the queue or the graph, so `make chat` needs the front door (or, for development, the API
alone with `AUTH_MODE=headers`) and a worker for the domain (`make agent-worker`); without one the turn ends, after
`CHAT_FIRST_RESPONSE_DEADLINE_SECONDS`, with an error that says so. What the worker does for every turn (the per-thread lock, crash reclaim,
tracing, the spend meter) it does for this one.

`AGENT_DOMAIN` (default `ecorp`) picks which domain's worker pool gets the turns: `AGENT_DOMAIN=support make chat` talks to the support copilot.
`CHANNEL_API_URL` is where the front door is; `CHANNEL_CLI_TENANT` is the tenant whose client the person signs in at (default `DEFAULT_TENANT`).

Raw tokens print as they arrive, with a visible indicator whenever a tool is called, so you can watch the agent work.

`--hitl` asks for an approval before EVERY tool call (`require_approval`): a runnable example of the pause/resume cycle, where the turn ends at
`approval_required` and the answer is a `POST /chat/resume`. A write is gated either way (constitution Principle II); this adds the reads.

Ctrl-C or end-of-input mid-turn asks the worker to stop it (`POST /chat/cancel`). The turn is not this process's to kill, so without that a
worker would run it to the end, including a tool call the person was about to be asked to approve.

`--sign-out` forgets the saved sign-in. With `AUTH_MODE=headers` (development, announced loudly by the API) nothing is signed in: the tenant and
the OS user are sent as headers, which is only safe where nothing a stranger controls can reach the API.

Run with: `make chat`, `make chat-hitl`, or `python -m app.adapters.inbound.channels.cli.chat --hitl`
"""
import asyncio
import getpass
import sys
import uuid
from collections.abc import AsyncGenerator
from contextlib import aclosing

from app.adapters.inbound.channels.cli.auth import (
    CliAuth,
    SessionStore,
    default_session_path,
)
from app.adapters.inbound.channels.door import Auth, Door, HeaderAuth, make_http_client
from app.core.config import AGENT_DOMAIN, DEFAULT_TENANT, settings
from app.domains.registry import DOMAINS

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


async def _render_stream(events: AsyncGenerator[dict, None]) -> bool:
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


async def _stop_turn(door: Door, thread_id: str) -> None:
    """Best effort, on the way out of an interrupted turn: the person is leaving, and a front door that is also down is not worth burying the
    interrupt under."""
    try:
        await door.cancel_turn(thread_id)
    except Exception as exc:  # noqa: BLE001 - cleanup on the way out must not replace the interrupt
        print(f"\n{_DIM}  [could not ask the worker to stop the turn: {type(exc).__name__}]{_RESET}")


async def _async_turn(door: Door, text: str, thread_id: str, hitl: bool = False) -> None:
    print("bot> ", end="", flush=True)
    try:
        needs_approval = await _render_stream(door.stream_turn(text, thread_id, require_approval=hitl))
        while needs_approval:
            decision = input("Approve? [y/N] ").strip().lower() == "y"  # noqa: ASYNC250 - nothing else runs while the CLI waits for the person
            needs_approval = await _render_stream(door.stream_resume(thread_id, decision))
    except (asyncio.CancelledError, KeyboardInterrupt, EOFError):
        await _stop_turn(door, thread_id)
        raise


def _tenant() -> str:
    return settings.channel_cli_tenant.strip() or DEFAULT_TENANT


async def async_main(hitl: bool = False) -> None:
    """Streaming CLI: token-level latency + visible tool indicators.

    `hitl=True` asks for an approval before every tool call (`require_approval`).
    """
    # Fails loud on a typo'd AGENT_DOMAIN: a stream no pool reads would only show up as a 30 second wait and an error.
    if AGENT_DOMAIN not in DOMAINS:
        sys.exit(f"AGENT_DOMAIN={AGENT_DOMAIN!r} is not a registered domain ({', '.join(sorted(DOMAINS))})")
    thread_id = str(uuid.uuid4())
    mode = " with HITL tool approval" if hitl else ""
    async with make_http_client() as http:
        auth: Auth
        if settings.auth_mode == "headers":
            auth = HeaderAuth(_tenant(), f"local:{getpass.getuser()}")
            who = "AUTH_MODE=headers: nothing is signed in"
        else:
            auth = CliAuth(http, tenant=_tenant())
            who = "you will be asked to sign in"
        door = Door(auth, channel="cli", domain=AGENT_DOMAIN, http=http)
        print(
            f"Local RAG agent ready{mode} ({AGENT_DOMAIN} domain, through {settings.channel_api_url}: {who}; "
            "needs a running `make agent-worker`). Type 'exit' to quit.\n"
        )
        while True:
            try:
                text = input("you> ").strip()  # noqa: ASYNC250 - as above: the turn is over, nothing else is running
            except (EOFError, KeyboardInterrupt):
                break
            if text.lower() in {"exit", "quit"}:
                break
            if not text:
                continue
            try:
                await _async_turn(door, text, thread_id, hitl=hitl)
            except (EOFError, KeyboardInterrupt):
                break  # input ended at an approval prompt; `_async_turn` already stopped the turn
    print("Bye!")


def sign_out() -> None:
    forgotten = SessionStore(default_session_path()).clear()
    print("Signed out." if forgotten else "There was no saved sign-in.")


if __name__ == "__main__":
    if "--sign-out" in sys.argv:
        sign_out()
    else:
        asyncio.run(async_main(hitl="--hitl" in sys.argv))
