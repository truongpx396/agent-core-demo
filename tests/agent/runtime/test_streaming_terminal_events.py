"""Regression tests: `astream_events_turn`/`_run_graph_stream`
(app/agent/runtime/core.py) must surface the actual answer text for EVERY way a turn
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
checkpointer — already covered separately by tests/agent/runtime/test_durable_checkpoint.py)
since only the EVENT SHAPE is under test here.
"""
import asyncio
import json
import uuid
from types import SimpleNamespace

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage

from app.agent.graph.build import GraphDeps
from app.agent.graph.compaction import _estimate_tokens
from app.agent.runtime import core as agent_module
from app.agent.runtime import stream as stream_module
from app.core import metrics, moderation
from tests.conftest import TEST_CTX, metric_value
from tests.ecorp import build_graph


async def _events_for(graph_obj, text, thread_id=None, ctx=TEST_CTX, monkeypatch=None):
    async def fake_init_graph_async():
        return graph_obj

    monkeypatch.setattr(agent_module, "init_graph_async", fake_init_graph_async)

    async def _run():
        return [
            event
            async for event in stream_module.astream_events_turn(
                text, thread_id or str(uuid.uuid4()), ctx
            )
        ]

    return await _run()


class TestRejectPathsSurfaceTheirText:
    async def test_empty_input_streams_the_rejection_message(self, monkeypatch):
        llm = GenericFakeChatModel(messages=iter([]))  # would raise if ever invoked
        graph_obj = build_graph(GraphDeps(llm=llm))

        events = await _events_for(graph_obj, "", monkeypatch=monkeypatch)

        token_events = [e for e in events if e["type"] == "token"]
        assert len(token_events) == 1
        assert "didn't receive a question" in token_events[0]["content"]
        assert events[-1] == {"type": "done"}

    async def test_missing_ctx_streams_the_rejection_message(self, monkeypatch):
        llm = GenericFakeChatModel(messages=iter([]))
        graph_obj = build_graph(GraphDeps(llm=llm))

        events = await _events_for(graph_obj, "hello", ctx=None, monkeypatch=monkeypatch)

        token_events = [e for e in events if e["type"] == "token"]
        assert len(token_events) == 1
        assert "couldn't verify who's asking" in token_events[0]["content"]

    async def test_moderation_block_streams_the_rejection_message(self, monkeypatch):
        llm = GenericFakeChatModel(messages=iter([]))
        graph_obj = build_graph(GraphDeps(llm=llm))
        async def fake_screen(text):
            return type("R", (), {"allowed": False})()

        monkeypatch.setattr(moderation, "screen", fake_screen)

        events = await _events_for(graph_obj, "anything", monkeypatch=monkeypatch)

        token_events = [e for e in events if e["type"] == "token"]
        assert len(token_events) == 1
        assert "can't help with that request" in token_events[0]["content"]


class TestSemanticCacheHitStreamsTheCachedAnswer:
    async def test_cache_hit_streams_the_cached_text_not_just_done(self, monkeypatch):
        llm = GenericFakeChatModel(messages=iter([]))  # would raise if ever invoked

        async def fake_cache_get(ctx, query):
            return "A cached answer, not freshly generated.", [{"marker": "[1]", "text": "..."}]

        graph_obj = build_graph(GraphDeps(llm=llm, cache_get=fake_cache_get))

        events = await _events_for(graph_obj, "what is a checkpointer?", monkeypatch=monkeypatch)

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

    async def test_a_cache_hit_records_the_cached_text_on_the_trace_not_a_blank(self, monkeypatch):
        fake_trace = _FakeTrace()
        monkeypatch.setattr(stream_module, "_open_trace", lambda *a, **k: (fake_trace, []))
        llm = GenericFakeChatModel(messages=iter([]))  # would raise if ever invoked

        async def fake_cache_get(ctx, query):
            return "A cached answer, not freshly generated.", [{"marker": "[1]", "text": "..."}]

        graph_obj = build_graph(GraphDeps(llm=llm, cache_get=fake_cache_get))

        await _events_for(graph_obj, "what is a checkpointer?", monkeypatch=monkeypatch)

        assert fake_trace.updates == [{"output": "A cached answer, not freshly generated."}]

    async def test_a_normally_streamed_answer_still_records_correctly(self, monkeypatch):
        """Guards the fix against a regression in the common case: a turn
        that DID stream real tokens must still record exactly that text,
        not a duplicate or an empty one."""
        fake_trace = _FakeTrace()
        monkeypatch.setattr(stream_module, "_open_trace", lambda *a, **k: (fake_trace, []))
        llm = GenericFakeChatModel(
            messages=iter([AIMessage(content="A normal, freshly generated answer.")])
        )
        graph_obj = build_graph(GraphDeps(llm=llm))

        await _events_for(graph_obj, "what is a checkpointer?", monkeypatch=monkeypatch)

        assert fake_trace.updates == [{"output": "A normal, freshly generated answer."}]


