"""Telegram bot channel (pattern 42): a fourth interface beside the CLI, the HTTP API and the web UI. It sends each message to the chat API through
the front door (specs/020; `app/adapters/inbound/channels/door.py`) as the Telegram user's own person at the authorization server, and a worker runs it. It never
touches the queue or the graph; it needs Redis for its own small state (below) and a worker for its domain.

Requires `TELEGRAM_BOT_TOKEN` (from @BotFather) and refuses to start without it, the same fail-closed discipline as SecurityCtx and moderation. It is
the ONE surface that necessarily reaches the public internet.

**Transport and dispatch** (specs/021). `TELEGRAM_MODE=poll` (the default) long-polls `getUpdates`; `webhook` is served by `server.py`.
Either one only pushes the update onto a Redis list; `inbox.py` runs them, up to `TELEGRAM_MAX_CONCURRENT_CHATS` chats at a time and each
chat in order, so a slow turn delays only its own chat and `/cancel` can reach it.

**Human in the loop** (specs/021 FR-001). A turn that needs approval ends at `approval_required`; the bot shows what it wants to do with Approve /
Reject buttons, and only the owner's tap resumes it (`approvals.py` for why that is safe). With `TELEGRAM_APPROVALS=false` the turn is sent
`unattended`: the worker declines every pause, so a mutating request never becomes a silent write and never leaves the conversation paused.

`AGENT_DOMAIN` (default `"ecorp"`) picks the domain whose worker pool receives messages: `AGENT_DOMAIN=support python -m app.adapters.inbound.channels.telegram.bot`
runs the support copilot, unmodified. Each domain is its own process with its own bot token. Run with `python -m app.adapters.inbound.channels.telegram.bot` (Makefile
`telegram`, `telegram-support`, `telegram-sales`); not started by `make up`/`make serve`.
"""
import asyncio
import logging
import signal
from collections.abc import AsyncGenerator
from contextlib import aclosing
from dataclasses import dataclass, field
from typing import Any

import httpx
import redis.asyncio as redis

from app.adapters.inbound.channels.door import (
    SIGNIN_MESSAGES,
    Auth,
    Door,
    DoorRefusedError,
    HeaderAuth,
    make_http_client,
)
from app.adapters.inbound.channels.oauth import SignInRefusedError
from app.adapters.inbound.channels.telegram import link, people, server
from app.adapters.inbound.channels.telegram.api import NO_KEYBOARD, BotApi
from app.adapters.inbound.channels.telegram.approvals import (
    ApprovalStore,
    Pending,
    keyboard,
    parse_callback,
    render,
)
from app.adapters.inbound.channels.telegram.inbox import Dispatcher, Inbox
from app.adapters.inbound.channels.telegram.link import LinkedAuth, LinkFlow, LinkStore
from app.adapters.inbound.channels.telegram.people import TelegramPeople
from app.adapters.inbound.channels.telegram.reply import ReplySink
from app.agent.runtime.sessions import TELEGRAM_THREAD_PREFIX
from app.core import metrics
from app.core.config import AGENT_DOMAIN, DEFAULT_TENANT, TELEGRAM_BOT_TOKEN, settings
from app.domains.registry import DOMAINS
from app.platform.logging_config import configure_logging

# Redis for the channel's OWN small state (offset, inbox, approvals, link records), through the one client factory and nothing else from that module.
from app.platform.redis_client import get_client as get_redis_client
from app.platform.telemetry import configure_telemetry

logger = logging.getLogger(__name__)

_POLL_TIMEOUT_SECONDS = 30  # Telegram long-poll window; getUpdates blocks up to this long
_MEDIA = ("photo", "voice", "audio", "video", "video_note", "document", "sticker", "animation", "location", "contact", "poll", "venue")

HELP = (
    "Send me a question in plain text and I will answer it.\n\n"
    "/cancel stops what I am doing, or drops an action waiting for your approval.\n"
    "/help shows this message."
)
LINK_HELP = "\n/login connects your account.\n/unlink disconnects it."
NON_TEXT_REPLY = "I can only read text messages for now. Please type your question."
_NO_REPLY_FALLBACK = "I wasn't able to put together a reply to that just now — could you try again?"
_ASK_FIRST = "You have an approval waiting above. Tap Approve or Reject on it, or send /cancel to drop it."
_COMMANDS = {"/start", "/help", "/cancel", "/login", "/unlink"}


