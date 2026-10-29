"""What happens to a turn a worker left mid-flight when the graph's edges change under it. LangGraph names the
channel triggering a node after the edge feeding it, and a conditional edge's channel carries the router
function's name. A checkpoint records which channels changed; rename a router (as #171 did,
`route_after_check` -> `route_after_check_bound`) or remove a node and a checkpoint written under the old name
no longer shows the pending node: `next` is empty and a resume runs nothing, silently. A turn PAUSED at
`human_approval` is unaffected while that node's edges don't change, which is why those are covered by
`STATE_SCHEMA_VERSION` and these are not.

The first test pins that LangGraph behaviour on a small graph, so an upgrade that changes it is noticed. The
rest pin what the app does: the worker dead-letters such a turn instead of "continuing" it, and the stream
reports an error instead of streaming the person's own question back as the answer. Deploying a graph change:
drain the workers first (GRAPH_PATTERNS.md pattern 16).
"""
import uuid
from typing import Annotated, TypedDict

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from app.agent.graph.build import GraphDeps
from app.agent.runtime import stream as stream_module
from app.core import metrics
from app.job_queue import agent_worker
from tests.agent.fakes import cfg
from tests.conftest import metric_value
from tests.ecorp import build_graph


class _State(TypedDict):
    messages: Annotated[list, add_messages]


def _graph(router_name: str, crash: dict, saver, ran: list | None = None):
    """a -> (router) -> b -> c. `b` is where a worker dies when `crash["on"]`; `ran` records the nodes that ran."""
    ran = ran if ran is not None else []
    g = StateGraph(_State)
    g.add_node("a", lambda s: {})

    def b(s):
        if crash["on"]:
            raise RuntimeError("the worker died here")
        ran.append("b")
        return {}

    def c(s):
        ran.append("c")
        return {}

    g.add_node("b", b)
    g.add_node("c", c)
    g.add_edge(START, "a")

    def router(s):
        return "b"

    router.__name__ = router_name
    g.add_conditional_edges("a", router, {"b": "b"})
    g.add_edge("b", "c")
    g.add_edge("c", END)
    return g.compile(checkpointer=saver)


async def _crashed_mid_flight(saver, thread):
    """A turn the OLD build started and a worker died partway through: pending node `b`."""
    config = {"configurable": {"thread_id": thread}}
    crash = {"on": True}
    old = _graph("route_old", crash, saver)
    with pytest.raises(RuntimeError, match="died"):
        await old.ainvoke({"messages": [HumanMessage(content="what is my balance", id="q")]}, config)
    assert (await old.aget_state(config)).next == ("b",)
    return config, crash


@pytest.mark.parametrize(
    ("router_name", "pending", "ran_after_resume"),
    [("route_old", ("b",), ["b", "c"]), ("route_new", (), [])],
    ids=["the router keeps its name", "the router is renamed"],
)
async def test_langgraph_resumes_a_pending_node_under_the_same_router_name_and_runs_nothing_under_a_new_one(
    router_name, pending, ran_after_resume
):
    saver = MemorySaver()
    config, crash = await _crashed_mid_flight(saver, str(uuid.uuid4()))
    crash["on"] = False
    ran: list[str] = []
    new = _graph(router_name, crash, saver, ran)

    assert (await new.aget_state(config)).next == pending
    await new.ainvoke(None, config)

    assert ran == ran_after_resume, "a resume must run the rest of the turn, or (renamed) silently run nothing"
    assert (await new.aget_state(config)).next == ()


@pytest.mark.parametrize(("router_name", "expected"), [("route_old", "retry_continue"), ("route_new", "dead_letter")])
async def test_the_worker_continues_a_resumable_turn_and_dead_letters_one_whose_pending_node_is_gone(router_name, expected):
    saver = MemorySaver()
    config, crash = await _crashed_mid_flight(saver, str(uuid.uuid4()))
    crash["on"] = False
    new = _graph(router_name, crash, saver)

    assert await agent_worker._classify_reclaimed_turn(new, config["configurable"]["thread_id"]) == expected


async def test_a_run_that_ends_with_no_answer_reports_an_error_instead_of_streaming_the_question_back():
    g = build_graph(GraphDeps(llm=GenericFakeChatModel(messages=iter([]))))
    config = cfg(str(uuid.uuid4()))
    # A thread whose graph has nothing pending and whose last message is the person's question.
    await g.aupdate_state(config, {"messages": [HumanMessage(content="what is my balance", id="q")]}, as_node="write_semantic_cache")
    assert (await g.aget_state(config)).next == ()
    errors_before = metric_value(metrics.agent_requests_total, outcome="error")

    events = [e async for e in stream_module._run_graph_stream(g, None, config, None)]

    assert [e["type"] for e in events] == ["error"]
    assert events[0]["code"] == "checkpoint_incompatible"
    assert "what is my balance" not in str(events)
    assert metric_value(metrics.agent_requests_total, outcome="error") == errors_before + 1