class TestAStreamClosedBeforeItsTerminalEventStillFinishesTheTrace:
    """The consumer can stop iterating before the turn ends: the queue worker's `publish_result`
    raises mid-loop, or its task is cancelled while it awaits the publish. Either way the
    cancellation lands in the CONSUMER's frame, so the stream's own `CancelledError` branch never
    runs; the generator is later closed with `GeneratorExit` at the `yield` it was suspended on.
    That used to match no `except` clause, so the trace kept its input and never got an output or
    a level, indistinguishable in Langfuse from a turn still in flight."""

    async def test_closing_the_stream_early_records_what_streamed_and_a_warning(self, monkeypatch):
        fake_trace = _FakeTrace()
        monkeypatch.setattr(stream_module, "_open_trace", lambda *a, **k: (fake_trace, []))
        llm = GenericFakeChatModel(
            messages=iter([AIMessage(content="A normal answer that streams in several tokens.")])
        )
        graph_obj = build_graph(GraphDeps(llm=llm))

        async def fake_init_graph_async():
            return graph_obj

        monkeypatch.setattr(agent_module, "init_graph_async", fake_init_graph_async)

        stream = stream_module.astream_events_turn("what is a checkpointer?", str(uuid.uuid4()), TEST_CTX)
        first_token = None
        async for event in stream:
            if event["type"] == "token":
                first_token = event["content"]
                break
        await stream.aclose()

        assert first_token
        assert len(fake_trace.updates) == 1
        update = fake_trace.updates[0]
        assert update["level"] == "WARNING"
        assert update["output"].startswith(first_token)
        assert "[abandoned: stream closed before the turn finished]" in update["output"]

    async def test_closing_the_stream_early_counts_the_turn_as_abandoned_not_cancelled(self, monkeypatch):
        """The turn's outcome used to be recorded on every exit but this one, so a run
        whose consumer vanished left no trace in `agent_requests_total` at all. It is counted under
        its own outcome: nobody asked for a stop, so it is not "cancelled", and the POST /chat/cancel
        counter stays where it was."""
        monkeypatch.setattr(stream_module, "_open_trace", lambda *a, **k: (_FakeTrace(), []))
        llm = GenericFakeChatModel(
            messages=iter([AIMessage(content="A normal answer that streams in several tokens.")])
        )
        graph_obj = build_graph(GraphDeps(llm=llm))

        async def fake_init_graph_async():
            return graph_obj

        monkeypatch.setattr(agent_module, "init_graph_async", fake_init_graph_async)
        abandoned = metric_value(metrics.agent_requests_total, outcome="abandoned")
        cancelled = metric_value(metrics.agent_requests_total, outcome="cancelled")
        streaming_cancellations = metric_value(metrics.agent_streaming_cancellation_total)

        stream = stream_module.astream_events_turn("what is a checkpointer?", str(uuid.uuid4()), TEST_CTX)
        async for event in stream:
            if event["type"] == "token":
                break
        await stream.aclose()

        assert metric_value(metrics.agent_requests_total, outcome="abandoned") == abandoned + 1
        assert metric_value(metrics.agent_requests_total, outcome="cancelled") == cancelled
        assert metric_value(metrics.agent_streaming_cancellation_total) == streaming_cancellations

    async def test_closing_the_stream_at_a_synthetic_answer_still_records_the_finished_turn(self, monkeypatch):
        """A turn that never called the model (here a semantic-cache hit) streams its answer as ONE
        synthetic token. That token used to be yielded BEFORE the trace output and the turn's
        metrics were written, and that part of the generator is covered by no `except` branch, so
        a consumer that closed the stream at it left a finished turn with a blank trace and no
        outcome. The turn is recorded first now; the client still gets the same events."""
        fake_trace = _FakeTrace()
        monkeypatch.setattr(stream_module, "_open_trace", lambda *a, **k: (fake_trace, []))

        async def fake_cache_get(ctx, query):
            return "A cached answer, not freshly generated.", [{"marker": "[1]", "text": "..."}]

        graph_obj = build_graph(GraphDeps(llm=GenericFakeChatModel(messages=iter([])), cache_get=fake_cache_get))

        async def fake_init_graph_async():
            return graph_obj

        monkeypatch.setattr(agent_module, "init_graph_async", fake_init_graph_async)
        success = metric_value(metrics.agent_requests_total, outcome="success")
        abandoned = metric_value(metrics.agent_requests_total, outcome="abandoned")

        stream = stream_module.astream_events_turn("what is a checkpointer?", str(uuid.uuid4()), TEST_CTX)
        async for event in stream:
            if event["type"] == "token":
                break
        await stream.aclose()

        assert fake_trace.updates == [{"output": "A cached answer, not freshly generated."}]
        assert metric_value(metrics.agent_requests_total, outcome="success") == success + 1
        assert metric_value(metrics.agent_requests_total, outcome="abandoned") == abandoned


