"""Telegram bot channel (pattern 42): a fourth interface beside the CLI, the HTTP API and the web UI. It
long-polls Telegram's `getUpdates` (no public webhook needed) and publishes each message as an UNATTENDED turn
onto the domain's requests stream (`app/job_queue/producer.py`, pattern 43), where an `agent_worker.py` runs
it through `runtime/stream.py::astream_events_turn_unattended()`: a new front door onto the queue every HTTP
turn already uses. Telegram has no token-streaming UX, so this process collects the events the worker
publishes back into one final reply. It never runs the graph, holds no checkpointer pool, and needs `make
up`'s Redis and a worker for its domain (`make agent-worker`, or `agent-worker-support`/`-sales`).

Requires `TELEGRAM_BOT_TOKEN` (create via @BotFather); it refuses to start when unset rather than silently
no-op, the same fail-closed discipline as SecurityCtx and moderation. It is also the ONE surface in this app
that necessarily reaches the public internet.

## A generalized gateway, not an Ecorp-only one

`AGENT_DOMAIN` (default `"ecorp"`) picks which domain's worker pool this process's messages go to:
`AGENT_DOMAIN=support python -m app.channels.telegram` runs the support copilot, `AGENT_DOMAIN=sales` the
sales concierge, unmodified. Each domain is still its own OS process with its own bot token; this is "which
domain a process serves", not the "several domains from one process" registry the Roadmap lists as unbuilt.

A WhatsApp gateway for the same domains would reuse `handle_message`/`stream_turn()` unchanged; only the
transport differs (webhook-based, not long-poll). Not built: no WhatsApp Business credentials to verify it
against.

HITL: there is no interactive approve/reject UX. The job is published with `unattended=True`, so the worker's
`astream_events_turn_unattended()` auto-declines a mandatory-capability-gate pause (repeatedly, up to
`UNATTENDED_MAX_DECLINE_ROUNDS`, then cancels the run), so a mutating request never becomes a silent write and
never leaves the conversation paused. The user always gets a visible reply: the model's answer, an explicit
"needs a person's approval" message, or `_NO_REPLY_FALLBACK` if a turn produced no text (`_send_message` sends
NOTHING for an empty string, so without that guard an empty turn read as the bot ignoring the user).

Run with: `python -m app.channels.telegram` (Makefile's `telegram`/`telegram-support`/`telegram-sales`). Its
own process, not started by `make up`/`make serve`, since it needs a real bot token.
"""
import asyncio
import logging
import signal
from contextlib import aclosing

import httpx
import redis.asyncio as redis

from app.agent.runtime.sessions import TELEGRAM_THREAD_PREFIX
from app.core import metrics
from app.core.config import AGENT_DOMAIN, DEFAULT_TENANT, TELEGRAM_BOT_TOKEN
from app.core.logging_config import configure_logging
from app.core.security import SecurityCtx
from app.core.telemetry import configure_telemetry
from app.domains.registry import resolve_domain
from app.job_queue.producer import cancel_turn, stream_turn
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
    bot restart. The prefix is shared with `app/agent/runtime/sessions.py` because HTTP callers
    may never claim an id in this namespace (see `TELEGRAM_THREAD_PREFIX`)."""
    return f"{TELEGRAM_THREAD_PREFIX}{chat_id}"


def _ctx_for_user(user_id: int) -> SecurityCtx:
    """Every Telegram user is its own principal within one shared tenant —
    same shape as chat.py's local dev ctx, keyed by Telegram's user id
    instead of the OS username, so different users never share memories."""
    return {"tenant": DEFAULT_TENANT, "principal": f"telegram:{user_id}", "claims": {}}


_NO_REPLY_FALLBACK = (
    "I wasn't able to put together a reply to that just now — could you try again?"
)


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


def _dedup_key(update_id: int) -> str:
    """What makes two deliveries of one Telegram update "the same submission".

    The domain is part of it because the thread id is not: two bots (two domains, two
    processes) talking to one person share `telegram:<chat id>`, and each bot numbers its
    updates on its own, so an update id alone could collide across them and hand one bot the
    other's reply."""
    return f"telegram:{AGENT_DOMAIN}:update:{update_id}"


