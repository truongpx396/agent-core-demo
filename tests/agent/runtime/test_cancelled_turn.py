"""A turn the person stopped is not left in the thread for the next turn to act on (pattern 80).

Langfuse trace `d6a00248`: "run it through a script" was stopped with POST /chat/cancel, then "Check
our API docs page ..." was sent. The model was shown both messages with nothing between them, answered
the older one ("Let's first count how many times ... I'll use a script"), and paused for approval to
run the script the person had just stopped.

Driven the way test_cancellation.py drives a stop: a hermetic graph behind a monkeypatched
`init_graph_async`, but with a real (in-memory) checkpointer, because what is under test is the thread.
"""
import asyncio
import uuid

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import MemorySaver

from app.agent.graph.build import GraphDeps
from app.agent.runtime import cancelled_turn
from app.agent.runtime import core as agent_module
from app.agent.runtime import stream as stream_module
from app.core import metrics
from tests.agent.fakes import cfg, scripted_llm
from tests.conftest import TEST_CTX
from tests.conftest import metric_value as _count
from tests.ecorp import build_graph


class _StoppableLLM(GenericFakeChatModel):
    """Streams its reply slowly and tells the test the moment the call starts, so a stop can land
    while the model is working: by then `validate_input` has put the question in the thread."""

    started: dict = {}

    async def _astream(self, *args, **kwargs):
        self.started["yes"] = True
        async for chunk in super()._astream(*args, **kwargs):
            yield chunk
            await asyncio.sleep(0.05)


def _graph_and_thread(monkeypatch, llm):
    graph = build_graph(GraphDeps(llm=llm), checkpointer=MemorySaver())

    async def fake_init_graph_async():
        return graph

    monkeypatch.setattr(agent_module, "init_graph_async", fake_init_graph_async)
    return graph, str(uuid.uuid4())


async def _run_turn(text, thread, cancel_check=None):
    return [e async for e in stream_module.astream_events_turn(text, thread, TEST_CTX, cancel_check=cancel_check)]


def _human_texts(messages):
    return [m.content for m in messages if isinstance(m, HumanMessage)]


async def test_a_stopped_turn_is_taken_out_of_the_thread_and_the_next_turn_does_not_see_it(monkeypatch):
    seen: list = []
    stoppable = _StoppableLLM(messages=iter([AIMessage(content="a long answer that will never be finished, word by word")]))
    stoppable.started.clear()
    graph, thread = _graph_and_thread(monkeypatch, stoppable)

    async def stop_once_the_model_is_working():
        return bool(stoppable.started)

    before = _count(metrics.agent_cancelled_turn_discarded_total, outcome="dropped")
    events = await _run_turn("run it through a script and count db_timeout", thread, stop_once_the_model_is_working)
    assert events[-1]["code"] == "cancelled"

    state = await graph.aget_state(cfg(thread))
    assert _human_texts(state.values["messages"]) == [], "the stopped question must not stay in the thread"
    assert _count(metrics.agent_cancelled_turn_discarded_total, outcome="dropped") == before + 1

    # The next turn: a different question, answered by a model that records what it was shown.
    answering = scripted_llm(lambda _m: AIMessage(content="The documented rate limit is 100 requests a minute."), seen)
    graph.nodes  # noqa: B018 - the compiled graph is reused; only the model behind the next turn changes
    graph2 = build_graph(GraphDeps(llm=answering), checkpointer=graph.checkpointer)

    async def fake_init_graph_async_2():
        return graph2

    monkeypatch.setattr(agent_module, "init_graph_async", fake_init_graph_async_2)
    events = await _run_turn("What is the documented rate limit?", thread)

    assert events[-1]["type"] == "done"
    shown = [m for m in seen[0] if isinstance(m, HumanMessage)]
    assert [m.content for m in shown] == ["What is the documented rate limit?"], "the model was shown the stopped question"


async def test_the_thread_keeps_the_earlier_turns_and_loses_only_the_stopped_one(monkeypatch):
    answered = scripted_llm(lambda _m: AIMessage(content="Support is open nine to five on weekdays."))
    graph, thread = _graph_and_thread(monkeypatch, answered)
    await _run_turn("When is support open?", thread)

    stoppable = _StoppableLLM(messages=iter([AIMessage(content="a long answer that will never be finished, word by word")]))
    stoppable.started.clear()
    graph2 = build_graph(GraphDeps(llm=stoppable), checkpointer=graph.checkpointer)

    async def init2():
        return graph2

    monkeypatch.setattr(agent_module, "init_graph_async", init2)

    async def stop():
        return bool(stoppable.started)

    await _run_turn("an unrelated second question", thread, stop)

    state = await graph.aget_state(cfg(thread))
    assert _human_texts(state.values["messages"]) == ["When is support open?"]
    assert state.values["messages"][-1].content == "Support is open nine to five on weekdays."


async def test_a_stopped_turn_in_which_a_tool_already_ran_stays_and_is_closed(monkeypatch):
    """The tool's result is a fact the model must keep: a note was added, a ticket was filed. Forget it
    and the person's "do it again" files it twice under a new call id, which nothing can catch."""
    stoppable = _StoppableLLM(messages=iter([AIMessage(content="a long answer that will never be finished, word by word")]))
    stoppable.started.clear()
    graph, thread = _graph_and_thread(monkeypatch, stoppable)
    await graph.aupdate_state(
        cfg(thread),
        {
            "messages": [
                HumanMessage(content="add a note about the outage", id="h1"),
                AIMessage(content="", id="a1", tool_calls=[{"name": "add_note", "args": {}, "id": "c1"}]),
                ToolMessage(content="note saved", tool_call_id="c1", name="add_note", id="t1"),
            ]
        },
        as_node="__start__",
    )

    outcome = await cancelled_turn.discard_cancelled_turn(graph, cfg(thread))

    assert outcome == "closed"
    messages = (await graph.aget_state(cfg(thread))).values["messages"]
    assert [type(m).__name__ for m in messages] == ["HumanMessage", "AIMessage", "ToolMessage", "AIMessage"]
    assert messages[-1].content == cancelled_turn.STOPPED_TURN_MESSAGE


async def test_a_thread_with_no_turn_in_it_is_left_alone(monkeypatch):
    graph, thread = _graph_and_thread(monkeypatch, scripted_llm(lambda _m: AIMessage(content="x")))

    assert await cancelled_turn.discard_cancelled_turn(graph, cfg(thread)) == "nothing"


async def test_a_failure_tidying_the_thread_never_fails_the_stop(monkeypatch):
    class _Broken:
        async def aget_state(self, _cfg):
            raise ConnectionError("checkpointer down")

    before = _count(metrics.agent_cancelled_turn_discarded_total, outcome="failed")

    assert await cancelled_turn.discard_cancelled_turn(_Broken(), cfg("t")) == "failed"
    assert _count(metrics.agent_cancelled_turn_discarded_total, outcome="failed") == before + 1


async def test_only_a_stop_the_person_asked_for_takes_the_question_out(monkeypatch):
    """A turn that fails or times out keeps its question, so "try again" still has something to retry."""

    class _Exploding(GenericFakeChatModel):
        async def _astream(self, *args, **kwargs):
            raise RuntimeError("model blew up")
            yield  # pragma: no cover - makes this an async generator

    graph, thread = _graph_and_thread(monkeypatch, _Exploding(messages=iter([AIMessage(content="x")])))

    events = await _run_turn("a question that will fail", thread)

    assert events[-1]["type"] == "error" and events[-1].get("code") != "cancelled"
    state = await graph.aget_state(cfg(thread))
    assert _human_texts(state.values["messages"]) == ["a question that will fail"]
