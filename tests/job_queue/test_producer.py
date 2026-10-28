"""Tests for app/job_queue/producer.py — how the CLI and the Telegram channel run a turn through
the queue instead of in their own process.

`FakeRedis` never blocks (its `xread` returns at once), so a test cannot start a worker task and
wait for it. `AnsweringRedis` stands in for the worker instead: the moment a job lands on a
requests stream it writes the scripted events to that job's results stream, which is what a worker
that picked it up immediately would do.
"""
import json

import pytest

from app.job_queue import producer, queue
from tests.job_queue.test_queue import FakeRedis

CTX = {"tenant": "ecorp", "principal": "telegram:42", "claims": {}}


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


def _jobs(client, domain="ecorp"):
    return [json.loads(f["payload"]) for _, f in client.streams.get(queue.requests_stream_key(domain), [])]


async def _collect(stream):
    return [event async for event in stream]


@pytest.fixture
def answers(monkeypatch):
    """Installs an `AnsweringRedis` as the producer's client and returns it."""

    def install(events):
        client = AnsweringRedis(events)
        monkeypatch.setattr(producer, "get_client", lambda: client)
        return client

    return install


class TestStreamTurn:
    async def test_the_turn_lands_on_its_domains_stream_and_the_workers_events_come_back(self, answers):
        client = answers([{"type": "token", "content": "hi"}, {"type": "done"}])

        events = await _collect(
            producer.stream_turn("hello", "telegram:7", CTX, domain="support", unattended=True)
        )

        assert events == [{"type": "token", "content": "hi"}, {"type": "done"}]
        assert queue.requests_stream_key("ecorp") not in client.streams
        (job,) = _jobs(client, "support")
        assert job["kind"] == "turn"
        assert (job["text"], job["thread_id"], job["ctx"]) == ("hello", "telegram:7", CTX)
        assert job["unattended"] is True

    async def test_a_turn_is_attended_unless_the_caller_says_otherwise(self, answers):
        client = answers([{"type": "done"}])

        await _collect(producer.stream_turn("hello", "t1", CTX, domain="ecorp", require_approval=True))

        (job,) = _jobs(client)
        assert job["unattended"] is False
        assert job["require_approval"] is True

    async def test_an_approval_pause_ends_the_stream_so_the_caller_can_answer_it(self, answers):
        answers(
            [
                {"type": "token", "content": "Saving that."},
                {"type": "approval_required", "tool_calls": [{"name": "add_note", "args": {}}]},
            ]
        )

        events = await _collect(producer.stream_turn("save a note", "t1", CTX, domain="ecorp"))

        assert events[-1]["type"] == "approval_required"

    async def test_no_worker_for_the_domain_ends_in_one_error_naming_the_problem(self, monkeypatch):
        """The wait for the first event is bounded (`read_results`), so a front door whose domain
        has no running pool reports it instead of hanging."""
        client = FakeRedis()  # nobody answers
        monkeypatch.setattr(producer, "get_client", lambda: client)
        monkeypatch.setattr(producer, "CHAT_FIRST_RESPONSE_DEADLINE_SECONDS", 0.05)

        events = await _collect(producer.stream_turn("hello", "t1", CTX, domain="ecorp"))

        assert [e["type"] for e in events] == ["error"]
        assert "agent-worker" in events[0]["content"]


class TestStreamTurnDedup:
    """A redelivered update must read the first attempt's results, not run the turn again.

    Before the queue a crashed channel took its turn down with it, so replaying the message was a
    retry. Now the turn outlives the channel, and a replay would be a second model call and a
    second copy of the question in the conversation."""

    async def test_the_same_key_on_the_same_thread_publishes_one_job_and_replays_its_events(self, answers):
        client = answers([{"type": "token", "content": "the answer"}, {"type": "done"}])

        first = await _collect(producer.stream_turn("hi", "telegram:7", CTX, domain="ecorp", dedup_key="u1"))
        second = await _collect(producer.stream_turn("hi", "telegram:7", CTX, domain="ecorp", dedup_key="u1"))

        assert len(_jobs(client)) == 1
        assert first == second == [{"type": "token", "content": "the answer"}, {"type": "done"}]

    @pytest.mark.parametrize(
        ("second_thread", "second_key"),
        [("telegram:7", "u2"), ("telegram:8", "u1")],
        ids=["another update", "another chat"],
    )
    async def test_a_different_update_or_chat_is_a_new_turn(self, answers, second_thread, second_key):
        client = answers([{"type": "done"}])

        await _collect(producer.stream_turn("hi", "telegram:7", CTX, domain="ecorp", dedup_key="u1"))
        await _collect(producer.stream_turn("hi", second_thread, CTX, domain="ecorp", dedup_key=second_key))

        assert len(_jobs(client)) == 2

    async def test_without_a_key_every_call_is_a_new_turn(self, answers):
        client = answers([{"type": "done"}])

        await _collect(producer.stream_turn("hi", "t1", CTX, domain="ecorp"))
        await _collect(producer.stream_turn("hi", "t1", CTX, domain="ecorp"))

        assert len(_jobs(client)) == 2

    async def test_the_claim_outlasts_no_less_than_the_results_stream_it_points_at(self, answers):
        """A claim that expired first would let a replay publish a second turn while the first
        attempt's reply was still sitting unread in its results stream."""
        client = answers([{"type": "done"}])

        await _collect(producer.stream_turn("hi", "t1", CTX, domain="ecorp", dedup_key="u1"))

        (claim,) = [k for k in client.kv if k.startswith("chat:submit_dedup:")]
        assert client.expiries[claim] >= queue.RESULTS_STREAM_TTL_SECONDS

    async def test_a_failed_publish_gives_the_claim_back_so_a_retry_can_publish(self, answers, monkeypatch):
        client = answers([{"type": "done"}])
        real_publish = producer.publish_request

        async def failing_publish(*args, **kwargs):
            raise ConnectionError("redis went away")

        monkeypatch.setattr(producer, "publish_request", failing_publish)
        with pytest.raises(ConnectionError):
            await _collect(producer.stream_turn("hi", "t1", CTX, domain="ecorp", dedup_key="u1"))
        assert not [k for k in client.kv if k.startswith("chat:submit_dedup:")]

        monkeypatch.setattr(producer, "publish_request", real_publish)
        events = await _collect(producer.stream_turn("hi", "t1", CTX, domain="ecorp", dedup_key="u1"))

        assert events == [{"type": "done"}]
        assert len(_jobs(client)) == 1


class TestStreamResume:
    async def test_the_answer_is_a_resume_job_on_the_same_domains_stream(self, answers):
        client = answers([{"type": "token", "content": "Done."}, {"type": "done"}])

        events = await _collect(producer.stream_resume("t1", True, CTX, domain="support"))

        assert [e["type"] for e in events] == ["token", "done"]
        (job,) = _jobs(client, "support")
        assert job["kind"] == "resume"
        assert (job["thread_id"], job["approved"], job["ctx"]) == ("t1", True, CTX)


class TestCancelTurn:
    async def test_it_stops_a_streaming_turn_and_a_paused_one_without_reading_anything_back(self, monkeypatch):
        client = FakeRedis()
        monkeypatch.setattr(producer, "get_client", lambda: client)

        await producer.cancel_turn("t1", CTX, domain="support")

        # What POST /chat/cancel does: the flag reaches a turn that is streaming, the job one that
        # is paused at an approval. Each is a no-op where it does not apply.
        assert await queue.is_cancelled(client, "t1")
        (job,) = _jobs(client, "support")
        assert job["kind"] == "cancel"
        assert (job["thread_id"], job["ctx"]) == ("t1", CTX)
