"""Tests for app/channels/telegram.py — a fake async httpx client stands in for a live Telegram API (see tests/core/test_model_resolver.py's
_FakeResponse for the sync-httpx equivalent), and the door (`app/channels/door.py`, the way the bot reaches the chat API) is replaced by a fake that
yields scripted events, so these never touch a queue/worker/graph/LLM, matching the rest of the suite's hermetic discipline. What the door and the
sign-in do with real HTTP is tested in tests/channels/test_door.py, test_oauth.py and test_telegram_people.py.
"""

import pytest

from app.channels import telegram as telegram_channel
from app.channels.oauth import SignInRefused
from app.core import metrics
from app.core.config import settings
from app.core.errors import ErrorCode, ErrorEnvelope
from tests.conftest import metric_value as _count
from tests.job_queue.test_queue import FakeRedis


class _FakeResponse:
    def __init__(self, json_data=None, status_code=200):
        self._json_data = json_data or {}
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._json_data


class _FakeAsyncClient:
    """Records every call made through it; `get`/`post` both return a
    canned _FakeResponse (configurable per test via `get_response`)."""

    def __init__(self, get_response=None):
        self.get_response = get_response or _FakeResponse({"result": []})
        self.posts: list[tuple[str, dict]] = []

    async def get(self, url, params=None):
        return self.get_response

    async def post(self, url, json=None):
        self.posts.append((url, json))
        return _FakeResponse({"ok": True})


def _message(text="hello", chat_id=1, user_id=42):
    return {"chat": {"id": chat_id}, "from": {"id": user_id}, "text": text}


class _FakeDoor:
    """Stands in for `Door`: yields `events` for a turn and records what it was asked for."""

    def __init__(self, events=(), *, cancel_fails=False):
        self.events = list(events)
        self.turns: list[dict] = []
        self.cancels: list[str] = []
        self.cancel_fails = cancel_fails

    async def stream_turn(self, text, thread_id, **kwargs):
        self.turns.append({"text": text, "thread_id": thread_id, **kwargs})
        for event in self.events:
            yield event

    async def cancel_turn(self, thread_id):
        self.cancels.append(thread_id)
        if self.cancel_fails:
            raise ConnectionError("the door went away")


class _Channel:
    """Stands in for `Channel`: every user is the person `p-<user id>`, or is refused with `refusal`."""

    def __init__(self, door, *, refusal=None):
        self.door = door
        self.refusal = refusal
        self.asked: list[int] = []

    async def door_for(self, user_id):
        self.asked.append(user_id)
        if self.refusal is not None:
            raise SignInRefused(self.refusal)
        return self.door, f"p-{user_id}"


class TestThreadId:
    def test_thread_id_is_stable_per_person_and_chat(self):
        assert telegram_channel._thread_id("p-1", 123) == telegram_channel._thread_id("p-1", 123)

    def test_thread_id_differs_across_chats(self):
        assert telegram_channel._thread_id("p-1", 1) != telegram_channel._thread_id("p-1", 2)

    def test_two_people_in_one_group_chat_get_their_own_conversations(self):
        assert telegram_channel._thread_id("p-1", 5) != telegram_channel._thread_id("p-2", 5)

    def test_it_names_its_owner_inside_the_reserved_namespace(self):
        """The API lets a caller claim a `telegram:` id only if it names them (app/api/routers/chat.py)."""
        assert telegram_channel._thread_id("abc-123", 7) == "telegram:abc-123:7"


