"""Tests for app/channels/telegram.py — a fake async httpx client stands in
for a live Telegram API (see tests/core/test_model_resolver.py's _FakeResponse
for the sync-httpx equivalent), and
app.channels.telegram.stream_turn is monkeypatched (as a fake async generator)
so these never touch a queue/worker/graph/LLM, matching the rest of the
suite's hermetic discipline. `TestRedeliveryThroughTheQueue` is the one exception:
it keeps the real producer and puts a fake Redis whose "worker" answers under it,
because the channel's promise there is about what reaches the queue.
"""

import json

import pytest

from app.channels import telegram as telegram_channel
from app.core import metrics
from app.core.errors import ErrorCode, ErrorEnvelope
from app.job_queue import producer, queue
from tests.conftest import metric_value as _count
from tests.job_queue.test_producer import AnsweringRedis
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


class TestThreadAndCtx:
    def test_thread_id_is_stable_per_chat(self):
        assert telegram_channel._thread_id_for_chat(123) == telegram_channel._thread_id_for_chat(123)

    def test_thread_id_differs_across_chats(self):
        assert telegram_channel._thread_id_for_chat(1) != telegram_channel._thread_id_for_chat(2)

    def test_ctx_scopes_by_telegram_user_id(self):
        ctx = telegram_channel._ctx_for_user(99)
        assert ctx["principal"] == "telegram:99"
        assert ctx["tenant"]
        assert ctx["claims"] == {}

    def test_different_users_get_different_principals(self):
        assert telegram_channel._ctx_for_user(1)["principal"] != telegram_channel._ctx_for_user(2)["principal"]


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


def _fake_stream_turn(events):
    """Builds a fake `stream_turn` replacement that yields `events` and records what it was
    called with: the (text, thread_id, ctx) onto `captured["args"]`, the keywords onto
    `captured["kwargs"]`."""
    captured = {}

    async def _fake(text, thread_id, ctx, *, domain, require_approval=False, unattended=False, dedup_key=None):
        captured["args"] = (text, thread_id, ctx)
        captured["kwargs"] = {
            "domain": domain,
            "require_approval": require_approval,
            "unattended": unattended,
            "dedup_key": dedup_key,
        }
        for event in events:
            yield event

    return _fake, captured


class TestRunTurn:
    async def test_an_error_event_returns_early_and_closes_the_stream_it_abandoned(self, monkeypatch):
        """The `error` branch returns before the stream is exhausted. A bare `async for` would leave
        the closing of what that return abandoned to the event loop's finalizer."""
        closed = []

        async def fake_stream_turn(text, thread_id, ctx, **kwargs):
            try:
                yield {"type": "error", "content": "Something went wrong."}
                yield {"type": "done"}
            finally:
                closed.append(True)

        monkeypatch.setattr(telegram_channel, "stream_turn", fake_stream_turn)

        reply = await telegram_channel._run_turn("hi", "telegram:1", object())

        assert reply == ("Something went wrong.", [])
        assert closed == [True]


class TestAnUnattendedJobThatEndsAtAPause:
    """The worker declines a pause for an unattended job, so the channel should never see one.
    It can: a rolling deploy that upgrades this channel before its workers leaves an old worker
    running the job as an ordinary turn, which stops at `approval_required`. Nothing has run (the
    gate held), but the conversation is left paused and an unattended channel can never resolve a
    pause, so every later message would be refused. The channel cancels it rather than depend on
    the worker's version."""

    @staticmethod
    def _pausing_stream(monkeypatch, cancels, *, cancel_fails=False):
        async def fake_stream_turn(text, thread_id, ctx, **kwargs):
            yield {"type": "token", "content": "Sure, saving that"}
            yield {
                "type": "approval_required",
                "tool_calls": [{"name": "add_note", "args": {}}, {"name": "add_note", "args": {}}],
            }

        async def fake_cancel_turn(thread_id, ctx, **kwargs):
            cancels.append({"thread_id": thread_id, "ctx": ctx, **kwargs})
            if cancel_fails:
                raise ConnectionError("redis went away")

        monkeypatch.setattr(telegram_channel, "stream_turn", fake_stream_turn)
        monkeypatch.setattr(telegram_channel, "cancel_turn", fake_cancel_turn)

    async def test_the_pause_is_cancelled_and_the_person_is_told_it_was_not_done(self, monkeypatch):
        cancels = []
        self._pausing_stream(monkeypatch, cancels)
        monkeypatch.setattr(telegram_channel, "AGENT_DOMAIN", "support")
        ctx = {"tenant": "ecorp", "principal": "telegram:42", "claims": {}}

        before = _count(metrics.agent_unattended_job_paused_total)

        reply, citations = await telegram_channel._run_turn("save a note", "telegram:7", ctx)

        assert [(c["thread_id"], c["domain"], c["ctx"]) for c in cancels] == [("telegram:7", "support", ctx)]
        assert _count(metrics.agent_unattended_job_paused_total) == before + 1  # visible, not only logged
        assert "add_note" in reply and "person's approval" in reply and "wasn't done" in reply
        assert "Sure, saving that" not in reply  # a half-sentence promising the write is not the answer
        assert citations == []

    async def test_a_cancel_that_fails_still_gives_the_person_a_reply(self, monkeypatch):
        cancels = []
        self._pausing_stream(monkeypatch, cancels, cancel_fails=True)

        reply, _ = await telegram_channel._run_turn("save a note", "telegram:7", object())

        assert len(cancels) == 1
        assert "person's approval" in reply


