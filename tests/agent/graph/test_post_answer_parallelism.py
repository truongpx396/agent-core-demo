"""Once an answer stands, the follow-up suggestions and the semantic-cache write run in parallel.

They never depended on each other (the cache write reads the answer and its citations; the follow-ups
only add `followups`), but were wired one after the other, so `done` waited for the follow-up LLM
call and THEN the cache write's embedding and Redis round trip.
"""
import threading

from langchain_core.messages import AIMessage, HumanMessage

from app.agent.graph.build import GraphDeps
from tests.agent.fakes import cfg, scripted_llm
from tests.ecorp import build_graph, build_subagent_graph

WAIT = 3  # seconds a step waits for the other before the test gives up on it overlapping


def _edges(graph):
    return {(e.source, e.target) for e in graph.get_graph().edges}


def test_neither_step_comes_before_the_other():
    edges = _edges(build_graph(GraphDeps(llm=scripted_llm(lambda _m: AIMessage(content="x")))))
    assert ("check_output", "suggest_followups") in edges
    assert ("check_output", "write_semantic_cache") in edges
    assert ("suggest_followups", "__end__") in edges and ("write_semantic_cache", "__end__") in edges
    assert ("suggest_followups", "write_semantic_cache") not in edges


def test_a_graph_without_the_optional_steps_ends_right_after_check_output():
    edges = _edges(build_subagent_graph(GraphDeps(llm=scripted_llm(lambda _m: AIMessage(content="x")))))
    assert ("check_output", "__end__") in edges
    assert not {s for s, _ in edges} & {"suggest_followups", "write_semantic_cache"}


async def test_the_cache_write_begins_while_the_follow_ups_are_still_being_generated():
    followups_in_flight, cache_started, followups_done = (threading.Event() for _ in range(3))
    overlapped: list[bool] = []

    def respond(messages):
        if str(messages[-1].content).startswith("Based on the answer below"):  # the follow-ups call
            followups_in_flight.set()
            cache_started.wait(WAIT)  # held here (in a worker thread) until the cache write begins
            followups_done.set()
            return AIMessage(content="What else?\nAnd more?")
        return AIMessage(content="The refund policy text says what it says [1].")

    async def search(query, ctx):
        return "[1] REFUND POLICY TEXT", [{"marker": "[1]", "text": "REFUND POLICY TEXT"}]

    async def miss(ctx, query):
        return None

    async def cache_set(ctx, query, answer, citations):
        import asyncio

        started_after_followups = await asyncio.to_thread(followups_in_flight.wait, WAIT)
        overlapped.append(started_after_followups and not followups_done.is_set())
        cache_started.set()

    g = build_graph(
        GraphDeps(llm=scripted_llm(respond), search_docs=search, cache_get=miss, cache_set=cache_set)
    )
    result = await g.ainvoke({"messages": [HumanMessage(content="what is the refund policy")]}, config=cfg())

    assert overlapped == [True], "the cache write waited for the follow-ups to finish"
    assert result["followups"] == ["What else?", "And more?"]
