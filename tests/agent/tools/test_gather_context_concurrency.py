"""`gather_context`'s document search and memory search are independent, so they run concurrently:
the pre-fetch costs the slower of the two, not their sum."""
import asyncio

import pytest

from app.agent.tools import builtin
from tests.conftest import TEST_CTX


class _Hit:
    def __init__(self, text, score=1.0):
        self.id = text
        self.score = score
        self.payload = {"text": text, "title": "t", "doc_id": text}


async def test_the_two_searches_are_in_flight_at_the_same_time(monkeypatch):
    docs_started, memories_started = asyncio.Event(), asyncio.Event()

    async def documents(ctx, query):
        docs_started.set()
        # Only returns once the OTHER search has started: sequential calls would deadlock here.
        await asyncio.wait_for(memories_started.wait(), 1)
        return [_Hit("a document")]

    async def memories(ctx, query):
        memories_started.set()
        await asyncio.wait_for(docs_started.wait(), 1)
        return [_Hit("a memory")]

    monkeypatch.setattr(builtin, "_document_hits", documents)
    monkeypatch.setattr(builtin, "_memory_hits", memories)

    context, citations = await builtin.gather_context(TEST_CTX, "q")

    assert [c["marker"] for c in citations] == ["[1]", "[2]"]  # one continuous numbering
    assert "a document" in context and "a memory" in context


async def test_a_failure_in_one_search_is_raised_as_is_and_the_other_is_not_left_running(monkeypatch):
    memory_finished = asyncio.Event()

    async def documents(ctx, query):
        raise ConnectionError("qdrant is down")

    async def memories(ctx, query):
        await asyncio.sleep(0.05)
        memory_finished.set()
        return []

    monkeypatch.setattr(builtin, "_document_hits", documents)
    monkeypatch.setattr(builtin, "_memory_hits", memories)

    with pytest.raises(ConnectionError, match="qdrant is down"):  # the original type, not a group
        await builtin.gather_context(TEST_CTX, "q")
    assert memory_finished.is_set(), "the sibling search was abandoned instead of awaited"


async def test_a_search_the_policy_does_not_permit_is_not_run(monkeypatch):
    calls = []

    async def documents(ctx, query):
        calls.append("documents")
        return [_Hit("a document")]

    async def memories(ctx, query):
        calls.append("memories")
        return []

    monkeypatch.setattr(builtin, "_document_hits", documents)
    monkeypatch.setattr(builtin, "_memory_hits", memories)
    monkeypatch.setattr(builtin.DEFAULT_POLICY, "permit", lambda action, ctx: action == "search")

    await builtin.gather_context(TEST_CTX, "q")
    assert calls == ["documents"]
