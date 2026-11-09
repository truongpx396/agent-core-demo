"""The door client (app/adapters/inbound/channels/door.py): how the CLI and the Telegram channel reach the chat API (specs/020).

A real `httpx.AsyncClient` over a `MockTransport` stands in for the front door and the API, so these exercise the actual request, the actual SSE
parsing and the actual status handling; only the network is replaced. What matters here is what a person sees and what is NEVER done: a refusal
becomes one `error` event with words, nothing falls back to another route, and a flag that could only loosen a turn does not exist.
"""
import json

import httpx
import pytest

from app.adapters.inbound.channels.door import (
    SIGNIN_MESSAGES,
    Door,
    DoorRefusedError,
    HeaderAuth,
)
from app.adapters.inbound.channels.oauth import SignInRefusedError
from app.core import metrics
from tests.conftest import metric_value as _count


def sse(*events: dict) -> bytes:
    return b"".join(b"data: " + json.dumps(e).encode() + b"\n\n" for e in events)


class _Auth:
    def __init__(self, token="t1", *, refreshed=True, fail=None):
        self.token = token
        self.refreshed = refreshed
        self.fail = fail
        self.reauths = 0

    async def headers(self):
        if self.fail:
            raise self.fail
        return {"Authorization": f"Bearer {self.token}"}

    async def reauthenticate(self):
        self.reauths += 1
        if self.refreshed:
            self.token = "t2"
        return self.refreshed


def make_door(handler, auth=None, *, channel="cli", domain="ecorp"):
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return Door(auth or _Auth(), channel=channel, domain=domain, http=http, base_url="https://door.test"), http


async def collect(stream):
    return [event async for event in stream]


class TestATurn:
    async def test_the_request_names_the_conversation_the_domain_and_the_caller(self):
        seen = {}

        def handler(request):
            seen["url"], seen["headers"], seen["body"] = str(request.url), request.headers, json.loads(request.content)
            return httpx.Response(200, content=sse({"type": "done"}))

        door, http = make_door(handler, domain="support")
        await collect(door.stream_turn("hello", "thread-1"))
        await http.aclose()

        assert seen["url"] == "https://door.test/chat/stream/queued"
        assert seen["body"] == {"message": "hello", "thread_id": "thread-1"}  # nothing the caller did not ask for
        assert seen["headers"]["authorization"] == "Bearer t1"
        assert seen["headers"]["x-domain"] == "support"

    async def test_the_events_come_back_in_order_and_junk_between_them_is_skipped(self):
        body = b": a comment\n\n" + sse({"type": "token", "content": "a"}) + b"data: {not json\n\n" + sse({"type": "done"})
        door, http = make_door(lambda request: httpx.Response(200, content=body))

        events = await collect(door.stream_turn("hi", "t1"))
        await http.aclose()

        assert [e["type"] for e in events] == ["token", "done"]

    async def test_unattended_approval_and_replay_key_are_sent_only_when_asked_for(self):
        bodies = []

        def handler(request):
            bodies.append(json.loads(request.content))
            return httpx.Response(200, content=sse({"type": "done"}))

        door, http = make_door(handler)
        await collect(door.stream_turn("a", "t1"))
        await collect(door.stream_turn("b", "t1", unattended=True, require_approval=True, replay_key="telegram:ecorp:update:9"))
        await http.aclose()

        assert bodies[0] == {"message": "a", "thread_id": "t1"}
        assert bodies[1] == {
            "message": "b",
            "thread_id": "t1",
            "unattended": True,
            "require_approval": True,
            "replay_key": "telegram:ecorp:update:9",
        }

    async def test_header_mode_names_the_caller_in_headers_and_sends_no_token(self):
        seen = {}

        def handler(request):
            seen.update(request.headers)
            return httpx.Response(200, content=sse({"type": "done"}))

        door, http = make_door(handler, HeaderAuth("ecorp", "local:me"))
        await collect(door.stream_turn("hi", "t1"))
        await http.aclose()

        assert (seen["x-tenant-id"], seen["x-principal-id"]) == ("ecorp", "local:me")
        assert "authorization" not in seen


class TestResumeAndCancel:
    async def test_a_resume_is_the_answer_to_a_pause_on_the_same_conversation(self):
        seen = {}

        def handler(request):
            seen["path"], seen["body"] = request.url.path, json.loads(request.content)
            return httpx.Response(200, content=sse({"type": "done"}))

        door, http = make_door(handler)
        await collect(door.stream_resume("t1", approved=True))
        await http.aclose()

        assert (seen["path"], seen["body"]) == ("/chat/resume", {"thread_id": "t1", "approved": True})

    async def test_a_cancel_returns_once_the_api_has_accepted_it_without_waiting_for_a_stream(self):
        """The API sets the cancel flag and publishes the cancel job BEFORE it streams; the stream is only the paused-run job's outcome. A stream
        that never ends must not hold a person who pressed Ctrl-C."""

        class _Endless(httpx.AsyncByteStream):
            async def __aiter__(self):
                while True:
                    yield b": waiting\n\n"
                    import asyncio

                    await asyncio.sleep(3600)

        door, http = make_door(lambda request: httpx.Response(200, stream=_Endless()))

        await door.cancel_turn("t1")  # returns; a hang here is the failure
        await http.aclose()

    async def test_a_cancel_the_door_refuses_is_reported_to_the_caller(self):
        door, http = make_door(lambda request: httpx.Response(404))

        with pytest.raises(DoorRefusedError, match="not found"):
            await door.cancel_turn("t1")
        await http.aclose()


