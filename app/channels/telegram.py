"""Telegram bot channel (pattern 42) — a fourth interface alongside the CLI,
the HTTP API, and the built-in web UI. Long-polls Telegram's `getUpdates`
(no public webhook needed) and drives the same
`runtime_stream.py::astream_events_turn_unattended()` every queued worker
turn runs through — just a new front door onto it, collecting streamed
events into one final reply since Telegram has no token-streaming UX.

Requires `TELEGRAM_BOT_TOKEN` (create via @BotFather) — refuses to start
rather than silently no-op when unset, same fail-closed discipline as
SecurityCtx/moderation. Also the ONE surface in this app that necessarily
reaches the public internet, unlike the rest of the fully-local stack.

## A generalized gateway, not an Ecorp-only one

`AGENT_DOMAIN` (default `"ecorp"`) picks which domain this process's shared
graph singleton boots against — `AGENT_DOMAIN=support python -m
app.channels.telegram` runs the support copilot, `AGENT_DOMAIN=sales` the
sales concierge, unmodified. Each domain is still its own OS process (own
bot token) — this is "which domain a process boots as," not the
"several domains from one process" registry the Roadmap still lists as
unbuilt (see runtime.py::init_graph_async).

A WhatsApp gateway for the same domains would reuse
`handle_message`/`astream_events_turn_unattended()` unchanged — only the
transport differs (WhatsApp is webhook-based, not long-poll). Not built
here: no WhatsApp Business credentials to verify it against.

HITL: this channel has no interactive approve/reject UX —
`astream_events_turn_unattended()` auto-declines a mandatory-capability-gate
pause for callers with no human on the other end, so a mutating request
gets a real reply explaining it wasn't approved, never a silent write.

Run with: `python -m app.channels.telegram` (Makefile's `telegram`/
`telegram-support`/`telegram-sales`). Its own process, not started by
`make up`/`make serve`, since it needs a real bot token to do anything.
"""
import asyncio
import logging
import signal

import httpx
import redis.asyncio as redis

from app.agent import sql_store
from app.agent.runtime import close_checkpointer_pool, init_graph_async
from app.agent.runtime_stream import astream_events_turn_unattended
from app.core.config import AGENT_DOMAIN, DEFAULT_TENANT, TELEGRAM_BOT_TOKEN
from app.core.logging_config import configure_logging
from app.core.security import SecurityCtx
from app.core.telemetry import configure_telemetry
from app.domains.registry import resolve_domain
from app.job_queue.queue import get_client as get_redis_client

logger = logging.getLogger(__name__)

_API_BASE = "https://api.telegram.org"
_POLL_TIMEOUT_SECONDS = 30  # Telegram long-poll window; getUpdates blocks up to this long
_MESSAGE_CHAR_LIMIT = 4000  # under Telegram's real 4096-UTF16-unit cap, with headroom


def _api_url(method: str) -> str:
    return f"{_API_BASE}/bot{TELEGRAM_BOT_TOKEN}/{method}"


def _offset_key(domain: str) -> str:
    return f"telegram:offset:{domain}"


async def _load_offset(client: redis.Redis, domain: str) -> int:
    """The durable half of the fix for a real bug: `offset` used to live
    only in a local variable, so any process restart reset it to 0 and
    Telegram would redeliver EVERY update it still remembers (its own
    retention window, not bounded by anything this app controls) — every
    already-handled message since the last restart, each producing a fresh
    duplicate turn and reply to a real user. Reading the last persisted
    value here (0 if this domain has never polled before) closes that."""
    raw = await client.get(_offset_key(domain))
    return int(raw) if raw is not None else 0


async def _save_offset(client: redis.Redis, domain: str, offset: int) -> None:
    """Called AFTER a message is handled, not before — see run()'s own
    comment on why persisting only once handling has actually succeeded is
    the safer failure mode (at-least-once/possible-duplicate-reply beats
    at-most-once/silently-dropped-message for a chat bot)."""
    await client.set(_offset_key(domain), str(offset))


def _thread_id_for_chat(chat_id: int) -> str:
    """One durable conversation thread per Telegram chat — stable across
    process restarts (the durable checkpointer), so history survives a
    bot restart."""
    return f"telegram:{chat_id}"


def _ctx_for_user(user_id: int) -> SecurityCtx:
    """Every Telegram user is its own principal within one shared tenant —
    same shape as chat.py's local dev ctx, keyed by Telegram's user id
    instead of the OS username, so different users never share memories."""
    return {"tenant": DEFAULT_TENANT, "principal": f"telegram:{user_id}", "claims": {}}


def _format_reply(text: str, citations: list[dict]) -> str:
    if not citations:
        return text
    sources = "\n".join(
        f"{c.get('marker', '')} {c.get('title') or c.get('doc_id', '?')}" for c in citations
    )
    return f"{text}\n\nSources:\n{sources}"


