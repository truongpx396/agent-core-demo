"""One reply as one message that grows (specs/021 FR-005). Telegram has no token streaming, so the bot sends the first words as a message and edits
that message as more arrive, at most once per `interval` seconds, instead of leaving the person looking at "typing" until the whole answer exists.

Two rules keep this from ever being worse than waiting:
- **The last word is not throttled and is never lost.** `finish` always sets the final text: it edits the growing message, and when the text is
  longer than one message (or the edit was refused) it sends the rest, or all of it, as new messages.
- **A refused edit is ignored, not retried in a loop.** Telegram's edit limits are not documented where they could be read (specs/021 research), so
  the interval is a floor and a failed edit just waits for the next one.
"""
import time
from collections.abc import Callable

from app.adapters.inbound.channels.telegram.api import (
    MESSAGE_CHAR_LIMIT,
    BotApi,
    split_text,
)


class ReplySink:
    def __init__(self, api: BotApi, chat_id: int, *, interval: float, enabled: bool = True, clock: Callable[[], float] = time.monotonic) -> None:
        self._api = api
        self._chat_id = chat_id
        self._interval = interval
        self._enabled = enabled
        self._clock = clock
        self._message_id: int | None = None
        self._shown = ""
        self._last = 0.0

    async def push(self, text_so_far: str) -> None:
        """Show the text so far, if enough time has passed since the last time and it still fits one message."""
        if not self._enabled or not text_so_far.strip() or len(text_so_far) > MESSAGE_CHAR_LIMIT or text_so_far == self._shown:
            return
        now = self._clock()
        if self._message_id is not None and now - self._last < self._interval:
            return
        self._last = now
        if self._message_id is None:
            self._message_id = await self._api.send_message(self._chat_id, text_so_far)
            if self._message_id is None:
                self._enabled = False  # the first message did not go: do not keep trying to edit one that does not exist
                return
        elif not await self._api.edit_message(self._chat_id, self._message_id, text_so_far):
            return
        self._shown = text_so_far

    async def finish(self, text: str) -> None:
        """Make `text` (any length) the reply: the first part replaces the growing message when there is one, the rest follow as new messages."""
        chunks = [chunk for chunk in split_text(text) if chunk]
        if not chunks:
            return
        rest = chunks
        if self._message_id is not None:
            if chunks[0] == self._shown or await self._api.edit_message(self._chat_id, self._message_id, chunks[0]):
                rest = chunks[1:]
        for chunk in rest:
            await self._api.send_message(self._chat_id, chunk)
