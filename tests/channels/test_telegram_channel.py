"""Tests for app/channels/telegram.py — a fake async httpx client stands in
for a live Telegram API (see tests/agent/test_model_resolver.py's _FakeResponse
for the sync-httpx equivalent), and app.channels.telegram.answer is
monkeypatched so these never touch a real graph/LLM, matching the rest of
the suite's hermetic discipline.

No pytest-asyncio plugin is installed in this project (see
tests/agent/test_durable_checkpoint.py) — async behavior here is driven the same
established way: a plain sync `def test_...` wrapping an inner `async def`
closure via `asyncio.run(...)`.
"""
import asyncio

import pytest

from app.channels import telegram as telegram_channel
from app.core.errors import ErrorCode, ErrorEnvelope


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
    def test_short_message_sent_as_a_single_post(self):
        client = _FakeAsyncClient()
        asyncio.run(telegram_channel._send_message(client, 1, "short reply"))
        assert len(client.posts) == 1
        assert client.posts[0][1]["text"] == "short reply"
        assert client.posts[0][1]["chat_id"] == 1

    def test_long_message_is_split_across_multiple_sends(self):
        client = _FakeAsyncClient()
        long_text = "x" * (telegram_channel._MESSAGE_CHAR_LIMIT + 500)
        asyncio.run(telegram_channel._send_message(client, 1, long_text))
        assert len(client.posts) == 2
        assert sum(len(p[1]["text"]) for p in client.posts) == len(long_text)

    def test_a_failed_send_is_swallowed_not_raised(self):
        class _RaisingClient(_FakeAsyncClient):
            async def post(self, url, json=None):
                raise RuntimeError("network down")

        # Must not raise — a bad chat_id can't be allowed to kill the poll loop.
        asyncio.run(telegram_channel._send_message(_RaisingClient(), 1, "hi"))


class TestHandleMessage:
    def test_calls_answer_with_the_scoped_thread_and_ctx_and_replies(self, monkeypatch):
        captured = {}

        def fake_answer(text, thread_id, ctx):
            captured["args"] = (text, thread_id, ctx)
            return ("the answer", [], None, 0)

        monkeypatch.setattr(telegram_channel, "answer", fake_answer)
        client = _FakeAsyncClient()

        asyncio.run(
            telegram_channel.handle_message(client, _message(text="hi", chat_id=7, user_id=42))
        )

        text, thread_id, ctx = captured["args"]
        assert text == "hi"
        assert thread_id == telegram_channel._thread_id_for_chat(7)
        assert ctx["principal"] == "telegram:42"
        # sendChatAction + sendMessage
        assert any("sendMessage" in url and body["text"] == "the answer" for url, body in client.posts)

    def test_reply_includes_citations_footer(self, monkeypatch):
        cited = [{"marker": "[1]", "title": "Refund Policy", "doc_id": "d1"}]
        monkeypatch.setattr(
            telegram_channel, "answer", lambda text, thread_id, ctx: ("grounded answer [1]", cited, None, 0)
        )
        client = _FakeAsyncClient()

        asyncio.run(telegram_channel.handle_message(client, _message()))

        sent = next(body["text"] for url, body in client.posts if "sendMessage" in url)
        assert "Sources:" in sent

    def test_non_text_message_is_skipped_without_calling_answer(self, monkeypatch):
        called = []
        monkeypatch.setattr(telegram_channel, "answer", lambda *a, **kw: called.append(1))
        client = _FakeAsyncClient()

        asyncio.run(
            telegram_channel.handle_message(client, {"chat": {"id": 1}, "from": {"id": 1}})
        )

        assert called == []
        assert client.posts == []

    def test_an_error_envelope_still_produces_a_reply_not_a_crash(self, monkeypatch):
        """answer() already turns a timeout/checkpoint issue into a real
        message text (see its docstring) — this just proves the channel
        doesn't need any special-casing for that; it just forwards it."""
        envelope = ErrorEnvelope(code=ErrorCode.TIMEOUT, message="Sorry, that took too long.")
        monkeypatch.setattr(
            telegram_channel, "answer", lambda *a, **kw: (envelope.message, [], envelope, 0)
        )
        client = _FakeAsyncClient()

        asyncio.run(telegram_channel.handle_message(client, _message()))

        sent = next(body["text"] for url, body in client.posts if "sendMessage" in url)
        assert sent == "Sorry, that took too long."


class TestRun:
    def test_refuses_to_start_without_a_bot_token(self, monkeypatch):
        monkeypatch.setattr(telegram_channel, "TELEGRAM_BOT_TOKEN", "")
        with pytest.raises(RuntimeError, match="TELEGRAM_BOT_TOKEN"):
            asyncio.run(telegram_channel.run())

    def test_resolves_agent_domain_and_primes_the_singleton_before_polling(self, monkeypatch):
        """AGENT_DOMAIN (app/core/config.py) must be resolved and passed
        into init_graph_sync BEFORE the poll loop starts — this generalized
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

        def _fake_init_graph_sync(manifest=None, domain=None):
            primed_with["manifest"] = manifest
            primed_with["domain"] = domain

        monkeypatch.setattr(telegram_channel, "TELEGRAM_BOT_TOKEN", "fake-token")
        monkeypatch.setattr(telegram_channel, "AGENT_DOMAIN", "support")
        monkeypatch.setattr(telegram_channel, "resolve_domain", _fake_resolve_domain)
        monkeypatch.setattr(telegram_channel, "init_graph_sync", _fake_init_graph_sync)
        monkeypatch.setattr(telegram_channel.httpx, "AsyncClient", _RaisingAsyncClient)

        with pytest.raises(_StopHere):
            asyncio.run(telegram_channel.run())

        assert resolved_with["name"] == "support"
        assert primed_with == {"manifest": fake_manifest, "domain": "fake-domain"}
