"""Approving a paused action from a Telegram button (specs/021 FR-001 to FR-004). This module is the STATE and the words; the flow that uses them is
`Bot` in app/adapters/inbound/channels/telegram.py.

What makes a button safe to put in a chat (constitution Principle II is non-negotiable, so each line below is a test):

- **A tap is the only thing that approves.** The turn is sent without `unattended`, the worker ends it at `approval_required`, and nothing resumes it
  but `POST /chat/resume` through the door after a tap. There is no timeout that approves and no default.
- **Only the person the conversation belongs to counts.** The record names the Telegram user the approval was shown for; a tap from anyone else (a
  group chat shows the buttons to everyone) is answered and does nothing. The API repeats the check for the signed-in person (`/chat/resume` is a 404
  for anyone but the conversation's owner).
- **One decision.** The first tap claims the approval (`SET NX`); a second tap, or a redelivered callback, finds the claim and does nothing. A claim
  whose resume the door refused is released so the person can tap again.
- **It expires, and expiry never acts.** The record lives `TELEGRAM_APPROVAL_TTL_SECONDS`. A tap after that only says so. The paused conversation
  is cancelled the NEXT time the person writes (never from a late tap: cancelling sets a flag that would stop a turn that is running by then).
- **`callback_data` carries a random token, not the decision's subject** (Telegram allows 64 bytes): what is being approved is looked up server-side.
"""
import json
import secrets
from dataclasses import asdict, dataclass

import redis.asyncio as redis

CALLBACK_PREFIX = "ap"
_ARG_CHARS = 160  # per argument: enough to see what will happen, not enough to flood a chat with a pasted document
_TEXT_CHARS = 3000
_CLAIM_SECONDS = 600


@dataclass(frozen=True)
class Pending:
    token: str
    user_id: int
    chat_id: int
    thread_id: str
    text: str  # what the approval message said, so a refused resume can put the same message (and buttons) back
    message_id: int | None = None


def callback_data(token: str, approve: bool) -> str:
    return f"{CALLBACK_PREFIX}:{token}:{'y' if approve else 'n'}"


def parse_callback(data: object) -> tuple[str, bool] | None:
    """(token, approved) of a button this module made, else None (a stale keyboard from another version, or someone else's data)."""
    if not isinstance(data, str):
        return None
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != CALLBACK_PREFIX or parts[2] not in ("y", "n") or not parts[1]:
        return None
    return parts[1], parts[2] == "y"


def keyboard(token: str) -> dict:
    return {"inline_keyboard": [[{"text": "✅ Approve", "callback_data": callback_data(token, True)}, {"text": "❌ Reject", "callback_data": callback_data(token, False)}]]}


def _shorten(value: object) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= _ARG_CHARS else text[: _ARG_CHARS - 1] + "…"


def render(tool_calls: list[dict]) -> str:
    """What the person is asked to decide: every pending call, its name and its arguments, clipped."""
    lines = ["⚠️ This needs your approval before it runs:", ""]
    for call in tool_calls:
        raw_args = call.get("args")
        args = raw_args if isinstance(raw_args, dict) else {}
        shown = ", ".join(f"{key}={_shorten(value)}" for key, value in args.items())
        lines.append(f"• {call.get('name', '?')}({shown})")
    text = "\n".join(lines)
    return text if len(text) <= _TEXT_CHARS else text[: _TEXT_CHARS - 1] + "…"


class ApprovalStore:
    def __init__(self, client: redis.Redis, domain: str, *, ttl_seconds: int) -> None:
        self._redis = client
        self._domain = domain
        self._ttl = ttl_seconds

    def _record(self, token: str) -> str:
        return f"telegram:approval:{self._domain}:{token}"

    def _claim(self, token: str) -> str:
        return f"telegram:approval-claim:{self._domain}:{token}"

    def _thread(self, thread_id: str) -> str:
        return f"telegram:approval-thread:{self._domain}:{thread_id}"

    async def create(self, *, user_id: int, chat_id: int, thread_id: str, text: str) -> Pending:
        pending = Pending(secrets.token_urlsafe(16), user_id, chat_id, thread_id, text)
        await self._save(pending)
        await self._redis.set(self._thread(thread_id), pending.token, ex=self._ttl)
        return pending

    async def _save(self, pending: Pending) -> None:
        await self._redis.set(self._record(pending.token), json.dumps(asdict(pending)), ex=self._ttl)

    async def attach_message(self, pending: Pending, message_id: int) -> Pending:
        """Remember which message carries the buttons (known only after it was sent). The record keeps its original lifetime."""
        updated = Pending(**{**asdict(pending), "message_id": message_id})
        await self._save(updated)
        return updated

    async def get(self, token: str) -> Pending | None:
        raw = await self._redis.get(self._record(token))
        if raw is None:
            return None
        try:
            return Pending(**json.loads(raw))
        except (ValueError, TypeError):
            return None

    async def for_thread(self, thread_id: str) -> Pending | None:
        token = await self._redis.get(self._thread(thread_id))
        return None if token is None else await self.get(token if isinstance(token, str) else token.decode())

    async def claim(self, token: str) -> bool:
        """True for exactly one caller per approval."""
        return bool(await self._redis.set(self._claim(token), "1", ex=_CLAIM_SECONDS, nx=True))

    async def release(self, token: str) -> None:
        await self._redis.delete(self._claim(token))

    async def finish(self, pending: Pending) -> None:
        """The decision was applied: forget the approval. The claim stays until it expires, so a late duplicate tap still finds it taken."""
        await self._redis.delete(self._record(pending.token))
        await self._redis.delete(self._thread(pending.thread_id))