class TestClosingTheStreamStopsTheGraphRun:
    """`astream_events` runs the graph in a task of its own, and that task stops only when the
    generator is closed. `_run_graph_stream` does not close the LangGraph iterator itself: it is
    finalised by the event loop's hook a loop turn after the stream is closed (measured: about a
    millisecond, with no further model output in between). That is accepted, and this pins what
    matters about it: a consumer that leaves must not leave the run behind it, still calling the
    model and its tools, until it finishes on its own."""

    async def test_the_model_stops_shortly_after_the_stream_is_closed(self, monkeypatch):
        chunks = []
        stopped = asyncio.Event()

        class _SlowModel(GenericFakeChatModel):
            async def _astream(self, *args, **kwargs):
                try:
                    async for chunk in super()._astream(*args, **kwargs):
                        chunks.append(chunk)
                        yield chunk
                        await asyncio.sleep(0.05)
                finally:
                    stopped.set()

        words = " ".join(f"w{i}" for i in range(80))  # about four seconds if nothing stops it
        graph_obj = build_graph(GraphDeps(llm=_SlowModel(messages=iter([AIMessage(content=words)]))))

        async def fake_init_graph_async():
            return graph_obj

        monkeypatch.setattr(agent_module, "init_graph_async", fake_init_graph_async)
        monkeypatch.setattr(stream_module, "_open_trace", lambda *a, **k: (None, []))

        stream = stream_module.astream_events_turn("what is a checkpointer?", str(uuid.uuid4()), TEST_CTX)
        async for event in stream:
            if event["type"] == "token":
                break
        chunks_at_close = len(chunks)
        await stream.aclose()

        await asyncio.wait_for(stopped.wait(), timeout=2)
        assert len(chunks) <= chunks_at_close + 1, "the model kept producing output after the consumer left"


class TestTheTurnTraceIsNeverLeftWithoutAnOutput:
    """`_turn_trace` is the net under `_run_graph_stream`: a failure between opening the trace and
    starting the stream, or any exit the stream did not handle, used to leave a trace with an input
    and nothing else, which looks in Langfuse like a turn still running."""

    async def test_a_failure_before_the_stream_starts_finishes_the_trace_as_an_error(self, monkeypatch):
        fake_trace = _FakeTrace()
        monkeypatch.setattr(stream_module, "_open_trace", lambda *a, **k: (fake_trace, []))

        def broken_input(text, images):
            raise RuntimeError("could not decode the attached image")

        monkeypatch.setattr(stream_module, "_build_human_content", broken_input)
        graph_obj = build_graph(GraphDeps(llm=GenericFakeChatModel(messages=iter([]))))

        async def fake_init_graph_async():
            return graph_obj

        monkeypatch.setattr(agent_module, "init_graph_async", fake_init_graph_async)

        with pytest.raises(RuntimeError, match="could not decode"):
            async for _ in stream_module.astream_events_turn("hi", str(uuid.uuid4()), TEST_CTX):
                pass

        assert fake_trace.updates == [{"output": "error: could not decode the attached image", "level": "ERROR"}]

    def test_each_kind_of_exit_gets_its_own_output_and_level(self, monkeypatch):
        cases = [
            (ValueError("boom"), "error: boom", "ERROR"),
            (GeneratorExit(), "[abandoned: stream closed before the turn finished]", "WARNING"),
            (asyncio.CancelledError(), "[cancelled: task cancelled]", "WARNING"),
        ]
        for exc, output, level in cases:
            fake_trace = _FakeTrace()
            monkeypatch.setattr(stream_module, "_open_trace", lambda *a, _t=fake_trace, **k: (_t, []))
            with pytest.raises(type(exc)):
                with stream_module._turn_trace("n", "s", "in"):
                    raise exc
            assert fake_trace.updates == [{"output": output, "level": level}], type(exc).__name__

    def test_a_trace_the_stream_already_finished_is_not_overwritten(self, monkeypatch):
        fake_trace = _FakeTrace()
        monkeypatch.setattr(stream_module, "_open_trace", lambda *a, **k: (fake_trace, []))

        with pytest.raises(GeneratorExit):
            with stream_module._turn_trace("n", "s", "in") as (trace, _callbacks):
                trace.update(output="partial answer [abandoned: stream closed before the turn finished]", level="WARNING")
                raise GeneratorExit

        assert len(fake_trace.updates) == 1

    def test_a_clean_exit_adds_nothing(self, monkeypatch):
        fake_trace = _FakeTrace()
        monkeypatch.setattr(stream_module, "_open_trace", lambda *a, **k: (fake_trace, []))

        with stream_module._turn_trace("n", "s", "in"):
            pass

        assert fake_trace.updates == []

    def test_without_langfuse_updates_are_a_no_op_not_an_error(self, monkeypatch):
        monkeypatch.setattr(stream_module, "_open_trace", lambda *a, **k: (None, ["metrics-callback"]))

        with pytest.raises(ValueError):
            with stream_module._turn_trace("n", "s", "in") as (trace, callbacks):
                assert callbacks == ["metrics-callback"]
                trace.update(output="anything", level="WARNING")
                raise ValueError("boom")