def _offset_key(domain: str) -> str:
    return f"telegram:offset:{domain}"


async def _load_offset(client: redis.Redis, domain: str) -> int:
    """The durable half of the fix for a real bug: `offset` used to live only in a local variable, so any process restart reset it to 0 and Telegram
    would redeliver EVERY update it still remembers, each producing a fresh duplicate turn and reply to a real user."""
    raw = await client.get(_offset_key(domain))
    return int(raw) if raw is not None else 0


async def _save_offset(client: redis.Redis, domain: str, offset: int) -> None:
    """Called once the batch is in the inbox (not once it is answered: the inbox is what makes the update durable, specs/021 FR-006)."""
    await client.set(_offset_key(domain), str(offset))


def _thread_id(principal: str, chat_id: int) -> str:
    """One durable conversation per person per Telegram chat, stable across restarts (the durable checkpointer). It NAMES its owner
    (`telegram:<person>:<chat>`) because the API lets a caller claim an id in this namespace only if it names that caller
    (app/adapters/inbound/api/routers/chat.py), so nobody can squat a chat's conversation before its person writes. In a group chat each person has their own."""
    return f"{TELEGRAM_THREAD_PREFIX}{principal}:{chat_id}"


def _replay_key(update_id: int) -> str:
    """What makes two deliveries of one Telegram update "the same submission". The domain is part of it because the thread id is not: two bots (two
    domains, two processes) talking to one person share `telegram:<person>:<chat id>`, and each bot numbers its updates on its own."""
    return f"telegram:{AGENT_DOMAIN}:update:{update_id}"


def _format_reply(text: str, citations: list[dict[str, Any]]) -> str:
    if not citations:
        return text
    sources = "\n".join(f"{c.get('marker', '')} {c.get('title') or c.get('doc_id', '?')}" for c in citations)
    return f"{text}\n\nSources:\n{sources}"


def _refusal_reply(reason: str, user_id: int) -> str:
    text = SIGNIN_MESSAGES.get(reason, SIGNIN_MESSAGES["credentials"])
    return f"{text} (Your Telegram id is {user_id}.)" if reason == "unknown_person" else text


def _command(text: str) -> tuple[str, str] | None:
    """(`/name`, the rest) when the text starts with a command this bot knows; `/name@botname` counts."""
    if not text.startswith("/"):
        return None
    head, _, rest = text.partition(" ")
    name = head.split("@", 1)[0].lower()
    return (name, rest.strip()) if name in _COMMANDS else None


class Channel:
    """How the bot reaches the front door as each Telegram user. In `identity` mode every user is a person at the authorization server and the
    bot presents that person's token; in `headers` mode (development) it names the user in headers, as the web page's pickers do. With account
    linking (specs/021 FR-008) the person is the account the user linked with `/login`, not a `telegram-<id>` person the bot signs in as."""

    def __init__(self, http: httpx.AsyncClient, *, links: LinkStore | None = None) -> None:
        self.http = http
        self.links = links if settings.auth_mode != "headers" else None
        self.people = None if settings.auth_mode == "headers" or self.links else TelegramPeople(http)
        self._linked: dict[int, LinkedAuth] = {}

    async def door_for(self, user_id: int) -> tuple[Door, str]:
        """(a door that acts as this Telegram user, that person's id). Raises `SignInRefusedError` when they cannot be signed in."""
        auth: Auth
        if self.links is not None:
            linked = self._linked.setdefault(user_id, LinkedAuth(self.http, self.links, user_id))
            principal = await linked.sign_in()
            auth = linked
        elif self.people is None:
            principal = f"telegram:{user_id}"
            auth = HeaderAuth(settings.telegram_tenant.strip() or DEFAULT_TENANT, principal)
        else:
            person = self.people.person(user_id)
            principal = await person.sign_in()
            auth = person
        if not principal:
            raise SignInRefusedError("credentials")  # a token with no subject names nobody
        return Door(auth, channel="telegram", domain=AGENT_DOMAIN, http=self.http), principal