class TestAnExpiredSignIn:
    async def test_a_401_gets_one_fresh_sign_in_and_one_repeat(self):
        auths = []

        def handler(request):
            auths.append(request.headers["authorization"])
            if request.headers["authorization"] == "Bearer t1":
                return httpx.Response(401)
            return httpx.Response(200, content=sse({"type": "token", "content": "ok"}))

        auth = _Auth()
        door, http = make_door(handler, auth)
        events = await collect(door.stream_turn("hi", "t1"))
        await http.aclose()

        assert auths == ["Bearer t1", "Bearer t2"]
        assert auth.reauths == 1
        assert events == [{"type": "token", "content": "ok"}]

    async def test_a_401_after_the_fresh_sign_in_is_not_retried_again(self):
        calls = []
        door, http = make_door(lambda request: calls.append(1) or httpx.Response(401))

        events = await collect(door.stream_turn("hi", "t1"))
        await http.aclose()

        assert len(calls) == 2
        assert events == [{"type": "error", "content": SIGNIN_MESSAGES["credentials"]}]

    async def test_a_sign_in_that_cannot_be_renewed_is_not_repeated(self):
        calls = []
        door, http = make_door(lambda request: calls.append(1) or httpx.Response(401), _Auth(refreshed=False))

        events = await collect(door.stream_turn("hi", "t1"))
        await http.aclose()

        assert len(calls) == 1
        assert events[0]["type"] == "error"


class TestWhatTheDoorRefuses:
    @pytest.mark.parametrize(
        ("status", "headers", "words", "reason"),
        [
            (429, {"retry-after": "12"}, "Try again in 12 seconds", "rate_limited"),
            (429, {"retry-after": "1"}, "Try again in 1 second.", "rate_limited"),
            (429, {}, "Try again in a moment", "rate_limited"),
            (403, {}, "not allowed", "forbidden"),
            (404, {}, "not found", "not_found"),
            (503, {}, "can't be checked right now and nothing was sent", "unavailable"),
            (502, {}, "nothing was sent", "unavailable"),
            (422, {}, "invalid", "invalid"),
            (418, {}, "(418)", "other"),
        ],
    )
    async def test_the_person_is_told_in_words_and_it_is_counted(self, status, headers, words, reason):
        door, http = make_door(lambda request: httpx.Response(status, headers=headers), channel="telegram")
        before = _count(metrics.channel_door_refusal_total, channel="telegram", reason=reason)

        events = await collect(door.stream_turn("hi", "t1"))
        await http.aclose()

        (event,) = events
        assert event["type"] == "error" and words in event["content"]
        assert _count(metrics.channel_door_refusal_total, channel="telegram", reason=reason) == before + 1

    async def test_a_refusal_never_echoes_the_body_it_came_with(self):
        """A door's error body can carry details nobody should paste into a chat window."""
        door, http = make_door(lambda request: httpx.Response(403, json={"message": "secret-detail-xyz"}))

        (event,) = await collect(door.stream_turn("hi", "t1"))
        await http.aclose()

        assert "secret-detail-xyz" not in event["content"]


class TestTheDoorCannotBeReached:
    async def test_nothing_is_sent_anywhere_else_and_the_person_is_told(self):
        """The old direct-to-queue path is gone: when the door does not answer there is no other route, and saying so is the whole behaviour."""

        def handler(request):
            raise httpx.ConnectError("refused")

        door, http = make_door(handler)
        before = _count(metrics.channel_door_refusal_total, channel="cli", reason="unreachable")

        (event,) = await collect(door.stream_turn("hi", "t1"))
        await http.aclose()

        assert event["type"] == "error" and "nothing was sent" in event["content"]
        assert _count(metrics.channel_door_refusal_total, channel="cli", reason="unreachable") == before + 1

    async def test_a_sign_in_that_is_refused_stops_the_request_before_it_is_made(self):
        calls = []
        door, http = make_door(
            lambda request: calls.append(1) or httpx.Response(200), _Auth(fail=SignInRefusedError("forbidden")), channel="telegram"
        )

        (event,) = await collect(door.stream_turn("hi", "t1"))
        await http.aclose()

        assert calls == []
        assert event == {"type": "error", "content": SIGNIN_MESSAGES["forbidden"]}