class TestAStreamThatEndsWithWorkPendingButNoApprovalWaiting:
    """Nodes still pending, none at an interrupt: the stream ended while something else was still running on
    this thread (another message into the same conversation at the same moment; the worker's per-thread lock stops
    that, but the Telegram and CLI paths do not take it). The stream used to index into an interrupt that was not
    there, from a part of the generator no handler covers, so a raw IndexError escaped it (seen once in #171's CI)."""

    async def test_it_reports_thread_busy_instead_of_raising(self):
        class _Graph:
            async def astream_events(self, *args, **kwargs):
                return
                yield  # pragma: no cover - makes this an async generator

            async def aget_state(self, config):
                return SimpleNamespace(
                    values={"messages": [HumanMessage(content="q")]},
                    next=("agent",),
                    tasks=[SimpleNamespace(interrupts=[])],
                )

        errors_before = metric_value(metrics.agent_requests_total, outcome="error")

        events = [e async for e in stream_module._run_graph_stream(_Graph(), {}, {}, None)]

        assert [e["type"] for e in events] == ["error"]
        assert events[0]["code"] == "thread_busy"
        assert metric_value(metrics.agent_requests_total, outcome="error") == errors_before + 1


class TestFollowupsEventIsSurfaced:
    """suggest_followups (pattern 27) computes real follow-up questions
    into state["followups"], but this streaming path never sent them to
    the client at all — a real, previously-undiscovered gap: the web UI
    already ships CSS for rendering them as clickable suggestion chips
    but had no event to populate it from, and the raw model text got
    rendered as one undifferentiated blob instead."""

    async def test_a_grounded_answer_streams_a_followups_event_before_done(self, monkeypatch):
        async def fake_search(query, ctx):
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

        events = await _events_for(graph_obj, "what is a checkpointer?", monkeypatch=monkeypatch)

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

    async def test_followups_own_llm_call_never_leaks_into_the_token_stream(self, monkeypatch):
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
        async def fake_search(query, ctx):
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

        events = await _events_for(graph_obj, "what is a checkpointer?", monkeypatch=monkeypatch)

        token_events = [e for e in events if e["type"] == "token"]
        streamed_text = "".join(e["content"] for e in token_events)
        assert streamed_text == "Checkpointers persist state [1]."
        assert "MemorySaver" not in streamed_text
        assert "How do I resume a run" not in streamed_text

    async def test_an_uncited_answer_streams_no_followups_event(self, monkeypatch):
        """suggest_followups itself skips an uncited answer (nothing to
        derive follow-ups from) — this just proves the streaming layer
        doesn't invent one on top."""
        llm = GenericFakeChatModel(
            messages=iter([AIMessage(content="A plain, uncited general-knowledge answer.")])
        )
        graph_obj = build_graph(GraphDeps(llm=llm))

        events = await _events_for(graph_obj, "what is the capital of France?", monkeypatch=monkeypatch)

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

    async def test_a_too_short_answer_retry_emits_a_retry_event_between_the_two_answers(
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

        events = await _events_for(graph_obj, "is this true?", monkeypatch=monkeypatch)

        types_in_order = [e["type"] for e in events]
        assert types_in_order.count("retry") == 1
        retry_idx = types_in_order.index("retry")
        before = [e["content"] for e in events[:retry_idx] if e["type"] == "token"]
        after = [e["content"] for e in events[retry_idx:] if e["type"] == "token"]
        assert "".join(before) == "Yes."
        assert "".join(after) == "Here is a sufficiently long final answer now."

    async def test_a_likely_misattributed_answer_retry_also_emits_a_retry_event(self, monkeypatch):
        """Was an "uncited" scenario; a genuinely uncited-but-matching
        answer now gets fixed in place by check_output's own citation
        auto-correction on the SAME round (see
        TestCheckOutputCitationAutoCorrectionStreaming below for that
        case) rather than triggering a real retry_output round, so it can
        no longer stand in for "a genuine model-retry round still emits a
        retry event" here. "misattributed" isn't auto-corrected — fixing
        it means dropping a marker the model chose to attach, not adding
        one — so it still reaches retry_output for real, same as before.
        """
        source_text = "Checkpointers persist state across a thread's whole lifetime reliably."

        async def fake_search(query, ctx):
            cited = {
                "marker": "[1]",
                "doc_id": "d1",
                "title": "Checkpointers",
                "text": source_text,
                "score": 0.9,
            }
            return f"[1] {source_text}", [cited]

        misattributed = "Vector databases use approximate nearest neighbor search for speed [1]."
        llm = GenericFakeChatModel(
            messages=iter(
                [
                    AIMessage(content=misattributed),  # [1] cited, unrelated to its source -> retry_output
                    AIMessage(content=f"{source_text} [1]"),
                ]
            )
        )
        graph_obj = build_graph(GraphDeps(llm=llm, search_docs=fake_search))

        events = await _events_for(graph_obj, "what is a checkpointer?", monkeypatch=monkeypatch)

        assert sum(1 for e in events if e["type"] == "retry") == 1

    async def test_normal_streaming_with_no_retry_never_emits_a_retry_event(self, monkeypatch):
        llm = GenericFakeChatModel(
            messages=iter([AIMessage(content="A normal, freshly generated answer.")])
        )
        graph_obj = build_graph(GraphDeps(llm=llm))

        events = await _events_for(graph_obj, "what is a checkpointer?", monkeypatch=monkeypatch)

        assert not any(e["type"] == "retry" for e in events)


class TestRetryExhaustedReplacesAlreadyStreamedContent:
    """Real bug, caught live immediately after shipping
    leaks_system_prompt detection: retry_exhausted (app/agent/graph/core.py)
    unconditionally replaces the last message rather than trusting it
    (it's content check_output already judged bad on repeat) — but that
    message's own tokens already streamed live via on_chat_model_stream
    before the graph decided to reject it a SECOND time, exactly like any
    other answer; nothing about being "about to be replaced" stops a
    model's tokens from reaching the client as they're generated.
    Observed live: a leaked system prompt streamed to a real user in
    full, TWICE (once per retry round), with the honest fallback the
    checkpointed state correctly held never actually reaching them."""

    async def test_client_sees_the_honest_fallback_not_the_repeatedly_rejected_text(
        self, monkeypatch
    ):
        # deferred_instead_of_acting — one of the two NOT trust-content
        # reasons (see app/agent/graph/core.py's _TRUST_CONTENT_RETRY_REASONS): pure
        # narration has zero answer value, so it must be replaced. (The
        # OTHER two — too_short, uncited — are deliberately TRUSTED and
        # shown as-is now; see TestRetryExhaustedTrustsAttributionOnlyFailures
        # below for that half of the behavior.)
        narration = "I will use the query_employees tool to look that up. Let's proceed with that."
        llm = GenericFakeChatModel(
            messages=iter([AIMessage(content=narration), AIMessage(content=narration)])
        )
        graph_obj = build_graph(GraphDeps(llm=llm))

        events = await _events_for(graph_obj, "who works in engineering?", monkeypatch=monkeypatch)

        token_events = [e for e in events if e["type"] == "token"]
        streamed_text = "".join(e["content"] for e in token_events)
        assert "try asking again and I'll go ahead with it" in streamed_text

        types_in_order = [e["type"] for e in events]
        # One retry from retry_output (round 1 rejected), one from
        # retry_exhausted giving up on round 2's identical rejection.
        assert types_in_order.count("retry") == 2
        last_retry_idx = len(types_in_order) - 1 - types_in_order[::-1].index("retry")
        # The give-up "retry" is immediately followed by the honest
        # fallback as a fresh token event — not silence, and not the
        # graph just ending with nothing more shown.
        assert types_in_order[last_retry_idx + 1] == "token"
        assert "try asking again and I'll go ahead with it" in events[last_retry_idx + 1]["content"]


class TestRetryExhaustedTrustsAttributionOnlyFailures:
    """The still-live half of _TRUST_CONTENT_RETRY_REASONS (app/agent/graph/core.py):
    "too_short" repeated twice reaches retry_exhausted (giving up after
    MAX_CONSECUTIVE_SAME_RETRY_REASON identical rounds), which trusts it
    and shows it as-is — no spurious SECOND "retry" clearing it out from
    under an already-decided answer, and no synthesized replacement token
    (retry_exhausted no-ops for this reason; nothing to replace).
    "uncited" used to be tested here too (see
    TestCheckOutputCitationAutoCorrectionStreaming below instead): a
    repeatedly-uncited but otherwise correct answer no longer needs
    retry_exhausted's trust at all, since check_output's own citation
    auto-correction fixes it directly on round 1, before a real retry
    round is even needed."""

    async def test_no_spurious_retry_when_too_short_content_is_trusted_on_exhaustion(
        self, monkeypatch
    ):
        llm = GenericFakeChatModel(
            messages=iter([AIMessage(content="Yes."), AIMessage(content="Yes.")])
        )
        graph_obj = build_graph(GraphDeps(llm=llm))

        events = await _events_for(graph_obj, "is this true?", monkeypatch=monkeypatch)

        types_in_order = [e["type"] for e in events]
        # Exactly one retry — round 1's rejection by retry_output. NONE
        # from retry_exhausted: it no-ops for "too_short," so no second
        # "retry" event fires (a client only ever clears its draft on an
        # actual "retry" event — round 2's real content, streamed after
        # the one retry above, is never cleared or replaced afterward).
        assert types_in_order.count("retry") == 1
        retry_idx = types_in_order.index("retry")
        after = [e["content"] for e in events[retry_idx:] if e["type"] == "token"]
        streamed_after_retry = "".join(after)
        # Round 2's real content streamed and was never cleared/replaced
        # by a second retry — it's the only text after the one real retry.
        assert streamed_after_retry == "Yes."
        assert "wasn't able to put together" not in streamed_after_retry


class TestCheckOutputCitationAutoCorrectionStreaming:
    """check_output's own citation auto-correction
    (insert_missing_citation_markers, app/agent/graph/core.py) mechanically edits an
    already-streamed answer — the SAME "tokens already reached the client
    before the graph changed them" problem retry_exhausted's in-place
    replacement (TestRetryExhaustedReplacesAlreadyStreamedContent above)
    solves, fixed the identical way in _run_graph_stream: a "retry" event
    to clear the client's stale (uncited) draft, then a synthesized
    "token" event carrying the corrected text. Real regression, found
    live (tests/live/test_prompt_injection_via_retrieval.py): a real
    model answered a question CORRECTLY, twice in a row, just without its
    citation marker — this used to be trusted-and-shown-uncited by
    retry_exhausted after 2 rounds; it's now fixed on round ONE instead,
    so a live-streaming client needs telling too, not just the
    checkpointed state."""

    async def test_streamed_answer_is_corrected_with_a_retry_and_replacement_token(
        self, monkeypatch
    ):
        source_text = "Ecorp support hours are 9am to 5pm on weekdays."

        async def fake_search(query, ctx):
            cited = {
                "marker": "[1]",
                "doc_id": "d1",
                "title": "Support",
                "text": source_text,
                "score": 0.9,
            }
            return f"[1] {source_text}", [cited]

        # Correct, on-topic, but never adds the [1] marker — the exact
        # live-observed failure shape. Only ONE fake response queued: if
        # this still needed a second round, GenericFakeChatModel would
        # raise on its exhausted iterator, failing this test loudly
        # rather than silently passing.
        correct_but_uncited = "Ecorp's support hours are from 9am to 5pm on weekdays."
        llm = GenericFakeChatModel(messages=iter([AIMessage(content=correct_but_uncited)]))
        graph_obj = build_graph(GraphDeps(llm=llm, search_docs=fake_search))

        events = await _events_for(graph_obj, "what are the support hours?", monkeypatch=monkeypatch)

        types_in_order = [e["type"] for e in events]
        assert types_in_order.count("retry") == 1
        retry_idx = types_in_order.index("retry")
        before = "".join(e["content"] for e in events[:retry_idx] if e["type"] == "token")
        after = "".join(e["content"] for e in events[retry_idx:] if e["type"] == "token")
        # The stale, uncited draft streamed live before the correction...
        assert before == correct_but_uncited
        assert "[1]" not in before
        # ...and the client is told to discard it and render the
        # corrected, cited text instead — sent as a single synthesized
        # "token" event, not fragmented (matches how retry_exhausted's
        # own replacement text is synthesized, not restreamed token by
        # token — there's no real model generation to fragment it).
        assert after == "Ecorp's support hours are from 9am to 5pm on weekdays [1]."


class TestCompactedEventSignalsHistoryTrimming:
    """graph.py's compact_history (GRAPH_PATTERNS.md pattern 41) runs on
    EVERY turn but only actually trims once history crosses its ceiling —
    with no signal for that, a client just goes quiet for however long the
    summarization LLM call takes, indistinguishable from any other slow
    turn. Fixed by emitting `{"type": "compacted"}` only on a turn that
    actually trimmed something, so a UI can show a transient status
    instead (see _run_graph_stream's own docstring)."""

    async def test_a_compacting_turn_emits_exactly_one_compacted_event(self, monkeypatch):
        # Same ceiling/floor derivation tests/agent/graph/test_graph_integration.py's
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
                async for event in stream_module.astream_events_turn(
                    triggering_question, thread_id, TEST_CTX
                )
            ]

        events = await _run()

        assert sum(1 for e in events if e["type"] == "compacted") == 1

    async def test_a_turn_within_budget_never_emits_a_compacted_event(self, monkeypatch):
        llm = GenericFakeChatModel(
            messages=iter([AIMessage(content="A normal, freshly generated answer.")])
        )
        graph_obj = build_graph(GraphDeps(llm=llm))

        events = await _events_for(graph_obj, "what is a checkpointer?", monkeypatch=monkeypatch)

        assert not any(e["type"] == "compacted" for e in events)


