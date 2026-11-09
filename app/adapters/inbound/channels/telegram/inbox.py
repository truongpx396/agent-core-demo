"""Where Telegram updates wait for the bot (specs/021 FR-006): both transports (long-polling and the webhook) do ONE thing, push the update onto a
Redis list, and a dispatcher takes them off it. That separates "Telegram has been told we have it" from "we have answered it":

- **Acknowledge after the push, not after the answer.** The poll offset advances and the webhook answers 200 once the update is in Redis, so a slow
  turn can no longer hold the offset back (one slow chat used to delay every chat) and Telegram does not redeliver an update that is merely slow.
- **Crash-safe.** An update moves to a `processing` list when taken and leaves it only when handled. After a crash `requeue_unfinished` puts what was
  in flight back at the FRONT, and counts the attempt; one that has crashed the process `MAX_ATTEMPTS` times is dropped and counted, so a poison
  update cannot loop forever. Handling is at-least-once, which the handlers already tolerate (a turn is keyed by its update id; a button is claimed).
- **Order per chat, parallel across chats.** Updates of one chat run one at a time in arrival order; up to `max_concurrent` chats run at once.
  `/cancel` is the exception: it must reach a turn that is running, so it never waits behind that turn.

One bot process per domain (one `getUpdates` consumer, one webhook): the processing list belongs to it.
"""
import asyncio
import json
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any

import redis.asyncio as redis

from app.core import metrics

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
MAX_WAITING = 500  # updates held in memory; past it the dispatcher stops reading and Redis holds the rest
MAX_PER_CHAT = 50  # one chat flooding itself cannot grow the process; the overflow is dropped and counted

Handler = Callable[[dict[str, Any]], Awaitable[None]]


def chat_of(update: dict[str, Any]) -> int | None:
    """The chat an update belongs to (a message's chat, or the chat a pressed button sits in)."""
    for part in (update.get("message"), (update.get("callback_query") or {}).get("message")):
        chat = (part or {}).get("chat")
        chat_id = chat.get("id") if isinstance(chat, dict) else None
        if isinstance(chat_id, int):
            return chat_id
    return None


def is_cancel(update: dict[str, Any]) -> bool:
    """`/cancel` or `/cancel@botname`, as a message's first word."""
    text = (update.get("message") or {}).get("text")
    if not isinstance(text, str) or not text.strip():
        return False
    head = text.split(maxsplit=1)[0]
    return head == "/cancel" or head.startswith("/cancel@")


class Inbox:
    def __init__(self, client: redis.Redis, domain: str) -> None:
        self._redis = client
        self._inbox = f"telegram:inbox:{domain}"
        self._processing = f"telegram:inbox:{domain}:processing"

    @staticmethod
    def _pack(update: dict[str, Any], attempts: int = 0) -> str:
        return json.dumps({"u": update, "n": attempts}, separators=(",", ":"))

    async def push(self, update: dict[str, Any]) -> None:
        await self._redis.lpush(self._inbox, self._pack(update))

    async def take(self, timeout: int) -> tuple[str, dict[str, Any]] | None:
        """(raw entry, update) of the oldest waiting update, moved to the processing list; None after `timeout` seconds of nothing."""
        while True:
            taken = await self._redis.blmove(self._inbox, self._processing, timeout, "RIGHT", "LEFT")
            if taken is None:
                return None
            raw = taken if isinstance(taken, str) else taken.decode()
            try:
                update = json.loads(raw)["u"]
                if not isinstance(update, dict):
                    raise TypeError("not an update")
            except (ValueError, KeyError, TypeError):
                logger.warning("telegram_inbox_unreadable_entry")
                metrics.channel_telegram_update_total.labels(outcome="dropped").inc()
                await self.done(raw)  # a bad entry must not stall the queue behind it
                continue
            return raw, update

    async def done(self, raw: str) -> None:
        await self._redis.lrem(self._processing, 1, raw)

    async def release(self, raw: str) -> None:
        """Put an update that was taken but never started back at the front, unchanged (a clean shutdown: not a crash, so not an attempt)."""
        await self._redis.rpush(self._inbox, raw)
        await self._redis.lrem(self._processing, 1, raw)

    async def requeue_unfinished(self) -> int:
        """Start-up: whatever a dead process left in flight goes back at the front with one more attempt counted; the over-limit are dropped."""
        requeued = 0
        for item in await self._redis.lrange(self._processing, 0, -1):
            raw = item if isinstance(item, str) else item.decode()
            try:
                entry = json.loads(raw)
                attempts = int(entry["n"]) + 1
                update = entry["u"]
            except (ValueError, KeyError, TypeError):
                attempts, update = MAX_ATTEMPTS, None
            if update is None or attempts >= MAX_ATTEMPTS:
                logger.warning("telegram_inbox_update_dropped", extra={"attempts": attempts})
                metrics.channel_telegram_update_total.labels(outcome="dropped").inc()
            else:
                await self._redis.rpush(self._inbox, self._pack(update, attempts))
                metrics.channel_telegram_update_total.labels(outcome="requeued").inc()
                requeued += 1
            await self._redis.lrem(self._processing, 1, raw)
        return requeued


