"""Regression tests: `astream_events_turn`/`_run_graph_stream`
(app/agent.py) must surface the actual answer text for EVERY way a turn
can end, not just the ones that stream token-by-token through the LLM.

Real bug, found live: `final_answer` only ever accumulates from
`on_chat_model_stream` events — so any node that produces a final
AIMessage WITHOUT calling the chat model (reject_input, reject_context,
reject_moderation, context_window_exceeded, and a semantic-cache HIT,
pattern 22) left the streaming caller with nothing but a bare
`{"type": "done"}` and no way to learn what the answer actually was.
Reproduced against a real cached "hi" response before fixing it by
falling back to `state.values["messages"][-1].content` — sent as a
synthetic "token" event, not a new event type, so every existing client
(the web UI, `make chat-stream`, `POST /chat/stream/queued`) already
renders it correctly with no changes of its own.

Each test drives `astream_events_turn` against a hermetic fake graph via
a monkeypatched `init_graph_async` (bypassing the real durable
checkpointer — already covered separately by tests/test_durable_checkpoint.py)
since only the EVENT SHAPE is under test here.
"""
import asyncio
import uuid

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from app import agent as agent_module
from app import moderation
from app.graph import GraphDeps, build_graph
from tests.conftest import TEST_CTX


def _events_for(graph_obj, text, thread_id=None, ctx=TEST_CTX, monkeypatch=None):
    async def fake_init_graph_async():
        return graph_obj

    monkeypatch.setattr(agent_module, "init_graph_async", fake_init_graph_async)

    async def _run():
        return [
            event
            async for event in agent_module.astream_events_turn(
                text, thread_id or str(uuid.uuid4()), ctx
            )
        ]

    return asyncio.run(_run())


class TestRejectPathsSurfaceTheirText:
    def test_empty_input_streams_the_rejection_message(self, monkeypatch):
        llm = GenericFakeChatModel(messages=iter([]))  # would raise if ever invoked
        graph_obj = build_graph(GraphDeps(llm=llm))

        events = _events_for(graph_obj, "", monkeypatch=monkeypatch)

        token_events = [e for e in events if e["type"] == "token"]
        assert len(token_events) == 1
        assert "didn't receive a question" in token_events[0]["content"]
        assert events[-1] == {"type": "done"}

    def test_missing_ctx_streams_the_rejection_message(self, monkeypatch):
        llm = GenericFakeChatModel(messages=iter([]))
        graph_obj = build_graph(GraphDeps(llm=llm))

        events = _events_for(graph_obj, "hello", ctx=None, monkeypatch=monkeypatch)

        token_events = [e for e in events if e["type"] == "token"]
        assert len(token_events) == 1
        assert "couldn't verify who's asking" in token_events[0]["content"]

    def test_moderation_block_streams_the_rejection_message(self, monkeypatch):
        llm = GenericFakeChatModel(messages=iter([]))
        graph_obj = build_graph(GraphDeps(llm=llm))
        monkeypatch.setattr(
            moderation, "screen", lambda text: type("R", (), {"allowed": False})()
        )

        events = _events_for(graph_obj, "anything", monkeypatch=monkeypatch)

        token_events = [e for e in events if e["type"] == "token"]
        assert len(token_events) == 1
        assert "can't help with that request" in token_events[0]["content"]


class TestSemanticCacheHitStreamsTheCachedAnswer:
    def test_cache_hit_streams_the_cached_text_not_just_done(self, monkeypatch):
        llm = GenericFakeChatModel(messages=iter([]))  # would raise if ever invoked

        def fake_cache_get(ctx, query):
            return "A cached answer, not freshly generated.", [{"marker": "[1]", "text": "..."}]

        graph_obj = build_graph(GraphDeps(llm=llm, cache_get=fake_cache_get))

        events = _events_for(graph_obj, "what is a checkpointer?", monkeypatch=monkeypatch)

        token_events = [e for e in events if e["type"] == "token"]
        assert len(token_events) == 1
        assert token_events[0]["content"] == "A cached answer, not freshly generated."
        assert events[-1] == {"type": "done"}


class TestFollowupsEventIsSurfaced:
    """suggest_followups (pattern 27) computes real follow-up questions
    into state["followups"], but this streaming path never sent them to
    the client at all — a real, previously-undiscovered gap: the web UI
    already ships CSS for rendering them as clickable suggestion chips
    but had no event to populate it from, and the raw model text got
    rendered as one undifferentiated blob instead."""

    def test_a_grounded_answer_streams_a_followups_event_before_done(self, monkeypatch):
        def fake_search(query, ctx):
            cited = {
                "marker": "[1]",
                "doc_id": "d1",
                "title": "Checkpointers",
                "text": "Checkpointers persist state.",
                "score": 0.9,
            }
            return "[1] Checkpointers persist state.", [cited]

        llm = GenericFakeChatModel(
            messages=iter(
                [
                    AIMessage(content="Checkpointers persist state [1], a real grounded answer."),
                    AIMessage(content="What is a MemorySaver?\nHow do I resume a run?"),
                ]
            )
        )
        graph_obj = build_graph(GraphDeps(llm=llm, search_docs=fake_search))

        events = _events_for(graph_obj, "what is a checkpointer?", monkeypatch=monkeypatch)

        followup_events = [e for e in events if e["type"] == "followups"]
        assert len(followup_events) == 1
        assert followup_events[0]["items"] == [
            "What is a MemorySaver?",
            "How do I resume a run?",
        ]
        # Ordering: followups comes after citations, both before done.
        types_in_order = [e["type"] for e in events]
        assert types_in_order.index("followups") > types_in_order.index("citations")
        assert types_in_order[-1] == "done"

    def test_an_uncited_answer_streams_no_followups_event(self, monkeypatch):
        """suggest_followups itself skips an uncited answer (nothing to
        derive follow-ups from) — this just proves the streaming layer
        doesn't invent one on top."""
        llm = GenericFakeChatModel(
            messages=iter([AIMessage(content="A plain, uncited general-knowledge answer.")])
        )
        graph_obj = build_graph(GraphDeps(llm=llm))

        events = _events_for(graph_obj, "what is the capital of France?", monkeypatch=monkeypatch)

        assert not any(e["type"] == "followups" for e in events)


class TestNormalStreamingIsUnaffected:
    def test_a_real_llm_answer_still_streams_token_by_token_with_no_extra_synthetic_event(
        self, monkeypatch
    ):
        """Guards against double-answering: a turn that DID stream
        through the LLM must not also get a synthetic fallback token."""
        llm = GenericFakeChatModel(
            messages=iter([AIMessage(content="A normal, freshly generated answer.")])
        )
        graph_obj = build_graph(GraphDeps(llm=llm))

        events = _events_for(graph_obj, "what is a checkpointer?", monkeypatch=monkeypatch)

        token_events = [e for e in events if e["type"] == "token"]
        assert "".join(e["content"] for e in token_events) == "A normal, freshly generated answer."
        # Genuinely streamed word-by-word (GenericFakeChatModel's own
        # chunking), not a single synthetic fallback token appended on
        # top of the real stream.
        assert len(token_events) > 1