class TestNormalStreamingIsUnaffected:
    async def test_a_real_llm_answer_still_streams_token_by_token_with_no_extra_synthetic_event(
        self, monkeypatch
    ):
        """Guards against double-answering: a turn that DID stream
        through the LLM must not also get a synthetic fallback token."""
        llm = GenericFakeChatModel(
            messages=iter([AIMessage(content="A normal, freshly generated answer.")])
        )
        graph_obj = build_graph(GraphDeps(llm=llm))

        events = await _events_for(graph_obj, "what is a checkpointer?", monkeypatch=monkeypatch)

        token_events = [e for e in events if e["type"] == "token"]
        assert "".join(e["content"] for e in token_events) == "A normal, freshly generated answer."
        # Genuinely streamed word-by-word (GenericFakeChatModel's own
        # chunking), not a single synthetic fallback token appended on
        # top of the real stream.
        assert len(token_events) > 1


class _FakeTurnState:
    def __init__(self, messages):
        self.values = {
            "messages": messages,
            "used_citations": [],
            "ungrounded_claims_count": 0,
            "followups": [],
        }
        self.next = ()


class _FakeStreamGraph:
    """A minimal stand-in for the compiled graph `_run_graph_stream`
    drives — yields exactly the hand-built `astream_events` v2 event dicts
    given, rather than a real GenericFakeChatModel-backed graph. Chosen
    over driving a real tool-calling turn through `astream_events_turn`
    (this file's usual `_events_for` pattern): verified directly that
    GenericFakeChatModel's own `_stream` reconstruction raises "No
    generations found in stream" for an empty-content, tool-calls-only
    AIMessage once routed through `astream_events()` specifically (its
    plain `.invoke()`/`.ainvoke()` path — what tests/agent/runtime/test_concurrent_turns.py's
    own subagent tests use instead — doesn't hit this) — a pre-existing
    fake-model/streaming-API interaction limitation, reproduced even with
    ZERO subagent involvement, unrelated to the filtering logic under test
    here. Testing `_run_graph_stream`'s event TRANSLATION directly, with
    synthetic events shaped exactly like real `astream_events` v2 output,
    sidesteps that limitation entirely and tests precisely the code this
    change touched."""

    def __init__(self, events, final_messages):
        self._events = events
        self._final_messages = final_messages

    async def astream_events(self, graph_input, config=None, version="v2"):
        for event in self._events:
            yield event

    async def aget_state(self, cfg):
        return _FakeTurnState(self._final_messages)


