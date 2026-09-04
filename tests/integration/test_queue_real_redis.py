"""A real Redis Stack round trip for app/turns/queue.py — the fake-free
counterpart to tests/turns/test_queue.py, which (correctly, for a fast/
hermetic suite) drives the exact same functions against a hand-rolled
`FakeRedis` covering just the Streams commands this app uses. What that
can't catch — a real redis-py/Redis Stack server version mismatch in
consumer-group semantics (`XGROUP CREATE`/`XREADGROUP`/`XACK`) breaking
silently across a dependency bump — needs an actual server, hence
`@pytest.mark.integration` (see tests/containers.py's own docstring for why
this self-skips without Docker rather than needing `make up`).

Drives the real producer/consumer functions end to end for the "turn" job
kind: `publish_request` (producer) → a real `XREADGROUP` pull, the same
shape app/turns/agent_worker.py's own dispatch loop reads (consumer) →
`publish_result`/`read_results` for the results leg (worker → producer) —
proving the two real Redis Streams primitives this whole queue is built on
actually round-trip against a real server, not just this app's own
in-memory model of them.
"""
import asyncio
import json
import uuid

import pytest

from app.turns import queue
from tests.conftest import TEST_CTX
from tests.containers import ensure_redis

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def real_redis(monkeypatch):
    """Points `queue` at a real, shared Redis container (see
    tests/containers.py) — and, since it's SHARED (across tests in this
    file, and across xdist workers running them concurrently), also gives
    each test its OWN requests-stream/`CONSUMER_GROUP` name rather than the
    fixed production ones. A domain's requests stream is shared by every
    "turn"/"resume"/"cancel" job for that domain (and, here, every test) by
    design in production — `XREADGROUP ... ">"` hands out whichever message
    is next in GROUP delivery order, not "whichever message this specific
    test just published," so two tests racing against the fixed names under
    real parallelism could cross-deliver each other's message. Unique
    per-test names are the same isolation idiom
    tests/agent/test_durable_checkpoint.py already uses via a unique
    thread_id, applied to the two names that stand in for it here.

    Patches `requests_stream_key` itself (not a fixed module constant, now
    that it's domain-parameterized) to ALWAYS return this one unique
    stream regardless of which domain a call passes in — this file only
    ever exercises the default ("acme") domain, so collapsing every domain
    onto the same unique test stream changes nothing it actually asserts."""
    info = ensure_redis()
    monkeypatch.setattr(queue, "REDIS_URL", info["redis_url"])
    monkeypatch.setattr(queue, "_client", None)  # get_client() is a lazy singleton — see its own docstring
    test_stream = f"agent:requests:test:{uuid.uuid4()}"
    monkeypatch.setattr(queue, "requests_stream_key", lambda domain="acme": test_stream)
    monkeypatch.setattr(queue, "CONSUMER_GROUP", f"agent-workers:test:{uuid.uuid4()}")
    yield
    queue._client = None


def test_a_turn_request_round_trips_from_producer_through_a_real_consumer_group():
    async def _run():
        client = queue.get_client()
        await queue.ensure_consumer_group(client)

        request_id = str(uuid.uuid4())
        thread_id = str(uuid.uuid4())
        await queue.publish_request(
            client,
            request_id=request_id,
            text="what is 21 * 2?",
            thread_id=thread_id,
            ctx=TEST_CTX,
        )

        # Consumer side: the same XREADGROUP shape app/turns/agent_worker.py's
        # own dispatch loop reads (see that module's `run()`).
        response = await client.xreadgroup(
            queue.CONSUMER_GROUP, "test-consumer", {queue.requests_stream_key(): ">"}, count=1
        )
        [(_, entries)] = response
        [(entry_id, fields)] = entries
        payload = json.loads(fields["payload"])
        assert payload == {
            "kind": "turn",
            "request_id": request_id,
            "text": "what is 21 * 2?",
            "thread_id": thread_id,
            "ctx": TEST_CTX,
            "require_approval": False,
            "images": [],
        }
        await client.xack(queue.requests_stream_key(), queue.CONSUMER_GROUP, entry_id)

        # Results leg: the worker publishes events, the producer drains them
        # via read_results until a terminal one.
        await queue.publish_result(client, request_id, {"type": "token", "content": "42"})
        await queue.publish_result(client, request_id, {"type": "done"})

        events = [event async for event in queue.read_results(client, request_id)]
        return events

    events = asyncio.run(_run())

    assert events == [{"type": "token", "content": "42"}, {"type": "done"}]


def test_a_second_worker_in_the_same_group_never_gets_a_message_already_delivered():
    """The whole reason this queue uses a Redis Streams CONSUMER GROUP (not
    a plain stream every worker reads independently) — "Redis's own group
    semantics guarantee each request is delivered to exactly one worker, so
    running N worker processes is the entire scaling story" per this
    module's own docstring. Worth a real-server assertion, not just trusting
    the docs: a second consumer in the SAME group must see nothing new."""

    async def _run():
        client = queue.get_client()
        await queue.ensure_consumer_group(client)
        await queue.publish_request(
            client, request_id="req-2", text="hi", thread_id="thread-2", ctx=TEST_CTX
        )

        first = await client.xreadgroup(
            queue.CONSUMER_GROUP, "worker-a", {queue.requests_stream_key(): ">"}, count=1
        )
        second = await client.xreadgroup(
            queue.CONSUMER_GROUP, "worker-b", {queue.requests_stream_key(): ">"}, count=1, block=100
        )
        return first, second

    first, second = asyncio.run(_run())

    assert first and first[0][1]  # worker-a got the one message
    assert second == [] or second[0][1] == []  # worker-b got nothing new
