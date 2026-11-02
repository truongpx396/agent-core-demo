"""Fakes the Telegram channel's tests share (specs/021). Nothing here touches a network, a queue or a model.

`FakeRedis` covers exactly the commands the channel's own state uses (plain keys with `ex`/`nx`, `getdel`, and the inbox's lists); it is not the
Streams fake in tests/job_queue/test_queue.py, which covers a different set. What a passing test proves is the channel's logic against these commands'
documented behaviour, not Redis itself (testing.md: a fake proves statement shape, not that a real server behaves).
"""
import asyncio

from app.channels.door import DoorRefused
from app.channels.oauth import SignInRefused


class FakeRedis:
    def __init__(self) -> None:
        self.kv: dict[str, str] = {}
        self.ttls: dict[str, int] = {}
        self.lists: dict[str, list[str]] = {}

    async def set(self, key, value, ex=None, nx=False):
        if nx and key in self.kv:
            return None
        self.kv[key] = value
        if ex is not None:
            self.ttls[key] = ex
        return True

    async def get(self, key):
        return self.kv.get(key)

    async def getdel(self, key):
        return self.kv.pop(key, None)

    async def delete(self, *keys):
        return sum(1 for key in keys if self.kv.pop(key, None) is not None or self.lists.pop(key, None) is not None)

    # Lists: `lpush` adds at the head (index 0), `rpush` at the tail; the inbox reads from the tail, so lpush-then-pop is first in, first out.
    async def lpush(self, key, value):
        self.lists.setdefault(key, []).insert(0, value)

    async def rpush(self, key, value):
        self.lists.setdefault(key, []).append(value)

    async def lrange(self, key, start, stop):
        items = self.lists.get(key, [])
        return list(items if stop == -1 else items[start : stop + 1])

    async def lrem(self, key, count, value):
        items = self.lists.get(key, [])
        if value in items:
            items.remove(value)
            return 1
        return 0

    async def blmove(self, source, destination, timeout, src, dest):
        items = self.lists.get(source, [])
        if not items:
            await asyncio.sleep(0.005)  # a real one blocks; yielding is what keeps a polling loop from starving the others
            return None
        value = items.pop() if src == "RIGHT" else items.pop(0)
        target = self.lists.setdefault(destination, [])
        target.insert(0, value) if dest == "LEFT" else target.append(value)
        return value


class FakeApi:
    """Stands in for `BotApi`: records what the person would see."""

    def __init__(self, *, send_fails: bool = False, edit_fails: bool = False) -> None:
        self.calls: list[tuple] = []
        self._next_id = 100
        self.send_fails = send_fails
        self.edit_fails = edit_fails

    async def send_message(self, chat_id, text, *, reply_markup=None):
        self.calls.append(("send", chat_id, text, reply_markup))
        if self.send_fails:
            return None
        self._next_id += 1
        return self._next_id

    async def edit_message(self, chat_id, message_id, text, *, reply_markup=None):
        self.calls.append(("edit", chat_id, message_id, text, reply_markup or {"inline_keyboard": []}))  # as BotApi.edit_message: always states the keyboard
        return not self.edit_fails

    async def answer_callback(self, callback_id, text="", *, alert=False):
        self.calls.append(("answer", callback_id, text, alert))

    async def typing(self, chat_id):
        self.calls.append(("typing", chat_id))

    def of(self, kind: str) -> list[tuple]:
        return [call for call in self.calls if call[0] == kind]

    def sent_texts(self) -> list[str]:
        return [call[2] for call in self.of("send")]


class FakeDoor:
    """Stands in for `Door`. Each call to `stream_turn` / `stream_resume` plays the next scripted list of events (`[{"type": "done"}]` when none is left)."""

    def __init__(self, turns=(), resumes=(), *, cancel_fails: bool = False) -> None:
        self.scripts = list(turns)
        self.resume_scripts = list(resumes)
        self.turns: list[dict] = []
        self.resumes: list[tuple[str, bool]] = []
        self.cancels: list[tuple[str, bool]] = []
        self.cancel_fails = cancel_fails

    async def stream_turn(self, text, thread_id, **kwargs):
        self.turns.append({"text": text, "thread_id": thread_id, **kwargs})
        for event in (self.scripts.pop(0) if self.scripts else [{"type": "done"}]):
            yield event

    async def stream_resume(self, thread_id, approved):
        self.resumes.append((thread_id, approved))
        for event in (self.resume_scripts.pop(0) if self.resume_scripts else [{"type": "done"}]):
            yield event

    async def cancel_turn(self, thread_id, *, wait=False):
        self.cancels.append((thread_id, wait))
        if self.cancel_fails:
            raise DoorRefused("the door went away")


class FakeChannel:
    """Stands in for `Channel`: every user is the person `p-<user id>`, or is refused with `refusal`."""

    def __init__(self, door, *, refusal=None) -> None:
        self.door = door
        self.refusal = refusal
        self.asked: list[int] = []

    async def door_for(self, user_id):
        self.asked.append(user_id)
        if self.refusal is not None:
            raise SignInRefused(self.refusal)
        return self.door, f"p-{user_id}"


def message(text="hello", chat_id=1, user_id=42, **extra):
    return {"chat": {"id": chat_id, "type": "private"}, "from": {"id": user_id}, "text": text, **extra}


def pause(*names):
    return {"type": "approval_required", "tool_calls": [{"name": name, "args": {"title": "x"}} for name in (names or ("add_note",))]}