def _chat_stream_event(content, *, subagent_name=None):
    metadata = {"langgraph_node": "agent"}
    if subagent_name:
        metadata["subagent_name"] = subagent_name
    return {
        "event": "on_chat_model_stream",
        "run_id": "fake-run",
        "name": "ChatOpenAI",
        "metadata": metadata,
        "data": {"chunk": AIMessageChunk(content=content)},
    }


def _tool_event(kind, tool_name, *, subagent_name=None):
    metadata = {"subagent_name": subagent_name} if subagent_name else {}
    data = {"input": {}} if kind == "on_tool_start" else {"output": "irrelevant"}
    return {"event": kind, "run_id": "fake-run", "name": tool_name, "metadata": metadata, "data": data}


async def _stream_events(fake_events, final_messages):
    async def _run():
        graph_obj = _FakeStreamGraph(fake_events, final_messages)
        cfg = {"configurable": {"thread_id": "fake-thread", "ctx": TEST_CTX}}
        return [
            event
            async for event in stream_module._run_graph_stream(graph_obj, {}, cfg, trace=None)
        ]

    return await _run()


class _RaisingStreamGraph:
    """`astream_events` raises `exc` as soon as it is iterated — what any
    unexpected failure inside a turn (a store outage, a driver error, a bug)
    looks like to `_run_graph_stream`."""

    def __init__(self, exc):
        self._exc = exc

    async def astream_events(self, graph_input, config=None, version="v2"):
        raise self._exc
        yield  # pragma: no cover - makes this an async generator

    async def aget_state(self, cfg):
        # Read after a failure so the tokens the turn had already spent can be
        # recorded (spec 008, B17; tests/agent/runtime/test_unfinished_turn_usage.py).
        # An empty checkpoint here: nothing was spent, nothing to record.
        return SimpleNamespace(values={}, next=(), tasks=[])


