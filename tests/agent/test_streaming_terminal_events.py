"""Regression tests: `astream_events_turn`/`_run_graph_stream`
(app/agent/runtime.py) must surface the actual answer text for EVERY way a turn
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
(the web UI, `make chat`, `POST /chat/stream/queued`) already
renders it correctly with no changes of its own.

Each test drives `astream_events_turn` against a hermetic fake graph via
a monkeypatched `init_graph_async` (bypassing the real durable
checkpointer — already covered separately by tests/agent/test_durable_checkpoint.py)
since only the EVENT SHAPE is under test here.
"""
import asyncio
import uuid

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage

from app.agent import moderation
from app.agent import runtime as agent_module
from app.agent.graph import GraphDeps, _estimate_tokens, build_graph
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


class _FakeTrace:
    def __init__(self):
        self.updates: list[dict] = []

    def update(self, **kwargs):
        self.updates.append(kwargs)


class TestTraceOutputMatchesWhatTheClientActuallySaw:
    """Real bug, found live via Langfuse: a turn that never streamed any
    real tokens (a semantic-cache hit, a reject_* short-circuit, or —
    caught live — the no_answer safety net after two empty LLM attempts)
    correctly streamed its FALLBACK text to the actual SSE client (see
    TestSemanticCacheHitStreamsTheCachedAnswer above), but
    `trace.update(output=...)` ran BEFORE that fallback text was computed,
    reading an empty `final_answer` list — so Langfuse recorded the turn's
    output as "" even though a real user saw real text. Fixed by moving
    `trace.update` to after the fallback synthesis and appending the
    fallback text into `final_answer` itself, so the recorded output
    always matches the client-visible one."""

    def test_a_cache_hit_records_the_cached_text_on_the_trace_not_a_blank(self, monkeypatch):
        fake_trace = _FakeTrace()
        monkeypatch.setattr(agent_module, "_open_trace", lambda *a, **k: (fake_trace, []))
        llm = GenericFakeChatModel(messages=iter([]))  # would raise if ever invoked

        def fake_cache_get(ctx, query):
            return "A cached answer, not freshly generated.", [{"marker": "[1]", "text": "..."}]

        graph_obj = build_graph(GraphDeps(llm=llm, cache_get=fake_cache_get))

        _events_for(graph_obj, "what is a checkpointer?", monkeypatch=monkeypatch)

        assert fake_trace.updates == [{"output": "A cached answer, not freshly generated."}]

    def test_a_normally_streamed_answer_still_records_correctly(self, monkeypatch):
        """Guards the fix against a regression in the common case: a turn
        that DID stream real tokens must still record exactly that text,
        not a duplicate or an empty one."""
        fake_trace = _FakeTrace()
        monkeypatch.setattr(agent_module, "_open_trace", lambda *a, **k: (fake_trace, []))
        llm = GenericFakeChatModel(
            messages=iter([AIMessage(content="A normal, freshly generated answer.")])
        )
        graph_obj = build_graph(GraphDeps(llm=llm))

        _events_for(graph_obj, "what is a checkpointer?", monkeypatch=monkeypatch)

        assert fake_trace.updates == [{"output": "A normal, freshly generated answer."}]


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

    def test_followups_own_llm_call_never_leaks_into_the_token_stream(self, monkeypatch):
        """Real bug, caught live via Langfuse: astream_events emits
        on_chat_model_stream for EVERY chat-model call in the graph, not
        just the main answer's. suggest_followups makes its own SEPARATE
        llm.invoke() call — without filtering by which node a given stream
        event belongs to (metadata.langgraph_node), its generated
        questions streamed as "token" events too, landing concatenated
        onto the end of the real answer with no separator: a real user saw
        their answer's last citation marker immediately followed by the
        raw follow-up questions text, no space, no newline, before the
        (correct, separate) "followups" event even fired."""
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
                    AIMessage(content="Checkpointers persist state [1]."),
                    AIMessage(content="What is a MemorySaver?\nHow do I resume a run?"),
                ]
            )
        )
        graph_obj = build_graph(GraphDeps(llm=llm, search_docs=fake_search))

        events = _events_for(graph_obj, "what is a checkpointer?", monkeypatch=monkeypatch)

        token_events = [e for e in events if e["type"] == "token"]
        streamed_text = "".join(e["content"] for e in token_events)
        assert streamed_text == "Checkpointers persist state [1]."
        assert "MemorySaver" not in streamed_text
        assert "How do I resume a run" not in streamed_text

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