class TestChannel:
    async def test_in_header_mode_the_user_is_named_in_headers_in_the_configured_tenant(self, monkeypatch):
        monkeypatch.setattr(settings, "auth_mode", "headers")
        monkeypatch.setattr(telegram_channel, "AGENT_DOMAIN", "support")
        channel = telegram_channel.Channel(object())

        door, principal = await channel.door_for(99)

        assert principal == "telegram:99"
        assert await door._auth.headers() == {"X-Tenant-Id": settings.default_tenant, "X-Principal-Id": "telegram:99"}
        assert door._domain == "support"

    async def test_in_identity_mode_each_user_is_the_person_they_sign_in_as(self, monkeypatch):
        monkeypatch.setattr(settings, "auth_mode", "identity")
        channel = telegram_channel.Channel(object())

        class _Person:
            def __init__(self, sub):
                self.sub = sub

            async def sign_in(self):
                return self.sub

        monkeypatch.setattr(channel.people, "person", lambda user_id: _Person(f"sub-{user_id}"))

        (_, one), (_, two) = await channel.door_for(1), await channel.door_for(2)

        assert (one, two) == ("sub-1", "sub-2")

    async def test_a_token_that_names_nobody_is_refused(self, monkeypatch):
        monkeypatch.setattr(settings, "auth_mode", "identity")
        channel = telegram_channel.Channel(object())

        class _Nobody:
            async def sign_in(self):
                return ""

        monkeypatch.setattr(channel.people, "person", lambda user_id: _Nobody())

        with pytest.raises(SignInRefused) as exc:
            await channel.door_for(1)

        assert exc.value.reason == "credentials"


class TestFormatReply:
    def test_no_citations_returns_plain_text(self):
        assert telegram_channel._format_reply("hello there", []) == "hello there"

    def test_citations_appended_as_a_sources_footer(self):
        citations = [{"marker": "[1]", "title": "Checkpointers", "doc_id": "abc"}]
        result = telegram_channel._format_reply("answer text", citations)
        assert "answer text" in result
        assert "Sources:" in result
        assert "[1] Checkpointers" in result

    def test_falls_back_to_doc_id_when_title_missing(self):
        citations = [{"marker": "[1]", "doc_id": "abc123"}]
        result = telegram_channel._format_reply("answer", citations)
        assert "abc123" in result


class TestSendMessage:
    async def test_short_message_sent_as_a_single_post(self):
        client = _FakeAsyncClient()
        await telegram_channel._send_message(client, 1, "short reply")
        assert len(client.posts) == 1
        assert client.posts[0][1]["text"] == "short reply"
        assert client.posts[0][1]["chat_id"] == 1

    async def test_long_message_is_split_across_multiple_sends(self):
        client = _FakeAsyncClient()
        long_text = "x" * (telegram_channel._MESSAGE_CHAR_LIMIT + 500)
        await telegram_channel._send_message(client, 1, long_text)
        assert len(client.posts) == 2
        assert sum(len(p[1]["text"]) for p in client.posts) == len(long_text)

    async def test_a_failed_send_is_swallowed_not_raised(self):
        class _RaisingClient(_FakeAsyncClient):
            async def post(self, url, json=None):
                raise RuntimeError("network down")

        # Must not raise — a bad chat_id can't be allowed to kill the poll loop.
        await telegram_channel._send_message(_RaisingClient(), 1, "hi")


class TestRunTurn:
    async def test_an_error_event_returns_early_and_closes_the_stream_it_abandoned(self):
        """The `error` branch returns before the stream is exhausted. A bare `async for` would leave
        the closing of what that return abandoned to the event loop's finalizer."""
        closed = []

        class _Door(_FakeDoor):
            async def stream_turn(self, text, thread_id, **kwargs):
                try:
                    yield {"type": "error", "content": "Something went wrong."}
                    yield {"type": "done"}
                finally:
                    closed.append(True)

        reply = await telegram_channel._run_turn(_Door(), "hi", "telegram:p:1")

        assert reply == ("Something went wrong.", [])
        assert closed == [True]