@dataclass
class Outcome:
    """What one stream of the worker's events came to."""

    text: str = ""
    citations: list[dict[str, Any]] = field(default_factory=list)
    paused: list[dict[str, Any]] | None = None  # tool calls awaiting a decision
    error: bool = False
    code: str | None = None  # the API's error code; None for a refusal the door itself made (no sign-in, rate limit, unreachable)
    events: int = 0


class Bot:
    def __init__(
        self,
        api: BotApi,
        channel: Channel,
        *,
        approvals: ApprovalStore | None = None,
        linker: LinkFlow | None = None,
    ) -> None:
        self.api = api
        self.channel = channel
        self.approvals = approvals
        self.linker = linker

    # --- updates ---

    async def handle_update(self, update: dict[str, Any]) -> None:
        if isinstance(update.get("callback_query"), dict):
            await self._on_button(update["callback_query"])
        elif isinstance(update.get("message"), dict):
            await self._on_message(update["message"], update.get("update_id"))

    async def _on_message(self, message: dict[str, Any], update_id: int | None) -> None:
        chat_id = message["chat"]["id"]
        user_id = message.get("from", {}).get("id", chat_id)
        text = message.get("text")
        if not text:
            if any(key in message for key in _MEDIA):  # a join or pin notice is not addressed to the bot; media is, and silence read as being ignored
                await self.api.send_message(chat_id, NON_TEXT_REPLY)
            return
        command = _command(text)
        if command is None:
            await self._converse(chat_id, user_id, text, update_id)
        elif command[0] in ("/start", "/help"):
            await self.api.send_message(chat_id, HELP + (LINK_HELP if self.linker else ""))
        elif command[0] == "/cancel":
            await self._cancel(chat_id, user_id)
        else:
            await self._link_command(command[0], message, user_id)

    async def _sign_in(self, chat_id: int, user_id: int) -> tuple[Door, str] | None:
        try:
            return await self.channel.door_for(user_id)
        except SignInRefusedError as exc:
            await self.api.send_message(chat_id, _refusal_reply(exc.reason, user_id))
            return None

    # --- a turn ---

    @property
    def _attended(self) -> bool:
        return self.approvals is not None and settings.telegram_approvals

    def _sink(self, chat_id: int) -> ReplySink:
        return ReplySink(
            self.api, chat_id, interval=settings.telegram_stream_edit_interval_seconds, enabled=settings.telegram_stream_replies
        )

    async def _converse(self, chat_id: int, user_id: int, text: str, update_id: int | None) -> None:
        signed_in = await self._sign_in(chat_id, user_id)
        if signed_in is None:
            return
        door, principal = signed_in
        thread_id = _thread_id(principal, chat_id)
        if self._attended and self.approvals is not None and await self.approvals.for_thread(thread_id):
            await self.api.send_message(chat_id, _ASK_FIRST)
            return
        replay_key = None if update_id is None else _replay_key(update_id)
        await self.api.typing(chat_id)
        sink = self._sink(chat_id)
        outcome = await self._collect(door.stream_turn(text, thread_id, unattended=not self._attended, replay_key=replay_key), sink, door, thread_id)
        if outcome.code == "pending_approval" and self._attended:
            # A pause nobody answered (its record expired). The person wrote again, so they no longer want it: clear it, wait for that to land, and
            # send the message again under a new replay key (the first key now holds the refusal).
            outcome = await self._after_stale_pause(door, thread_id, text, replay_key, sink, outcome)
        await self._deliver(outcome, sink, door, chat_id, user_id, thread_id)

    async def _after_stale_pause(self, door: Door, thread_id: str, text: str, replay_key: str | None, sink: ReplySink, refused: Outcome) -> Outcome:
        try:
            await door.cancel_turn(thread_id, wait=True)
        except DoorRefusedError:
            logger.warning("telegram_stale_pause_not_cleared", extra={"thread_id": thread_id})
            return refused
        metrics.channel_telegram_approval_total.labels(outcome="expired").inc()
        return await self._collect(
            door.stream_turn(text, thread_id, unattended=False, replay_key=None if replay_key is None else f"{replay_key}:cleared"), sink, door, thread_id
        )

    async def _collect(self, events: AsyncGenerator[dict[str, Any], None], sink: ReplySink, door: Door, thread_id: str) -> Outcome:
        out = Outcome()
        # `aclosing`: an early return leaves the stream open for the event loop's finalizer to close what it abandoned.
        async with aclosing(events) as stream:
            async for event in stream:
                out.events += 1
                kind = event["type"]
                if kind == "token":
                    out.text += event["content"]
                    await sink.push(out.text)
                elif kind == "citations":
                    out.citations = event["items"]
                elif kind == "error":
                    return Outcome(text=str(event["content"]), error=True, code=event.get("code"), events=out.events)
                elif kind == "approval_required":
                    if self._attended:
                        out.paused = event.get("tool_calls") or []
                    else:
                        out.text = await self._decline_pause(door, thread_id, event)
                        out.citations = []
                    return out
        return out

    async def _deliver(self, outcome: Outcome, sink: ReplySink, door: Door, chat_id: int, user_id: int, thread_id: str) -> None:
        if outcome.paused is not None:
            if outcome.text.strip():
                await sink.finish(_format_reply(outcome.text, outcome.citations))
            await self._ask(door, chat_id, user_id, thread_id, outcome.paused)
            return
        text = outcome.text if outcome.error else _format_reply(outcome.text, outcome.citations)
        await sink.finish(text if text.strip() else _NO_REPLY_FALLBACK)

    async def _decline_pause(self, door: Door, thread_id: str, event: dict[str, Any]) -> str:
        """An unattended turn should never end at a pause: the worker declines it. One that does was run by a worker that predates the flag (a
        rolling deploy that upgraded this channel first), which ran it as an ordinary turn. Nothing ran, but the conversation is left paused and an
        unattended channel can never resolve a pause (pattern 8), so the channel cancels it itself rather than depend on the worker's version."""
        names = ", ".join(sorted({tc.get("name", "?") for tc in event.get("tool_calls", [])})) or "an action"
        metrics.agent_unattended_job_paused_total.inc()
        logger.warning("telegram_unattended_job_paused", extra={"thread_id": thread_id, "tools": names})
        try:
            await door.cancel_turn(thread_id)
        except Exception as exc:  # noqa: BLE001 - the person still gets the reply below; a cancel that did not land leaves the pause, which the operator sees in this warning
            logger.warning("telegram_pause_cancel_failed", extra={"error_class": type(exc).__name__})
        return f"That needs a person's approval ({names}), which this channel can't provide — so it wasn't done."

    # --- approvals ---

    async def _ask(self, door: Door, chat_id: int, user_id: int, thread_id: str, tool_calls: list[dict[str, Any]]) -> None:
        assert self.approvals is not None  # `_attended` is the only way here
        if await self.approvals.for_thread(thread_id):
            return  # a redelivered update replayed a pause that already has its buttons
        pending = await self.approvals.create(user_id=user_id, chat_id=chat_id, thread_id=thread_id, text=render(tool_calls))
        message_id = await self.api.send_message(chat_id, pending.text, reply_markup=keyboard(pending.token))
        if message_id is None:
            # Nobody can be asked, so nobody can answer: leave no paused conversation behind that every later message would be refused by.
            await self.approvals.finish(pending)
            await self._cancel_quietly(door, thread_id)
            await self.api.send_message(chat_id, "I need your approval for an action but could not show the buttons, so I did not do it.")
            return
        await self.approvals.attach_message(pending, message_id)
        metrics.channel_telegram_approval_total.labels(outcome="shown").inc()

    async def _cancel_quietly(self, door: Door, thread_id: str) -> None:
        try:
            await door.cancel_turn(thread_id)
        except Exception as exc:  # noqa: BLE001 - reported to the person as what was done (nothing); the operator sees the class here
            logger.warning("telegram_cancel_failed", extra={"error_class": type(exc).__name__})

    async def _on_button(self, query: dict[str, Any]) -> None:
        callback_id = str(query.get("id", ""))
        user_id = (query.get("from") or {}).get("id")
        message = query.get("message") or {}
        parsed = parse_callback(query.get("data"))
        if parsed is None or self.approvals is None or not isinstance(user_id, int):
            await self.api.answer_callback(callback_id, "This button is not active any more.")
            return
        token, approve = parsed
        pending = await self.approvals.get(token)
        if pending is None:
            metrics.channel_telegram_approval_total.labels(outcome="expired").inc()
            await self.api.answer_callback(callback_id, "This approval expired or was already handled.")
            await self._close_message(message, "⌛ No longer pending.")
            return
        if pending.user_id != user_id:
            metrics.channel_telegram_approval_total.labels(outcome="not_owner").inc()
            await self.api.answer_callback(callback_id, "This approval is not yours to give.", alert=True)
            return
        if not await self.approvals.claim(token):
            metrics.channel_telegram_approval_total.labels(outcome="duplicate").inc()
            await self.api.answer_callback(callback_id, "Already being handled.")
            return
        await self.api.answer_callback(callback_id, "Approved — running it…" if approve else "Rejected.")
        await self._decide(pending, approve)

    async def _close_message(self, message: dict[str, Any], note: str) -> None:
        chat_id, message_id = (message.get("chat") or {}).get("id"), message.get("message_id")
        if isinstance(chat_id, int) and isinstance(message_id, int):
            await self.api.edit_message(chat_id, message_id, f"{message.get('text') or ''}\n\n{note}".strip())

    async def _decide(self, pending: Pending, approve: bool) -> None:
        assert self.approvals is not None
        mark = "✅ Approved" if approve else "❌ Rejected"
        if pending.message_id is not None:
            await self.api.edit_message(pending.chat_id, pending.message_id, f"{pending.text}\n\n{mark}")
        signed_in = await self._sign_in(pending.chat_id, pending.user_id)  # a refusal is already explained to the person
        if signed_in is None:
            await self._undo(pending, None)
            return
        door, principal = signed_in
        if _thread_id(principal, pending.chat_id) != pending.thread_id:
            await self._undo(pending, "This approval was asked of an earlier sign-in of yours, so nothing was decided. Ask again to get a new one.")
            return
        sink = self._sink(pending.chat_id)
        outcome = await self._collect(door.stream_resume(pending.thread_id, approve), sink, door, pending.thread_id)
        if outcome.error and outcome.code is None and outcome.events == 1:
            # The door refused the decision itself (rate limit, unreachable, sign-in): it never reached the agent, so it can be made again.
            metrics.channel_telegram_approval_total.labels(outcome="resume_failed").inc()
            await self._undo(pending, f"{outcome.text} Your decision was not applied; tap again to retry.")
            return
        await self.approvals.finish(pending)
        metrics.channel_telegram_approval_total.labels(outcome="approved" if approve else "rejected").inc()
        await self._deliver(outcome, sink, door, pending.chat_id, pending.user_id, pending.thread_id)

    async def _undo(self, pending: Pending, note: str | None) -> None:
        """Put the buttons back and release the claim, so a decision that did not land can be made again."""
        assert self.approvals is not None
        await self.approvals.release(pending.token)
        if pending.message_id is not None:
            await self.api.edit_message(pending.chat_id, pending.message_id, pending.text, reply_markup=keyboard(pending.token))
        if note:
            await self.api.send_message(pending.chat_id, note)

    # --- /cancel and linking ---

    async def _cancel(self, chat_id: int, user_id: int) -> None:
        signed_in = await self._sign_in(chat_id, user_id)
        if signed_in is None:
            return
        door, principal = signed_in
        thread_id = _thread_id(principal, chat_id)
        pending = await self.approvals.for_thread(thread_id) if self.approvals is not None else None
        try:
            await door.cancel_turn(thread_id)
        except DoorRefusedError:
            await self.api.send_message(chat_id, "I could not reach the agent to cancel that. Try again shortly.")
            return
        if pending is not None and self.approvals is not None:
            await self.approvals.finish(pending)
            metrics.channel_telegram_approval_total.labels(outcome="cancelled").inc()
            if pending.message_id is not None:
                await self.api.edit_message(chat_id, pending.message_id, f"{pending.text}\n\n🚫 Cancelled", reply_markup=NO_KEYBOARD)
        await self.api.send_message(chat_id, "Cancelled." if pending is not None else "Asked the agent to stop what it was doing.")

    async def _link_command(self, name: str, message: dict[str, Any], user_id: int) -> None:
        chat = message["chat"]
        if self.linker is None:
            await self.api.send_message(chat["id"], "Connecting an account is not set up for this bot.")
        elif chat.get("type") != "private":
            await self.api.send_message(chat["id"], "Send me that in a private chat: the link it gives is yours alone.")
        elif name == "/login":
            await self.linker.login(chat["id"], user_id, message.get("from") or {})
        else:
            await self.linker.unlink(chat["id"], user_id)