class TestRetryEventClearsTheStream:
    """A rejected answer's already-streamed tokens must never render
    concatenated with the retried answer's. Real bug, found live via
    Langfuse: a citation-retry loop's rejected (uncited) answer and its
    retried (cited) one streamed as "token" events back to back with no
    separator — the web UI's handleEvent just appends every token it gets,
    so the client rendered both answers run together as if they were one
    continuous response, with no indication a retry ever happened. Fixed
    by emitting a `{"type": "retry"}` event (see _run_graph_stream's own
    docstring) whenever retry_output runs, so a client knows to clear its
    buffer before the next round's tokens arrive."""

    def test_a_too_short_answer_retry_emits_a_retry_event_between_the_two_answers(
        self, monkeypatch
    ):
        llm = GenericFakeChatModel(
            messages=iter(
                [
                    AIMessage(content="Yes."),  # too short -> retry_output
                    AIMessage(content="Here is a sufficiently long final answer now."),
                ]
            )
        )
        graph_obj = build_graph(GraphDeps(llm=llm))

        events = _events_for(graph_obj, "is this true?", monkeypatch=monkeypatch)

        types_in_order = [e["type"] for e in events]
        assert types_in_order.count("retry") == 1
        retry_idx = types_in_order.index("retry")
        before = [e["content"] for e in events[:retry_idx] if e["type"] == "token"]
        after = [e["content"] for e in events[retry_idx:] if e["type"] == "token"]
        assert "".join(before) == "Yes."
        assert "".join(after) == "Here is a sufficiently long final answer now."

    def test_a_likely_uncited_answer_retry_also_emits_a_retry_event(self, monkeypatch):
        source_text = "Checkpointers persist state across a thread's whole lifetime reliably."

        def fake_search(query, ctx):
            cited = {
                "marker": "[1]",
                "doc_id": "d1",
                "title": "Checkpointers",
                "text": source_text,
                "score": 0.9,
            }
            return f"[1] {source_text}", [cited]

        llm = GenericFakeChatModel(
            messages=iter(
                [
                    AIMessage(content=source_text),  # paraphrased, uncited -> retry_output
                    AIMessage(content=f"{source_text} [1]"),
                ]
            )
        )
        graph_obj = build_graph(GraphDeps(llm=llm, search_docs=fake_search))

        events = _events_for(graph_obj, "what is a checkpointer?", monkeypatch=monkeypatch)

        assert sum(1 for e in events if e["type"] == "retry") == 1

    def test_normal_streaming_with_no_retry_never_emits_a_retry_event(self, monkeypatch):
        llm = GenericFakeChatModel(
            messages=iter([AIMessage(content="A normal, freshly generated answer.")])
        )
        graph_obj = build_graph(GraphDeps(llm=llm))

        events = _events_for(graph_obj, "what is a checkpointer?", monkeypatch=monkeypatch)

        assert not any(e["type"] == "retry" for e in events)