class TestAnUnattendedTurnThatEndsAtAPause:
    """The worker declines a pause for an unattended turn, so the channel should never see one.
    It can: a rolling deploy that upgrades this channel before its workers leaves an old worker
    running the turn as an ordinary one, which stops at `approval_required`. Nothing has run (the
    gate held), but the conversation is left paused and an unattended channel can never resolve a
    pause, so every later message would be refused. The channel cancels it rather than depend on
    the worker's version."""

    PAUSING = [
        {"type": "token", "content": "Sure, saving that"},
        {"type": "approval_required", "tool_calls": [{"name": "add_note", "args": {}}, {"name": "add_note", "args": {}}]},
    ]

    async def test_the_pause_is_cancelled_and_the_person_is_told_it_was_not_done(self):
        door = _FakeDoor(self.PAUSING)
        before = _count(metrics.agent_unattended_job_paused_total)

        reply, citations = await telegram_channel._run_turn(door, "save a note", "telegram:p:7")

        assert door.cancels == ["telegram:p:7"]
        assert _count(metrics.agent_unattended_job_paused_total) == before + 1  # visible, not only logged
        assert "add_note" in reply and "person's approval" in reply and "wasn't done" in reply
        assert "Sure, saving that" not in reply  # a half-sentence promising the write is not the answer
        assert citations == []

    async def test_a_cancel_that_fails_still_gives_the_person_a_reply(self):
        door = _FakeDoor(self.PAUSING, cancel_fails=True)

        reply, _ = await telegram_channel._run_turn(door, "save a note", "telegram:p:7")

        assert len(door.cancels) == 1
        assert "person's approval" in reply


class TestHandleMessage:
    async def test_sends_an_unattended_turn_for_the_persons_thread_and_replies(self, monkeypatch):
        door = _FakeDoor([{"type": "token", "content": "the answer"}])
        channel = _Channel(door)
        client = _FakeAsyncClient()

        await telegram_channel.handle_message(client, _message(text="hi", chat_id=7, user_id=42), channel=channel)

        (turn,) = door.turns
        assert turn["text"] == "hi"
        assert turn["thread_id"] == telegram_channel._thread_id("p-42", 7)
        # Nobody can answer an approval here, so the worker must decline it.
        assert turn["unattended"] is True
        assert any("sendMessage" in url and body["text"] == "the answer" for url, body in client.posts)

    async def test_an_update_id_becomes_a_replay_key_scoped_to_the_domain(self, monkeypatch):
        door = _FakeDoor([{"type": "done"}])
        monkeypatch.setattr(telegram_channel, "AGENT_DOMAIN", "support")

        await telegram_channel.handle_message(_FakeAsyncClient(), _message(), update_id=777, channel=_Channel(door))

        assert door.turns[0]["replay_key"] == "telegram:support:update:777"

    async def test_the_same_update_id_on_another_domains_bot_is_another_submission(self, monkeypatch):
        """Two bots share a person's conversation for one chat and each numbers its updates on its
        own, so the update id alone could hand one bot the other's reply."""
        monkeypatch.setattr(telegram_channel, "AGENT_DOMAIN", "support")
        support_key = telegram_channel._replay_key(5)
        monkeypatch.setattr(telegram_channel, "AGENT_DOMAIN", "sales")

        assert telegram_channel._replay_key(5) != support_key
        assert telegram_channel._replay_key(5) != telegram_channel._replay_key(6)

    async def test_without_an_update_id_there_is_no_replay_key(self):
        door = _FakeDoor([{"type": "done"}])

        await telegram_channel.handle_message(_FakeAsyncClient(), _message(), channel=_Channel(door))

        assert door.turns[0]["replay_key"] is None

    async def test_reply_includes_citations_footer(self):
        cited = [{"marker": "[1]", "title": "Refund Policy", "doc_id": "d1"}]
        door = _FakeDoor([{"type": "token", "content": "grounded answer [1]"}, {"type": "citations", "items": cited}])
        client = _FakeAsyncClient()

        await telegram_channel.handle_message(client, _message(), channel=_Channel(door))

        sent = next(body["text"] for url, body in client.posts if "sendMessage" in url)
        assert "Sources:" in sent

    async def test_a_turn_that_produces_no_text_still_gets_an_explicit_reply_not_silence(self):
        """Real bug (reproduced): `_send_message` loops over `range(0, len(text), ...)`, so an EMPTY reply sends
        nothing at all.
        """
        client = _FakeAsyncClient()

        await telegram_channel.handle_message(
            client, _message(text="save a note", chat_id=7), channel=_Channel(_FakeDoor([{"type": "done"}]))
        )

        sent = [body["text"] for url, body in client.posts if "sendMessage" in url]
        assert sent and sent[0].strip(), "an empty turn must still produce a visible reply"

    async def test_non_text_message_is_skipped_without_signing_anyone_in_or_sending_a_turn(self):
        door = _FakeDoor()
        channel = _Channel(door)
        client = _FakeAsyncClient()

        await telegram_channel.handle_message(client, {"chat": {"id": 1}, "from": {"id": 1}}, channel=channel)

        assert (channel.asked, door.turns, client.posts) == ([], [], [])

    async def test_an_error_envelope_still_produces_a_reply_not_a_crash(self):
        """The worker's `error` event already carries a real message text — this just proves the channel doesn't
        need any special-casing for that; it just forwards it."""
        envelope = ErrorEnvelope(code=ErrorCode.TIMEOUT, message="Sorry, that took too long.")
        client = _FakeAsyncClient()

        await telegram_channel.handle_message(
            client, _message(), channel=_Channel(_FakeDoor([{"type": "error", "content": envelope.message}]))
        )

        sent = next(body["text"] for url, body in client.posts if "sendMessage" in url)
        assert sent == "Sorry, that took too long."


