"""Tests for app/channels/telegram.py — a fake async httpx client stands in
for a live Telegram API (see tests/agent/test_model_resolver.py's _FakeResponse
for the sync-httpx equivalent), and
app.channels.telegram.astream_events_turn_unattended is monkeypatched (as a
fake async generator) so these never touch a real graph/LLM, matching the
rest of the suite's hermetic discipline.

No pytest-asyncio plugin is installed in this project (see
tests/agent/test_durable_checkpoint.py) — async behavior here is driven the same
established way: a plain sync `def test_...` wrapping an inner `async def`
closure via `asyncio.run(...)`.
"""

import pytest

from app.channels import telegram as telegram_channel
from app.core.errors import ErrorCode, ErrorEnvelope
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


def _fake_astream(events):
    """Builds a fake `astream_events_turn_unattended` replacement that
    yields `events` and records the (text, thread_id, ctx) it was called
    with onto `captured["args"]`."""
    captured = {}

    async def _fake(text, thread_id, ctx):
        captured["args"] = (text, thread_id, ctx)
        for event in events:
            yield event

    return _fake, captured


class TestHandleMessage:
    async def test_calls_astream_events_turn_unattended_with_the_scoped_thread_and_ctx_and_replies(
        self, monkeypatch
    ):
        fake, captured = _fake_astream([{"type": "token", "content": "the answer"}])
        monkeypatch.setattr(telegram_channel, "astream_events_turn_unattended", fake)
        client = _FakeAsyncClient()

        await telegram_channel.handle_message(client, _message(text="hi", chat_id=7, user_id=42))

        text, thread_id, ctx = captured["args"]
        assert text == "hi"
        assert thread_id == telegram_channel._thread_id_for_chat(7)
        assert ctx["principal"] == "telegram:42"
        # sendChatAction + sendMessage
        assert any("sendMessage" in url and body["text"] == "the answer" for url, body in client.posts)

    async def test_reply_includes_citations_footer(self, monkeypatch):
        cited = [{"marker": "[1]", "title": "Refund Policy", "doc_id": "d1"}]
        fake, _ = _fake_astream(
            [
                {"type": "token", "content": "grounded answer [1]"},
                {"type": "citations", "items": cited},
            ]
        )
        monkeypatch.setattr(telegram_channel, "astream_events_turn_unattended", fake)
        client = _FakeAsyncClient()

        await telegram_channel.handle_message(client, _message())

        sent = next(body["text"] for url, body in client.posts if "sendMessage" in url)
        assert "Sources:" in sent

    async def test_non_text_message_is_skipped_without_calling_astream_events_turn_unattended(self, monkeypatch):
        called = []

        def fake(*a, **kw):
            called.append(1)
            raise AssertionError("should not be called")

        monkeypatch.setattr(telegram_channel, "astream_events_turn_unattended", fake)
        client = _FakeAsyncClient()

        await telegram_channel.handle_message(client, {"chat": {"id": 1}, "from": {"id": 1}})

        assert called == []
        assert client.posts == []

    async def test_an_error_envelope_still_produces_a_reply_not_a_crash(self, monkeypatch):
        """astream_events_turn_unattended's `error` event already carries a
        real message text (see _run_graph_stream's docstring) — this just
        proves the channel doesn't need any special-casing for that; it
        just forwards it."""
        envelope = ErrorEnvelope(code=ErrorCode.TIMEOUT, message="Sorry, that took too long.")
        fake, _ = _fake_astream([{"type": "error", "content": envelope.message}])
        monkeypatch.setattr(telegram_channel, "astream_events_turn_unattended", fake)
        client = _FakeAsyncClient()

        await telegram_channel.handle_message(client, _message())

        sent = next(body["text"] for url, body in client.posts if "sendMessage" in url)
        assert sent == "Sorry, that took too long."


class TestRun:
    async def test_refuses_to_start_without_a_bot_token(self, monkeypatch):
        monkeypatch.setattr(telegram_channel, "TELEGRAM_BOT_TOKEN", "")
        with pytest.raises(RuntimeError, match="TELEGRAM_BOT_TOKEN"):
            await telegram_channel.run()

    async def test_resolves_agent_domain_and_primes_the_singleton_before_polling(self, monkeypatch):
        """AGENT_DOMAIN (app/core/config.py) must be resolved and passed
        into init_graph_async BEFORE the poll loop starts — this generalized
        gateway is what app/domains/support|sales/ run behind (see
        README.md's "Example domains" section). Stops the run() coroutine
        right after that point (a fake httpx.AsyncClient whose __aenter__
        raises a marker exception) rather than actually driving the
        long-poll loop, which is out of scope for this test."""

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

        primed_with = {}

        async def _fake_init_graph_async(manifest=None, domain=None):
            primed_with["manifest"] = manifest
            primed_with["domain"] = domain

        monkeypatch.setattr(telegram_channel, "TELEGRAM_BOT_TOKEN", "fake-token")
        monkeypatch.setattr(telegram_channel, "AGENT_DOMAIN", "support")
        monkeypatch.setattr(telegram_channel, "resolve_domain", _fake_resolve_domain)
        monkeypatch.setattr(telegram_channel, "init_graph_async", _fake_init_graph_async)
        # run() now loads the persisted offset (get_redis_client) BEFORE
        # opening the httpx client below — without this mock, this test
        # would silently reach whatever real Redis happens to be running
        # on this machine instead of staying hermetic.
        monkeypatch.setattr(telegram_channel, "get_redis_client", lambda: FakeRedis())
        monkeypatch.setattr(telegram_channel.httpx, "AsyncClient", _RaisingAsyncClient)

        with pytest.raises(_StopHere):
            await telegram_channel.run()

        assert resolved_with["name"] == "support"
        assert primed_with == {"manifest": fake_manifest, "domain": "fake-domain"}


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

        async def _fake_init_graph_async(manifest=None, domain=None):
            return None

        monkeypatch.setattr(telegram_channel, "init_graph_async", _fake_init_graph_async)
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

        fake_astream, _ = _fake_astream([{"type": "token", "content": "ok"}])
        monkeypatch.setattr(telegram_channel, "astream_events_turn_unattended", fake_astream)
        monkeypatch.setattr(telegram_channel, "TELEGRAM_BOT_TOKEN", "fake-token")
        monkeypatch.setattr(telegram_channel, "AGENT_DOMAIN", "ecorp")
        monkeypatch.setattr(
            telegram_channel, "resolve_domain", lambda name: (type("M", (), {"name": name})(), "d")
        )

        async def _fake_init_graph_async(manifest=None, domain=None):
            return None

        monkeypatch.setattr(telegram_channel, "init_graph_async", _fake_init_graph_async)
        monkeypatch.setattr(telegram_channel, "get_redis_client", lambda: redis_client)
        monkeypatch.setattr(telegram_channel, "_save_offset", _save_offset_then_stop)
        monkeypatch.setattr(telegram_channel.httpx, "AsyncClient", _OneBatchClient)

        with pytest.raises(_StopHere):
            await telegram_channel.run()

        assert await telegram_channel._load_offset(redis_client, "ecorp") == 778