class TestHandleMessage:
    async def test_publishes_an_unattended_turn_for_the_scoped_thread_and_ctx_and_replies(
        self, monkeypatch
    ):
        fake, captured = _fake_stream_turn([{"type": "token", "content": "the answer"}])
        monkeypatch.setattr(telegram_channel, "stream_turn", fake)
        monkeypatch.setattr(telegram_channel, "AGENT_DOMAIN", "support")
        client = _FakeAsyncClient()

        await telegram_channel.handle_message(client, _message(text="hi", chat_id=7, user_id=42))

        text, thread_id, ctx = captured["args"]
        assert text == "hi"
        assert thread_id == telegram_channel._thread_id_for_chat(7)
        assert ctx["principal"] == "telegram:42"
        # Nobody can answer an approval here, so the worker must decline it; and the job goes to
        # the pool of the domain this process serves.
        assert captured["kwargs"]["unattended"] is True
        assert captured["kwargs"]["domain"] == "support"
        # sendChatAction + sendMessage
        assert any("sendMessage" in url and body["text"] == "the answer" for url, body in client.posts)

    async def test_an_update_id_becomes_a_dedup_key_scoped_to_the_domain(self, monkeypatch):
        fake, captured = _fake_stream_turn([{"type": "done"}])
        monkeypatch.setattr(telegram_channel, "stream_turn", fake)
        monkeypatch.setattr(telegram_channel, "AGENT_DOMAIN", "support")

        await telegram_channel.handle_message(_FakeAsyncClient(), _message(), update_id=777)

        assert captured["kwargs"]["dedup_key"] == "telegram:support:update:777"

    async def test_the_same_update_id_on_another_domains_bot_is_another_submission(self, monkeypatch):
        """Two bots share `telegram:<chat id>` for one person and each numbers its updates on its
        own, so the update id alone could hand one bot the other's reply."""
        monkeypatch.setattr(telegram_channel, "AGENT_DOMAIN", "support")
        support_key = telegram_channel._dedup_key(5)
        monkeypatch.setattr(telegram_channel, "AGENT_DOMAIN", "sales")

        assert telegram_channel._dedup_key(5) != support_key
        assert telegram_channel._dedup_key(5) != telegram_channel._dedup_key(6)

    async def test_without_an_update_id_there_is_no_dedup_key(self, monkeypatch):
        fake, captured = _fake_stream_turn([{"type": "done"}])
        monkeypatch.setattr(telegram_channel, "stream_turn", fake)

        await telegram_channel.handle_message(_FakeAsyncClient(), _message())

        assert captured["kwargs"]["dedup_key"] is None

    async def test_reply_includes_citations_footer(self, monkeypatch):
        cited = [{"marker": "[1]", "title": "Refund Policy", "doc_id": "d1"}]
        fake, _ = _fake_stream_turn(
            [
                {"type": "token", "content": "grounded answer [1]"},
                {"type": "citations", "items": cited},
            ]
        )
        monkeypatch.setattr(telegram_channel, "stream_turn", fake)
        client = _FakeAsyncClient()

        await telegram_channel.handle_message(client, _message())

        sent = next(body["text"] for url, body in client.posts if "sendMessage" in url)
        assert "Sources:" in sent

    async def test_a_turn_that_produces_no_text_still_gets_an_explicit_reply_not_silence(self, monkeypatch):
        """Real bug (reproduced): `_send_message` loops over `range(0, len(text), ...)`, so an EMPTY reply sends
        nothing at all.
        """
        fake, _ = _fake_stream_turn([{"type": "done"}])
        monkeypatch.setattr(telegram_channel, "stream_turn", fake)
        client = _FakeAsyncClient()

        await telegram_channel.handle_message(client, _message(text="save a note", chat_id=7))

        sent = [body["text"] for url, body in client.posts if "sendMessage" in url]
        assert sent and sent[0].strip(), "an empty turn must still produce a visible reply"

    async def test_non_text_message_is_skipped_without_publishing_a_turn(self, monkeypatch):
        called = []

        def fake(*a, **kw):
            called.append(1)
            raise AssertionError("should not be called")

        monkeypatch.setattr(telegram_channel, "stream_turn", fake)
        client = _FakeAsyncClient()

        await telegram_channel.handle_message(client, {"chat": {"id": 1}, "from": {"id": 1}})

        assert called == []
        assert client.posts == []

    async def test_an_error_envelope_still_produces_a_reply_not_a_crash(self, monkeypatch):
        """The worker's `error` event already carries a
        real message text (see _run_graph_stream's docstring) — this just
        proves the channel doesn't need any special-casing for that; it
        just forwards it."""
        envelope = ErrorEnvelope(code=ErrorCode.TIMEOUT, message="Sorry, that took too long.")
        fake, _ = _fake_stream_turn([{"type": "error", "content": envelope.message}])
        monkeypatch.setattr(telegram_channel, "stream_turn", fake)
        client = _FakeAsyncClient()

        await telegram_channel.handle_message(client, _message())

        sent = next(body["text"] for url, body in client.posts if "sendMessage" in url)
        assert sent == "Sorry, that took too long."