class TestAUserWhoCannotBeSignedIn:
    """Specs/020 FR-006 and FR-012: the person is told what is wrong, in words, and NOTHING is sent to the agent."""

    @pytest.mark.parametrize(
        ("reason", "expected"),
        [
            ("unknown_person", "not registered"),
            ("forbidden", "not allowed"),
            ("credentials", "sign-in was refused"),
            ("unavailable", "unavailable"),
        ],
    )
    async def test_the_reply_says_why_and_no_turn_is_sent(self, reason, expected):
        door = _FakeDoor([{"type": "token", "content": "must never be shown"}])
        client = _FakeAsyncClient()

        await telegram_channel.handle_message(client, _message(user_id=42), channel=_Channel(door, refusal=reason))

        (sent,) = [body["text"] for url, body in client.posts if "sendMessage" in url]
        assert expected in sent
        assert door.turns == []
        assert "must never be shown" not in sent

    async def test_an_unregistered_user_is_given_the_id_an_administrator_needs(self):
        client = _FakeAsyncClient()

        await telegram_channel.handle_message(client, _message(user_id=424242), channel=_Channel(_FakeDoor(), refusal="unknown_person"))

        sent = next(body["text"] for url, body in client.posts if "sendMessage" in url)
        assert "424242" in sent


class TestRun:
    async def test_refuses_to_start_without_a_bot_token(self, monkeypatch):
        monkeypatch.setattr(telegram_channel, "TELEGRAM_BOT_TOKEN", "")
        with pytest.raises(RuntimeError, match="TELEGRAM_BOT_TOKEN"):
            await telegram_channel.run()

    async def test_refuses_to_start_in_identity_mode_without_the_secret_the_people_are_derived_from(self, monkeypatch):
        monkeypatch.setattr(telegram_channel, "TELEGRAM_BOT_TOKEN", "fake-token")
        monkeypatch.setattr(settings, "auth_mode", "identity")
        monkeypatch.setattr(settings, "telegram_person_secret", "too short")

        with pytest.raises(SystemExit, match="TELEGRAM_PERSON_SECRET"):
            await telegram_channel.run()

    async def test_validates_agent_domain_before_polling(self, monkeypatch):
        """AGENT_DOMAIN must be checked BEFORE the poll loop starts, so a typo'd domain fails at startup instead of
        sending to a stream no worker pool reads."""
        monkeypatch.setattr(telegram_channel, "TELEGRAM_BOT_TOKEN", "fake-token")
        monkeypatch.setattr(settings, "auth_mode", "headers")
        monkeypatch.setattr(telegram_channel, "AGENT_DOMAIN", "no-such-domain")

        with pytest.raises(RuntimeError, match="no-such-domain"):
            await telegram_channel.run()