class TestRetryExhaustedReplacesAlreadyStreamedContent:
    """Real bug, caught live immediately after shipping
    leaks_system_prompt detection: retry_exhausted (graph.py)
    unconditionally replaces the last message rather than trusting it
    (it's content check_output already judged bad on repeat) — but that
    message's own tokens already streamed live via on_chat_model_stream
    before the graph decided to reject it a SECOND time, exactly like any
    other answer; nothing about being "about to be replaced" stops a
    model's tokens from reaching the client as they're generated.
    Observed live: a leaked system prompt streamed to a real user in
    full, TWICE (once per retry round), with the honest fallback the
    checkpointed state correctly held never actually reaching them."""

    def test_client_sees_the_honest_fallback_not_the_repeatedly_rejected_text(
        self, monkeypatch
    ):
        llm = GenericFakeChatModel(
            messages=iter([AIMessage(content="Yes."), AIMessage(content="Yes.")])
        )
        graph_obj = build_graph(GraphDeps(llm=llm))

        events = _events_for(graph_obj, "is that right?", monkeypatch=monkeypatch)

        token_events = [e for e in events if e["type"] == "token"]
        streamed_text = "".join(e["content"] for e in token_events)
        assert "wasn't able to put together" in streamed_text

        types_in_order = [e["type"] for e in events]
        # One retry from retry_output (round 1 rejected), one from
        # retry_exhausted giving up on round 2's identical rejection.
        assert types_in_order.count("retry") == 2
        last_retry_idx = len(types_in_order) - 1 - types_in_order[::-1].index("retry")
        # The give-up "retry" is immediately followed by the honest
        # fallback as a fresh token event — not silence, and not the
        # graph just ending with nothing more shown.
        assert types_in_order[last_retry_idx + 1] == "token"
        assert "wasn't able to put together" in events[last_retry_idx + 1]["content"]


class TestCompactedEventSignalsHistoryTrimming:
    """graph.py's compact_history (GRAPH_PATTERNS.md pattern 41) runs on
    EVERY turn but only actually trims once history crosses its ceiling —
    with no signal for that, a client just goes quiet for however long the
    summarization LLM call takes, indistinguishable from any other slow
    turn. Fixed by emitting `{"type": "compacted"}` only on a turn that
    actually trimmed something, so a UI can show a transient status
    instead (see _run_graph_stream's own docstring)."""

    def test_a_compacting_turn_emits_exactly_one_compacted_event(self, monkeypatch):
        # Same ceiling/floor derivation tests/agent/test_graph_integration.py's
        # TestHistorySummarization uses: real turns driven through the
        # graph (not a hand-poked aupdate_state, which LangGraph rejects
        # as an ambiguous update with no originating node) via 3 REAL
        # ainvoke() calls, sized so the 4th (triggering) question — already
        # appended to state by the time compact_history reads it — is what
        # pushes estimated non-system history over `ceiling`.
        pre_questions = [HumanMessage(content=f"question {i}?") for i in range(3)]
        pre_answers = [
            AIMessage(content=f"Answer number {i}, long enough to pass the length check.")
            for i in range(3)
        ]
        triggering_question = "question 4?"
        turns = [m for pair in zip(pre_questions, pre_answers, strict=True) for m in pair]
        ceiling = _estimate_tokens(turns)
        floor = _estimate_tokens(turns[2:] + [HumanMessage(content=triggering_question)])

        summary_response = AIMessage(content="a summary of the earlier turns")
        final_response = AIMessage(content="A normal, freshly generated final answer.")
        llm = GenericFakeChatModel(
            messages=iter([*pre_answers, summary_response, final_response])
        )
        graph_obj = build_graph(
            GraphDeps(llm=llm), history_token_ceiling=ceiling, history_token_floor=floor
        )

        async def fake_init_graph_async():
            return graph_obj

        monkeypatch.setattr(agent_module, "init_graph_async", fake_init_graph_async)
        thread_id = str(uuid.uuid4())
        cfg = {"configurable": {"thread_id": thread_id, "ctx": TEST_CTX}}

        async def _run():
            for q in pre_questions:
                await graph_obj.ainvoke({"messages": [q]}, config=cfg)
            return [
                event
                async for event in agent_module.astream_events_turn(
                    triggering_question, thread_id, TEST_CTX
                )
            ]

        events = asyncio.run(_run())

        assert sum(1 for e in events if e["type"] == "compacted") == 1

    def test_a_turn_within_budget_never_emits_a_compacted_event(self, monkeypatch):
        llm = GenericFakeChatModel(
            messages=iter([AIMessage(content="A normal, freshly generated answer.")])
        )
        graph_obj = build_graph(GraphDeps(llm=llm))

        events = _events_for(graph_obj, "what is a checkpointer?", monkeypatch=monkeypatch)

        assert not any(e["type"] == "compacted" for e in events)


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
