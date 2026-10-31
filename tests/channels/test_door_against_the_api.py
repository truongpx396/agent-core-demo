"""The door client against the REAL chat API (app/api/main.py) in process, with a fake Redis and a "worker" that answers the moment a job lands.

test_door.py replaces the network; this replaces only Redis and the worker. What it adds is that the request the client builds is the one the
real routes accept (their validation, their ownership check, their header-mode caller), and that the SSE the real route streams is what the client
parses: the two halves were written from reading each other, and nothing else would notice if they drifted.
"""
import json

import httpx
import pytest

from app.agent.runtime import sessions
from app.api.main import app
from app.channels.door import Door, HeaderAuth
from app.core.config import settings
from app.job_queue import queue
from tests.job_queue.test_queue import FakeRedis


class AnsweringRedis(FakeRedis):
    """A Redis whose worker answers every job on a requests stream with `events`."""

    def __init__(self, events):
        super().__init__()
        self.events = events

    async def xadd(self, stream, fields, maxlen=None, approximate=True):
        entry_id = await super().xadd(stream, fields, maxlen, approximate)
        if stream.startswith("agent:requests:"):
            request_id = json.loads(fields["payload"])["request_id"]
            for event in self.events:
                await queue.publish_result(self, request_id, event)
        return entry_id

    def jobs(self, domain="ecorp"):
        return [json.loads(f["payload"]) for _, f in self.streams.get(queue.requests_stream_key(domain), [])]


class _Sessions:
    """`chat_sessions` in memory with the real semantics: the first claimant of an id owns it, ownership is (tenant, principal, domain)."""

    def __init__(self):
        self.rows: dict[str, tuple] = {}

    async def claim(self, ctx, thread_id, title=None, domain="ecorp"):
        return self.rows.setdefault(thread_id, (ctx["tenant"], ctx["principal"], domain)) == (ctx["tenant"], ctx["principal"], domain)

    async def belongs(self, ctx, thread_id, domain="ecorp"):
        return self.rows.get(thread_id) == (ctx["tenant"], ctx["principal"], domain)


@pytest.fixture
def world(monkeypatch):
    redis = AnsweringRedis([{"type": "token", "content": "the answer"}, {"type": "done"}])
    owned = _Sessions()
    monkeypatch.setattr(queue, "get_client", lambda: redis)
    monkeypatch.setattr(sessions, "claim_session", owned.claim)
    monkeypatch.setattr(sessions, "session_belongs_to", owned.belongs)
    monkeypatch.setattr(settings, "auth_mode", "headers")
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://api.test")

    def door(principal, *, tenant="ecorp", domain="ecorp"):
        return Door(HeaderAuth(tenant, principal), channel="telegram", domain=domain, http=http, base_url="http://api.test")

    return type("World", (), {"redis": redis, "door": staticmethod(door), "sessions": owned, "http": http})


async def collect(stream):
    return [event async for event in stream]


async def test_a_turn_goes_in_and_the_workers_events_come_back_through_the_real_routes(world):
    events = await collect(world.door("p-1").stream_turn("hello", "t1", unattended=True))
    await world.http.aclose()

    assert [e["type"] for e in events] == ["token", "done"]
    (job,) = world.redis.jobs()
    assert (job["text"], job["thread_id"], job["unattended"], job["require_approval"]) == ("hello", "t1", True, False)
    assert job["ctx"]["principal"] == "p-1", "the caller is the one the door named, not anything in the body"


async def test_a_redelivered_update_is_one_job_and_the_first_attempts_events_come_back_again(world):
    door = world.door("p-1")

    first = await collect(door.stream_turn("hi", "t1", unattended=True, replay_key="telegram:ecorp:update:7"))
    again = await collect(door.stream_turn("hi", "t1", unattended=True, replay_key="telegram:ecorp:update:7"))
    await world.http.aclose()

    assert len(world.redis.jobs()) == 1
    assert first == again and [e["type"] for e in again] == ["token", "done"]


async def test_the_telegram_channel_can_claim_its_own_conversation_and_nobody_can_claim_it_for_them(world):
    mine = await collect(world.door("abc-123").stream_turn("hi", "telegram:abc-123:555", unattended=True))
    squatter = await collect(world.door("someone-else").stream_turn("hi", "telegram:abc-123:777", unattended=True))
    await world.http.aclose()

    assert [e["type"] for e in mine] == ["token", "done"]
    assert squatter == [{"type": "error", "content": "That conversation was not found."}]
    assert "telegram:abc-123:777" not in world.sessions.rows
    assert [j["thread_id"] for j in world.redis.jobs()] == ["telegram:abc-123:555"]


async def test_someone_elses_conversation_is_the_same_404_an_unknown_one_gets(world):
    await collect(world.door("owner").stream_turn("private", "t1"))

    events = await collect(world.door("intruder").stream_turn("hi", "t1"))
    await world.http.aclose()

    assert events == [{"type": "error", "content": "That conversation was not found."}]
    assert len(world.redis.jobs()) == 1


async def test_a_resume_and_a_cancel_reach_the_routes_for_the_owner(world):
    door = world.door("p-1")
    await collect(door.stream_turn("hi", "t1"))

    await collect(door.stream_resume("t1", True))
    await door.cancel_turn("t1")
    await world.http.aclose()

    assert await queue.is_cancelled(world.redis, "t1") is True  # the API set the flag before it began to stream
    kinds = [json.loads(f["payload"])["kind"] for _, f in world.redis.streams[queue.requests_stream_key("ecorp")]]
    assert kinds == ["turn", "resume", "cancel"]


async def test_a_cancel_for_someone_elses_conversation_is_refused_and_sets_no_flag(world):
    await collect(world.door("owner").stream_turn("hi", "t1"))

    with pytest.raises(Exception, match="not found"):
        await world.door("intruder").cancel_turn("t1")
    await world.http.aclose()

    assert await queue.is_cancelled(world.redis, "t1") is False


async def test_the_domain_header_picks_the_pool_the_job_lands_on(world):
    await collect(world.door("p-1", domain="support").stream_turn("hi", "t1"))
    await world.http.aclose()

    assert len(world.redis.jobs("support")) == 1 and world.redis.jobs("ecorp") == []