class Dispatcher:
    def __init__(self, inbox: Inbox, handler: Handler, *, max_concurrent: int) -> None:
        self._inbox = inbox
        self._handler = handler
        self._slots = asyncio.Semaphore(max_concurrent)
        self._chats: dict[int | None, deque[tuple[str, dict[str, Any]]]] = {}
        self._tasks: set[asyncio.Task[Any]] = set()
        self._waiting = 0
        self._stopping = False

    async def run(self, stop: asyncio.Event, *, poll_seconds: int = 1) -> None:
        """Read until `stop`, then let what is running finish; what has not started goes back to the inbox untouched."""
        await self._inbox.requeue_unfinished()
        while not stop.is_set():
            if self._waiting >= MAX_WAITING:
                await asyncio.sleep(poll_seconds)
                continue
            try:
                taken = await self._inbox.take(poll_seconds)
            except Exception:  # a Redis blip must not end the bot; what was taken stays on the processing list
                logger.warning("telegram_inbox_read_failed", exc_info=True)
                await asyncio.sleep(poll_seconds)
                continue
            if taken is None:
                continue
            self._route(*taken)
        self._stopping = True
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        for queue in self._chats.values():
            for raw, _ in queue:
                await self._inbox.release(raw)
        self._chats.clear()

    def _spawn(self, coroutine: Awaitable[None]) -> None:
        task = asyncio.ensure_future(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _route(self, raw: str, update: dict[str, Any]) -> None:
        if is_cancel(update):
            self._spawn(self._handle(raw, update))  # never queued behind the turn it is meant to stop
            return
        chat = chat_of(update)
        queue = self._chats.get(chat)
        if queue is not None:
            if len(queue) >= MAX_PER_CHAT:
                metrics.channel_telegram_update_total.labels(outcome="overflow").inc()
                self._spawn(self._inbox.done(raw))
                return
            queue.append((raw, update))
            self._waiting += 1
            return
        self._chats[chat] = deque([(raw, update)])
        self._waiting += 1
        self._spawn(self._serve_chat(chat))

    async def _serve_chat(self, chat: int | None) -> None:
        async with self._slots:
            queue = self._chats[chat]
            while queue and not self._stopping:
                raw, update = queue.popleft()
                self._waiting -= 1
                await self._handle(raw, update)
            # No await between the emptiness check and the delete, so a message routed meanwhile either found the queue (and is served above)
            # or finds no entry and starts a new worker.
            if not queue:
                del self._chats[chat]

    async def _handle(self, raw: str, update: dict[str, Any]) -> None:
        try:
            await self._handler(update)
        except Exception:  # one update's failure is counted and dropped; retrying a poison update would only repeat it
            logger.exception("telegram_update_failed")
            metrics.channel_telegram_update_total.labels(outcome="error").inc()
        else:
            metrics.channel_telegram_update_total.labels(outcome="handled").inc()
        await self._inbox.done(raw)