async def _send_message(client: httpx.AsyncClient, chat_id: int, text: str) -> None:
    """Splits rather than truncates: a long, grounded answer with citations
    is a normal shape here, not an edge case. Best-effort — a failed send
    is logged, never raised, so one bad chat_id can't kill the poll loop."""
    for i in range(0, len(text), _MESSAGE_CHAR_LIMIT):
        chunk = text[i : i + _MESSAGE_CHAR_LIMIT]
        try:
            resp = await client.post(
                _api_url("sendMessage"), json={"chat_id": chat_id, "text": chunk}
            )
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001 - one failed send must not kill the poll loop
            logger.warning(
                "telegram_send_failed",
                extra={"chat_id": chat_id, "error_class": type(exc).__name__},
            )


async def _run_turn(text: str, thread_id: str, ctx: SecurityCtx) -> tuple[str, list[dict]]:
    """Drive astream_events_turn_unattended to completion and collect the one
    final reply + citations this channel sends — built locally since this
    channel needs one full reply per message, not the raw event stream."""
    parts: list[str] = []
    citations: list[dict] = []
    async for event in astream_events_turn_unattended(text, thread_id, ctx):
        kind = event["type"]
        if kind == "token":
            parts.append(event["content"])
        elif kind == "citations":
            citations = event["items"]
        elif kind == "error":
            return event["content"], []
    return "".join(parts), citations


async def handle_message(client: httpx.AsyncClient, message: dict) -> None:
    """Process one Telegram `message` update end to end: resolve thread/ctx,
    run the turn, and reply.

    Non-text messages (photos, stickers, voice, ...) are silently skipped —
    out of scope for this channel.
    """
    chat_id = message["chat"]["id"]
    user_id = message.get("from", {}).get("id", chat_id)
    text = message.get("text")
    if not text:
        return

    thread_id = _thread_id_for_chat(chat_id)
    ctx = _ctx_for_user(user_id)

    try:
        await client.post(
            _api_url("sendChatAction"), json={"chat_id": chat_id, "action": "typing"}
        )
    except Exception:  # noqa: BLE001, S110 - a typing indicator is cosmetic, never worth failing over
        pass

    reply_text, citations = await _run_turn(text, thread_id, ctx)
    await _send_message(client, chat_id, _format_reply(reply_text, citations))


async def run() -> None:
    """The long-poll loop: fetch updates since the last offset, handle each
    sequentially (one chat at a time), then advance AND PERSIST `offset`
    past each one — a message is never replayed after a successful reply,
    and (since `offset` is now durable in Redis, not just a local variable)
    a process restart resumes from the last one actually handled instead of
    redelivering every update Telegram still remembers. A crash between
    finishing `handle_message` and the persisting `_save_offset` call below
    still means that one message gets redelivered and re-handled on
    restart (a duplicate reply) — accepted as the right tradeoff over the
    alternative (persist BEFORE handling), which would silently drop a
    user's message forever if the crash happened while handling it.
    Sequential is a deliberate demo-scope choice; a real deployment fanning
    out to many concurrent chats would want a worker pool.
    """
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is not set — see app/channels/telegram.py's module docstring"
        )

    manifest, domain = resolve_domain(AGENT_DOMAIN)
    logger.info("telegram_channel_domain", extra={"domain": manifest.name})
    # Opens the durable checkpointer on THIS loop, against whichever domain
    # AGENT_DOMAIN resolved to — it must be bound to the same loop driving
    # it (see runtime.py).
    await init_graph_async(manifest=manifest, domain=domain)
    redis_client = get_redis_client()
    offset = await _load_offset(redis_client, AGENT_DOMAIN)

    # Graceful shutdown: SIGTERM/SIGINT stops new getUpdates polls but never
    # interrupts a message already being handled (same shape as
    # agent_worker.py's `run()`). Bounded by `_POLL_TIMEOUT_SECONDS` since
    # an in-flight getUpdates call isn't cancelled mid-flight.
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop_event.set)

    async with httpx.AsyncClient(timeout=_POLL_TIMEOUT_SECONDS + 10) as client:
        logger.info("telegram_channel_started")
        while not stop_event.is_set():
            try:
                resp = await client.get(
                    _api_url("getUpdates"),
                    params={"offset": offset, "timeout": _POLL_TIMEOUT_SECONDS},
                )
                resp.raise_for_status()
                updates = resp.json().get("result", [])
            except Exception as exc:  # noqa: BLE001 - a poll failure must not kill the loop
                logger.warning("telegram_poll_failed", extra={"error_class": type(exc).__name__})
                await asyncio.sleep(5)
                continue

            for update in updates:
                message = update.get("message")
                if message:
                    await handle_message(client, message)
                offset = update["update_id"] + 1
                await _save_offset(redis_client, AGENT_DOMAIN, offset)

    logger.info("telegram_channel_stopping")
    # Same reasoning as app/api/main.py's lifespan shutdown: handle_message
    # runs the full graph, which may have opened sql_store.py's pool.
    # No-op if this process never touched it.
    await sql_store.close_pool()
    # Same reasoning for the checkpointer's pool — init_graph_async() above
    # always opens it, so this is never a no-op here.
    await close_checkpointer_pool()


if __name__ == "__main__":
    configure_logging()
    configure_telemetry("agent-core-telegram")
    asyncio.run(run())