class TestRun:
    async def test_refuses_to_start_without_a_bot_token(self, monkeypatch):
        monkeypatch.setattr(telegram_channel, "TELEGRAM_BOT_TOKEN", "")
        with pytest.raises(RuntimeError, match="TELEGRAM_BOT_TOKEN"):
            await telegram_channel.run()

    async def test_validates_agent_domain_before_polling_and_never_builds_a_graph(self, monkeypatch):
        """AGENT_DOMAIN (app/core/config.py) must be resolved BEFORE the poll loop starts, so a typo'd domain
        fails at startup instead of publishing to a stream no worker pool reads — this generalized gateway is
        what app/domains/support|sales/ run behind (see README.md's "Example domains" section).
        """

        class _StopHere(Exception):
            pass

        class _RaisingAsyncClient:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                raise _StopHere()

            async def __aexit__(self, *exc):
                return False

        resolved_with = {}

        fake_manifest = type("FakeManifest", (), {"name": "support"})()

        def _fake_resolve_domain(name):
            resolved_with["name"] = name
            return (fake_manifest, "fake-domain")

        monkeypatch.setattr(telegram_channel, "TELEGRAM_BOT_TOKEN", "fake-token")
        monkeypatch.setattr(telegram_channel, "AGENT_DOMAIN", "support")
        monkeypatch.setattr(telegram_channel, "resolve_domain", _fake_resolve_domain)
        # run() now loads the persisted offset (get_redis_client) BEFORE
        # opening the httpx client below — without this mock, this test
        # would silently reach whatever real Redis happens to be running
        # on this machine instead of staying hermetic.
        monkeypatch.setattr(telegram_channel, "get_redis_client", lambda: FakeRedis())
        monkeypatch.setattr(telegram_channel.httpx, "AsyncClient", _RaisingAsyncClient)

        with pytest.raises(_StopHere):
            await telegram_channel.run()

        assert resolved_with["name"] == "support"


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