async def _stream_raising(exc):
    cfg = {"configurable": {"thread_id": "fake-thread", "ctx": TEST_CTX}}
    return [
        event
        async for event in stream_module._run_graph_stream(_RaisingStreamGraph(exc), {}, cfg, trace=None)
    ]


class TestAGraphFailureNeverLeaksItsMessageToTheCaller:
    """Real defect (fixed): the generic `except` in `_run_graph_stream` put
    `str(exc)` into the caller-facing `error` event, so any unexpected
    exception's own text — a driver error naming an internal host or a SQL
    fragment, a library message echoing an input — crossed the trust
    boundary to whoever was reading the SSE stream. Principle V requires
    caller-facing errors to use the ErrorCode envelope, and the envelope's
    message is for the *caller*, not for the operator (who has the logs).
    There was no test of this path at all."""

    SENTINEL = "could not connect to internal-db-host.example:5432"

    async def test_an_unexpected_exception_yields_a_generic_internal_error_with_no_leaked_text(self):
        events = await _stream_raising(RuntimeError(self.SENTINEL))

        assert [e["type"] for e in events] == ["error"]
        error = events[0]
        assert error["code"] == "internal"
        assert "internal-db-host" not in json.dumps(error)
        assert error["content"] == error["message"] and error["message"], "a blank error helps nobody"
        # Metadata only: the exception CLASS is safe and is what an operator
        # triaging a client report needs; its message is not.
        assert error["details"] == {"error_class": "RuntimeError"}

    async def test_a_timeout_still_reports_a_timeout_not_an_internal_error(self):
        events = await _stream_raising(TimeoutError())

        assert [e["type"] for e in events] == ["error"]
        assert events[0]["code"] == "timeout"
        assert "timeout" in events[0]["message"].lower()


