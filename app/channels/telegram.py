"""Telegram bot channel (GRAPH_PATTERNS.md pattern 42) — a fourth
first-party interface alongside the CLI (app/channels/chat.py), the HTTP API
(app/api/main.py), and the built-in web UI (pattern 29). Long-polls Telegram's
`getUpdates` (no public webhook URL needed — appropriate for a local/demo
deployment with no inbound port to expose) and calls the SAME
`app/agent/runtime.py::answer()` every other synchronous interface already calls —
no bespoke graph-driving logic here, just a new front door onto it.

Requires `TELEGRAM_BOT_TOKEN` (create a bot via @BotFather, then set it in
.env) — refuses to start (raises, loud) rather than silently no-op when
unset, matching this app's fail-closed-on-missing-config discipline
elsewhere (SecurityCtx, moderation). This is also the ONE surface in this
app that necessarily reaches the public internet (Telegram's own servers)
— unlike the rest of the stack, which runs fully local via docker-compose.

## A generalized gateway, not an Acme-only one

`AGENT_DOMAIN` (app/core/config.py, default `"acme"`) picks which domain
(app/domains/registry.py) this process's shared graph singleton boots
against — `AGENT_DOMAIN=support python -m app.channels.telegram` runs the
Tier-1 support copilot (app/domains/support/), `AGENT_DOMAIN=sales` the
sales concierge (app/domains/sales/) behind this exact same gateway,
unmodified. Each is still its own OS process (its own bot token, in
practice) — see app/agent/runtime.py's init_graph_sync/init_graph_async
docstrings for why this is "which one domain a process boots as," not the
"several domains from one running process" registry GRAPH_PATTERNS.md's
Roadmap still lists as unbuilt.

A WhatsApp gateway for the same domains would reuse the identical
`handle_message`/`answer()` core below unchanged — only the transport
differs: WhatsApp's Business Cloud API is push/webhook-based (Meta POSTs
to a public HTTPS endpoint you expose and verify), not long-poll-based
like Telegram, so it would be a small FastAPI route (app/api/main.py) with
signature verification and a Graph API send call, not a poller. Not built
here: this repo doesn't ship integration code it can't verify against a
live service (the same reasoning "Extending Further" already gives for
not building a real webhook-based Telegram deployment), and there's no
WhatsApp Business account/credentials to verify one against.

HITL tool-call approval: this channel has no interactive approve/reject UX
(no inline keyboard handling) — `answer()` already auto-declines a
mandatory-capability-gate pause for exactly this kind of single-shot,
one-way caller (see its docstring and GRAPH_PATTERNS.md pattern 8's note
on `answer`/`stream_turn`), so a Telegram user asking for a mutating action
gets a real reply explaining it wasn't approved, never a silently-run
write or a message that never arrives.

Run with: `python -m app.channels.telegram` (see Makefile's `telegram`/
`telegram-support`/`telegram-sales` targets). Runs as its own process —
not started by `make up`/`make serve` — since it's opt-in and needs a real
bot token to do anything at all.
"""
import asyncio
import logging
import signal

import httpx

from app.agent import sql_store
from app.agent.runtime import answer, init_graph_sync
from app.core.config import AGENT_DOMAIN, DEFAULT_TENANT, TELEGRAM_BOT_TOKEN
from app.core.logging_config import configure_logging
from app.core.security import SecurityCtx
from app.core.telemetry import configure_telemetry
from app.domains.registry import resolve_domain

logger = logging.getLogger(__name__)

_API_BASE = "https://api.telegram.org"
_POLL_TIMEOUT_SECONDS = 30  # Telegram long-poll window; getUpdates blocks up to this long
_MESSAGE_CHAR_LIMIT = 4000  # under Telegram's real 4096-UTF16-unit cap, with headroom


def _api_url(method: str) -> str:
    return f"{_API_BASE}/bot{TELEGRAM_BOT_TOKEN}/{method}"


def _thread_id_for_chat(chat_id: int) -> str:
    """One durable conversation thread per Telegram chat — stable across
    process restarts (the durable AsyncPostgresSaver checkpointer, same as
    every other interface), so a user's history survives a bot restart."""
    return f"telegram:{chat_id}"


def _ctx_for_user(user_id: int) -> SecurityCtx:
    """Every Telegram user is its own principal within one shared tenant —
    the same shape as app/channels/chat.py's local dev ctx, just keyed by Telegram's
    own user id instead of the OS username, so different Telegram users
    never share memories (app/agent/tools.py's remember/recall_memories)."""
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
    is a normal, expected shape here, not an edge case to just cut off.
    Best-effort — a failed send is logged, never raised, so one bad chat_id
    can't take down the poll loop for every other chat."""
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


async def handle_message(client: httpx.AsyncClient, message: dict) -> None:
    """Process one Telegram `message` update end to end: resolve thread/ctx,
    run the turn (via answer() in a worker thread so the poll loop's own
    event loop is never blocked by a slow local model), and reply.

    Non-text messages (photos, stickers, voice, ...) are silently skipped —
    out of scope for this channel; see GRAPH_PATTERNS.md's "Extending
    Further" note on multimodal support for what would need to change to
    handle an image here instead of just in a browser/API caller.
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

    reply_text, citations, _error, _ungrounded = await asyncio.to_thread(
        answer, text, thread_id, ctx
    )
    await _send_message(client, chat_id, _format_reply(reply_text, citations))


async def run() -> None:
    """The long-poll loop: fetch updates since the last processed offset,
    handle each sequentially — one bot process, one chat handled at a time
    — then advance `offset` past every update in the batch so a message is
    never replayed after a successful reply, but IS retried (at-least-once,
    not exactly-once) if the process dies mid-batch. A single, sequential
    loop is a deliberate demo-scope choice: a real deployment fanning out
    to many concurrent chats would want a worker pool, which is exactly the
    shape GRAPH_PATTERNS.md's "Extending Further" Redis Streams note
    describes generalizing this whole app towards.
    """
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is not set — see app/channels/telegram.py's module docstring"
        )

    manifest, domain = resolve_domain(AGENT_DOMAIN)
    logger.info("telegram_channel_domain", extra={"domain": manifest.name})
    # Prime the durable checkpointer on its own background thread up front,
    # against WHICHEVER domain AGENT_DOMAIN resolved to — see this module's
    # docstring and app/agent/runtime.py::init_graph_sync's own docstring.
    init_graph_sync(manifest=manifest, domain=domain)
    offset = 0

    # Graceful shutdown: a SIGTERM/SIGINT stops this loop from starting a
    # NEW getUpdates poll, but never interrupts a message already being
    # handled — same "finish what's claimed, never abandon it mid-turn"
    # shape as app/turns/agent_worker.py's `run()`. Bounded by how long a single
    # getUpdates call can block (`_POLL_TIMEOUT_SECONDS`, Telegram's own
    # long-poll window) rather than instant, since that call itself isn't
    # cancelled mid-flight — the tradeoff a long-poll design accepts, per
    # this function's own docstring on why a real webhook deployment would
    # differ.
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
                offset = update["update_id"] + 1
                message = update.get("message")
                if message:
                    await handle_message(client, message)

    logger.info("telegram_channel_stopping")
    # Same reasoning as app/api/main.py's lifespan shutdown: handle_message ->
    # answer() runs the full graph, which may have opened
    # app/agent/sql_store.py's connection pool (query_employees,
    # app/agent/meter.py::record_usage). A no-op if this process never touched it.
    sql_store.close_pool()


if __name__ == "__main__":
    configure_logging()
    configure_telemetry("agent-core-telegram")
    asyncio.run(run())