class TestRunPersistsOffsetAcrossPolls:
    async def test_resumes_from_the_previously_persisted_offset(self, monkeypatch):
        """A fresh process must pick up where the last one left off, not
        restart from 0 (Telegram's own update history) — the actual
        regression this whole fix exists for."""

        class _StopHere(BaseException):
            """BaseException, not Exception: raised from INSIDE getUpdates,
            which run()'s own poll loop wraps in a broad
            `except Exception` (a real poll failure must not kill the
            loop) — an ordinary Exception here would just be swallowed and
            retried forever instead of ending this test."""

        redis_client = FakeRedis()
        await telegram_channel._save_offset(redis_client, "ecorp", 500)
        seen_offsets = []

        class _RecordingThenStoppingClient:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def get(self, url, params=None):
                seen_offsets.append(params["offset"])
                raise _StopHere()

        monkeypatch.setattr(telegram_channel, "TELEGRAM_BOT_TOKEN", "fake-token")
        monkeypatch.setattr(telegram_channel, "AGENT_DOMAIN", "ecorp")
        monkeypatch.setattr(
            telegram_channel, "resolve_domain", lambda name: (type("M", (), {"name": name})(), "d")
        )

        monkeypatch.setattr(telegram_channel, "get_redis_client", lambda: redis_client)
        monkeypatch.setattr(telegram_channel.httpx, "AsyncClient", _RecordingThenStoppingClient)

        with pytest.raises(_StopHere):
            await telegram_channel.run()

        assert seen_offsets == [500]

    async def test_persists_the_new_offset_after_each_message_is_handled(self, monkeypatch):
        """`_save_offset` is called from the `for update in updates:` body,
        NOT inside run()'s narrow `except Exception` around getUpdates —
        raising from there (rather than from a second getUpdates call)
        stops the loop right after the one behavior this test cares about,
        without depending on run()'s own retry/sleep mechanics at all."""

        class _StopHere(Exception):
            pass

        redis_client = FakeRedis()
        real_save_offset = telegram_channel._save_offset

        async def _save_offset_then_stop(client, domain, offset):
            await real_save_offset(client, domain, offset)
            raise _StopHere()

        class _OneBatchClient:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def get(self, url, params=None):
                return _FakeResponse({"result": [{"update_id": 777, "message": _message()}]})

            async def post(self, url, json=None):
                return _FakeResponse({"ok": True})

        fake_astream, captured = _fake_stream_turn([{"type": "token", "content": "ok"}])
        monkeypatch.setattr(telegram_channel, "stream_turn", fake_astream)
        monkeypatch.setattr(telegram_channel, "TELEGRAM_BOT_TOKEN", "fake-token")
        monkeypatch.setattr(telegram_channel, "AGENT_DOMAIN", "ecorp")
        monkeypatch.setattr(
            telegram_channel, "resolve_domain", lambda name: (type("M", (), {"name": name})(), "d")
        )

        monkeypatch.setattr(telegram_channel, "get_redis_client", lambda: redis_client)
        monkeypatch.setattr(telegram_channel, "_save_offset", _save_offset_then_stop)
        monkeypatch.setattr(telegram_channel.httpx, "AsyncClient", _OneBatchClient)

        with pytest.raises(_StopHere):
            await telegram_channel.run()

        assert await telegram_channel._load_offset(redis_client, "ecorp") == 778
        # The poll loop is where the update id enters. Dropped here, nothing else notices: every
        # message still gets a reply, and a redelivered one quietly becomes a second turn again.
        assert captured["kwargs"]["dedup_key"] == "telegram:ecorp:update:777"


class TestRedeliveryThroughTheQueue:
    """Telegram redelivers an update the channel never persisted an offset for: after a crash
    between finishing `handle_message` and `_save_offset` (see run()'s docstring).

    When the channel ran the graph itself that crash took the turn down with it, so handling the
    message again was a retry. The turn now runs on a worker and outlives the channel, so handling
    it again used to mean a second turn: a second model call, a second copy of the question in
    the conversation. These keep the real producer and put a fake Redis whose worker answers
    under it, since what the channel promises here is what reaches the queue."""

    @staticmethod
    def _jobs(redis_client):
        stream = queue.requests_stream_key(telegram_channel.AGENT_DOMAIN)
        return [json.loads(fields["payload"]) for _, fields in redis_client.streams.get(stream, [])]

    async def test_a_redelivered_update_is_one_turn_and_is_still_answered(self, monkeypatch):
        redis_client = AnsweringRedis([{"type": "token", "content": "the answer"}, {"type": "done"}])
        monkeypatch.setattr(producer, "get_client", lambda: redis_client)
        client = _FakeAsyncClient()

        await telegram_channel.handle_message(client, _message(text="hi", chat_id=7), update_id=777)
        await telegram_channel.handle_message(client, _message(text="hi", chat_id=7), update_id=777)

        (job,) = self._jobs(redis_client)
        assert job["unattended"] is True  # the worker, not this channel, declines an approval
        assert job["thread_id"] == telegram_channel._thread_id_for_chat(7)
        replies = [body["text"] for url, body in client.posts if "sendMessage" in url]
        assert replies == ["the answer", "the answer"]  # a duplicate reply is the accepted cost

    async def test_two_different_updates_with_the_same_words_are_two_turns(self, monkeypatch):
        """Dedup is by update id, not by text: a person who sends "yes" twice means both."""
        redis_client = AnsweringRedis([{"type": "token", "content": "ok"}, {"type": "done"}])
        monkeypatch.setattr(producer, "get_client", lambda: redis_client)
        client = _FakeAsyncClient()

        await telegram_channel.handle_message(client, _message(text="yes", chat_id=7), update_id=100)
        await telegram_channel.handle_message(client, _message(text="yes", chat_id=7), update_id=101)

        assert len(self._jobs(redis_client)) == 2