# --- transports ---


async def _poll(api: BotApi, client: redis.Redis, inbox: Inbox, stop: asyncio.Event) -> None:
    """Long-poll Telegram and put every update on the inbox. The offset moves once the batch is there: a crash in between redelivers a few
    updates, and a redelivered update is one turn (the API's `replay_key`) or one button claim, never a second."""
    offset = await _load_offset(client, AGENT_DOMAIN)
    while not stop.is_set():
        try:
            updates = await api.get_updates(offset, _POLL_TIMEOUT_SECONDS)
            for update in updates:
                await inbox.push(update)
            if updates:
                offset = updates[-1]["update_id"] + 1
                await _save_offset(client, AGENT_DOMAIN, offset)
        except Exception as exc:  # noqa: BLE001 - a poll failure must not kill the loop
            logger.warning("telegram_poll_failed", extra={"error_class": type(exc).__name__})
            await asyncio.sleep(5)


def check_settings() -> None:
    """Refuse to start a bot that is set up half-way, before it takes a single message."""
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set — see app/adapters/inbound/channels/telegram/bot.py's module docstring")
    if AGENT_DOMAIN not in DOMAINS:
        raise RuntimeError(f"AGENT_DOMAIN={AGENT_DOMAIN!r} is not a registered domain ({', '.join(sorted(DOMAINS))})")
    if settings.auth_mode == "headers":
        return
    if settings.telegram_linking:
        link.check_settings()
    else:
        people.check_settings()