class TestOffsetPersistence:
    """`_load_offset`/`_save_offset` are the fix for a real bug: `offset`
    used to live only in a local variable inside run(), so any process
    restart reset it to 0 and Telegram would redeliver every update it
    still remembers — every already-handled message since the last
    restart, each producing a fresh duplicate turn and reply to a real
    user. See run()'s own docstring for the full reasoning."""

    async def test_load_returns_zero_when_nothing_persisted_yet(self):
        client = FakeRedis()
        assert await telegram_channel._load_offset(client, "ecorp") == 0

    async def test_save_then_load_round_trips(self):
        client = FakeRedis()
        await telegram_channel._save_offset(client, "ecorp", 12345)
        assert await telegram_channel._load_offset(client, "ecorp") == 12345

    async def test_offsets_are_scoped_per_domain(self):
        """Each domain runs as its own process/bot token — a support
        channel's offset must never leak into (or be clobbered by) a
        sales channel's, even sharing the same Redis."""
        client = FakeRedis()
        await telegram_channel._save_offset(client, "support", 10)
        await telegram_channel._save_offset(client, "sales", 20)

        assert await telegram_channel._load_offset(client, "support") == 10
        assert await telegram_channel._load_offset(client, "sales") == 20


class _StopHere(BaseException):
    """BaseException, not Exception: raised from INSIDE getUpdates, which run()'s own poll loop wraps in a broad
    `except Exception` (a real poll failure must not kill the loop) — an ordinary Exception would be swallowed
    and retried forever instead of ending the test."""


class _ClientBase:
    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class TestRunPersistsOffsetAcrossPolls:
    @pytest.fixture(autouse=True)
    def a_running_bot(self, monkeypatch):
        monkeypatch.setattr(telegram_channel, "TELEGRAM_BOT_TOKEN", "fake-token")
        monkeypatch.setattr(telegram_channel, "AGENT_DOMAIN", "ecorp")
        monkeypatch.setattr(settings, "auth_mode", "headers")

    async def test_resumes_from_the_previously_persisted_offset(self, monkeypatch):
        """A fresh process must pick up where the last one left off, not restart from 0 (Telegram's own update
        history) — the actual regression this whole fix exists for."""
        redis_client = FakeRedis()
        await telegram_channel._save_offset(redis_client, "ecorp", 500)
        seen_offsets = []

        class _RecordingThenStoppingClient(_ClientBase):
            async def get(self, url, params=None):
                seen_offsets.append(params["offset"])
                raise _StopHere()

        monkeypatch.setattr(telegram_channel, "get_redis_client", lambda: redis_client)
        monkeypatch.setattr(telegram_channel.httpx, "AsyncClient", _RecordingThenStoppingClient)

        with pytest.raises(_StopHere):
            await telegram_channel.run()

        assert seen_offsets == [500]

    async def test_persists_the_new_offset_after_each_message_is_handled(self, monkeypatch):
        """`_save_offset` is called from the `for update in updates:` body, NOT inside run()'s narrow
        `except Exception` around getUpdates — raising from there stops the loop right after the one behavior
        this test cares about, without depending on run()'s own retry/sleep mechanics at all."""
        redis_client = FakeRedis()
        real_save_offset = telegram_channel._save_offset

        async def _save_offset_then_stop(client, domain, offset):
            await real_save_offset(client, domain, offset)
            raise _StopHere()

        class _OneBatchClient(_ClientBase):
            async def get(self, url, params=None):
                return _FakeResponse({"result": [{"update_id": 777, "message": _message()}]})

            async def post(self, url, json=None):
                return _FakeResponse({"ok": True})

        door = _FakeDoor([{"type": "token", "content": "ok"}])

        async def door_for(self, user_id):
            return door, f"p-{user_id}"

        monkeypatch.setattr(telegram_channel.Channel, "door_for", door_for)
        monkeypatch.setattr(telegram_channel, "get_redis_client", lambda: redis_client)
        monkeypatch.setattr(telegram_channel, "_save_offset", _save_offset_then_stop)
        monkeypatch.setattr(telegram_channel.httpx, "AsyncClient", _OneBatchClient)

        with pytest.raises(_StopHere):
            await telegram_channel.run()

        assert await telegram_channel._load_offset(redis_client, "ecorp") == 778
        # The poll loop is where the update id enters. Dropped here, nothing else notices: every message still
        # gets a reply, and a redelivered one quietly becomes a second turn again.
        assert door.turns[0]["replay_key"] == "telegram:ecorp:update:777"