async def _run_turn(
    text: str, thread_id: str, ctx: SecurityCtx, *, dedup_key: str | None = None
) -> tuple[str, list[dict]]:
    """Publish the message as an unattended turn, wait for the worker's events, and collect the
    one final reply + citations this channel sends — built locally since this channel needs
    one full reply per message, not the raw event stream.

    `dedup_key` is what keeps a redelivered update from becoming a second turn (see
    `producer.stream_turn`): the turn outlives this process now, so replaying the message after
    a crash would run it again rather than retry something that died with the channel."""
    parts: list[str] = []
    citations: list[dict] = []
    # `aclosing`: the `error` branch returns before the stream is exhausted, and a bare
    # `async for` leaves it to the event loop's finalizer to close what that return abandoned.
    async with aclosing(
        stream_turn(text, thread_id, ctx, domain=AGENT_DOMAIN, unattended=True, dedup_key=dedup_key)
    ) as events:
        async for event in events:
            kind = event["type"]
            if kind == "token":
                parts.append(event["content"])
            elif kind == "citations":
                citations = event["items"]
            elif kind == "error":
                return event["content"], []
            elif kind == "approval_required":
                return await _decline_pause(thread_id, ctx, event), []
    return "".join(parts), citations


async def _decline_pause(thread_id: str, ctx: SecurityCtx, event: dict) -> str:
    """A job published as unattended should never end at a pause: the worker declines it. One
    that does was run by a worker that predates the flag (a rolling deploy that upgraded this
    channel first), which ran it as an ordinary turn. Nothing ran — the gate held — but the
    conversation is left paused, and an unattended channel can never resolve a pause, so every
    later message would be refused (pattern 8). So the channel cancels it itself rather than
    depend on the worker's version, and says what the worker's own ceiling message says."""
    names = ", ".join(sorted({tc.get("name", "?") for tc in event.get("tool_calls", [])})) or "an action"
    metrics.agent_unattended_job_paused_total.inc()
    logger.warning("telegram_unattended_job_paused", extra={"thread_id": thread_id, "tools": names})
    try:
        await cancel_turn(thread_id, ctx, domain=AGENT_DOMAIN)
    except Exception as exc:  # noqa: BLE001 - the person still gets the reply below; a cancel that did not land leaves the pause, which the operator sees in this warning
        logger.warning("telegram_pause_cancel_failed", extra={"error_class": type(exc).__name__})
    return f"That needs a person's approval ({names}), which this channel can't provide — so it wasn't done."


async def handle_message(
    client: httpx.AsyncClient, message: dict, update_id: int | None = None
) -> None:
    """Process one Telegram `message` update end to end: resolve thread/ctx,
    run the turn, and reply.

    `update_id` (the update this message arrived in) makes the turn idempotent across a
    redelivery; without it every call is a new turn.

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

    reply_text, citations = await _run_turn(
        text,
        thread_id,
        ctx,
        dedup_key=None if update_id is None else _dedup_key(update_id),
    )
    if not reply_text.strip():
        reply_text = _NO_REPLY_FALLBACK
    await _send_message(client, chat_id, _format_reply(reply_text, citations))


async def run() -> None:
    """The long-poll loop: fetch updates since the last offset, handle each sequentially (one chat at a time),
    then advance AND PERSIST `offset` past each. A message is never replayed after a successful reply, and
    since `offset` is durable in Redis, a restart resumes from the last one actually handled instead of
    redelivering every update Telegram still remembers.

    A crash between finishing `handle_message` and the `_save_offset` below still redelivers that one message
    (a duplicate reply), the accepted tradeoff against persisting BEFORE handling, which would silently drop a
    user's message forever if the crash hit while handling it. The redelivery is not a second TURN:
    `handle_message` keys the submission by update id (`producer.stream_turn`'s `dedup_key`), so within
    `RESULTS_STREAM_TTL_SECONDS` it reads the first attempt's results instead of publishing again: possibly a
    second copy of the reply, never a second model call or a second question in the thread.

    Sequential is a deliberate demo-scope choice and a real limit now that turns run on a worker pool: the
    pool can run many chats at once, but this loop waits for one reply before starting the next, so one slow
    turn delays every other chat this process serves. Fanning out would need per-chat ordering and an offset
    that advances only past a contiguous run of finished updates; not built.
    """
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is not set — see app/channels/telegram.py's module docstring"
        )

    # Fails loud on a typo'd AGENT_DOMAIN, before a stream no worker pool reads is ever
    # published to. The graph itself is built by the worker, not here.
    manifest, _ = resolve_domain(AGENT_DOMAIN)
    logger.info("telegram_channel_domain", extra={"domain": manifest.name})
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
                    await handle_message(client, message, update_id=update["update_id"])
                offset = update["update_id"] + 1
                await _save_offset(redis_client, AGENT_DOMAIN, offset)

    # Nothing else to close: this process never touched the checkpointer or
    # core/sql_store.py's pool, since the graph runs in the worker.
    logger.info("telegram_channel_stopping")


if __name__ == "__main__":
    configure_logging()
    configure_telemetry("agent-core-telegram")
    asyncio.run(run())