async def run() -> None:
    check_settings()
    if settings.telegram_mode == "webhook":
        server.check_webhook_settings()
    redis_client = get_redis_client()
    inbox = Inbox(redis_client, AGENT_DOMAIN)

    # Graceful shutdown: SIGTERM/SIGINT stops reading new updates and lets what is being answered finish.
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    links = LinkStore(redis_client, settings.telegram_link_key) if settings.telegram_linking and settings.auth_mode != "headers" else None
    linker: LinkFlow | None = None

    async with httpx.AsyncClient(timeout=_POLL_TIMEOUT_SECONDS + 10) as http, make_http_client() as door_http:
        api = BotApi(http, TELEGRAM_BOT_TOKEN)
        if links is not None:
            linker = LinkFlow(links, door_http, api)
        approvals = ApprovalStore(redis_client, AGENT_DOMAIN, ttl_seconds=settings.telegram_approval_ttl_seconds) if settings.telegram_approvals else None
        bot = Bot(api, Channel(door_http, links=links), approvals=approvals, linker=linker)
        dispatcher = Dispatcher(inbox, bot.handle_update, max_concurrent=settings.telegram_max_concurrent_chats)

        async with asyncio.TaskGroup() as group:
            group.create_task(dispatcher.run(stop))
            if settings.telegram_mode == "webhook":
                await api.set_webhook(settings.telegram_webhook_url, settings.telegram_webhook_secret)
            else:
                await api.delete_webhook()
                group.create_task(_poll(api, redis_client, inbox, stop))
            if settings.telegram_mode == "webhook" or linker is not None:
                group.create_task(server.serve(server.build_app(inbox, linker, webhook=settings.telegram_mode == "webhook"), stop))
            logger.info("telegram_channel_started", extra={"domain": AGENT_DOMAIN, "mode": settings.telegram_mode, "linking": linker is not None})
    logger.info("telegram_channel_stopping")


if __name__ == "__main__":
    configure_logging()
    configure_telemetry("agent-core-telegram")
    asyncio.run(run())