class TestSubagentEventsDontLeakIntoTheMainStream:
    """_run_graph_stream (app/agent/runtime/stream.py) threads this turn's own
    callbacks/metadata into a subagent's NESTED graph.invoke() (see
    app/agent/tools/builtin.py::run_subagent_impl's `nested_config`) so its
    internal LLM/tool calls trace correctly — but that nested graph is
    built via this exact same build_graph(), so it ALSO has a node named
    "agent". Without the `metadata.subagent_name` guard added alongside
    that threading, the nested run's own reasoning tokens would satisfy
    the existing `langgraph_node == "agent"` check too and leak into the
    client's main answer stream, indistinguishable from the real answer."""

    async def test_nested_reasoning_tokens_never_appear_in_the_token_stream(self):
        events = await _stream_events(
            [
                _chat_stream_event(
                    "Nested subagent reasoning that must never leak.",
                    subagent_name="researcher",
                ),
                _chat_stream_event("Delegation complete, here is the final answer."),
            ],
            final_messages=[AIMessage(content="Delegation complete, here is the final answer.")],
        )

        token_events = [e for e in events if e["type"] == "token"]
        streamed_text = "".join(e["content"] for e in token_events)
        assert streamed_text == "Delegation complete, here is the final answer."
        assert "Nested subagent reasoning" not in streamed_text

    async def test_the_subagents_own_tool_activity_is_surfaced_and_tagged(self):
        """The other half of the same fix: unlike raw reasoning tokens,
        the subagent's own internal tool_start/tool_end SHOULD reach the
        client (instead of a silent ~45s black box for the whole
        delegation) — tagged with which subagent they came from, distinct
        from the top-level run_subagent call itself, which carries no tag."""
        events = await _stream_events(
            [
                _tool_event("on_tool_start", "run_subagent"),
                _tool_event("on_tool_start", "calculator", subagent_name="researcher"),
                _tool_event("on_tool_end", "calculator", subagent_name="researcher"),
                _chat_stream_event("Delegation complete, here is the final answer."),
                _tool_event("on_tool_end", "run_subagent"),
            ],
            final_messages=[AIMessage(content="Delegation complete, here is the final answer.")],
        )

        tool_starts = [e for e in events if e["type"] == "tool_start"]
        top_level_call = next(e for e in tool_starts if e["tool"] == "run_subagent")
        assert "subagent" not in top_level_call

        nested_start = next(e for e in tool_starts if e["tool"] == "calculator")
        assert nested_start["subagent"] == "researcher"

        nested_end = next(
            e for e in events if e["type"] == "tool_end" and e["tool"] == "calculator"
        )
        assert nested_end["subagent"] == "researcher"

        top_level_end = next(
            e for e in events if e["type"] == "tool_end" and e["tool"] == "run_subagent"
        )
        assert "subagent" not in top_level_end


def test_a_semantic_cache_hit_is_counted_as_a_success_not_a_rejection():
    """A cache hit never reaches `agent`, so `iterations` stays 0, which `_turn_outcome` read as
    "the turn never got past validation"."""
    assert stream_module._turn_outcome({"cache_hit": True, "iterations": 0}) == "success"
    assert stream_module._turn_outcome({"cache_hit": False, "iterations": 0}) == "rejected"


class _ScriptedEventsGraph:
    """Just enough graph for `_run_graph_stream`: replays crafted `astream_events` events, then
    reports a finished turn whose last message is `final_text`."""

    def __init__(self, events, final_text="The parent's own answer."):
        self._events, self._final_text = events, final_text

    async def astream_events(self, graph_input, config=None, version=None):
        for event in self._events:
            yield event

    async def aget_state(self, cfg):
        values = {"messages": [AIMessage(content=self._final_text)], "iterations": 1}
        return SimpleNamespace(next=(), tasks=(), values=values)


def _chain_end(name, *, subagent=None, output=None):
    return {
        "event": "on_chain_end",
        "name": name,
        "metadata": {"subagent_name": subagent} if subagent else {},
        "data": {"output": output or {"messages": [AIMessage(content="replacement text")]}},
    }


async def _event_types(events):
    graph = _ScriptedEventsGraph(events)
    return [e["type"] async for e in stream_module._run_graph_stream(graph, {}, {}, None)]


async def test_a_nested_subagents_chain_ends_never_reach_the_parents_stream():
    """The nested graph shares the node names below with the parent's, and its events arrive on
    the parent's stream: acting on them cleared the parent's answer and replaced it."""
    for name in ("retry_output", "check_output", "retry_exhausted", "compact_history"):
        assert await _event_types([_chain_end(name, subagent="researcher")]) == ["token", "done"]


async def test_the_same_chain_ends_still_act_when_they_are_the_parents_own():
    """The control for the test above: it proves the guard is on `subagent_name`, not that these
    branches stopped working."""
    assert await _event_types([_chain_end("retry_output")]) == ["retry", "token", "done"]
    assert await _event_types([_chain_end("check_output")]) == ["retry", "token", "done"]
    assert await _event_types([_chain_end("retry_exhausted")]) == ["retry", "token", "done"]
    assert await _event_types([_chain_end("compact_history")]) == ["compacted", "token", "done"]
