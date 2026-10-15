"""A cached answer must never stand in for an action.

`write_semantic_cache` stored the final text of ANY completed turn, including one
that ran an approved write. A later identical or near-identical request from the
same principal then hit the cache and was answered with that text — "Remembered.",
"Ticket #123 created" — with no model call, no approval pause and no write. The
user is told something was done when nothing was, and on a deployment where a
person approves every write the approval is silently bypassed (the cache never
reaches `human_approval`).

Found by accident: a real-model browser test repeated the same prompt in one
session, got the cached answer instead of an approval prompt, and failed on a
button that never appeared. Reproduced here against the real graph with the
read side and the write side of the cache backed by one dict (what the real Redis
cache does for one principal).

What is cached stays as before: an answer from a turn that called no tool, or
only read-only tools, is still cached. Only a turn that called a tool that is not
`read_only` is kept out — and a tool with no declared tier counts as `outward`,
the repo-wide fail-closed default.
"""
import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.types import Command

from app.agent.graph import GraphDeps
from app.agent.graph_build import build_graph
from app.agent.graph_cache import make_write_semantic_cache_node
from app.domains.sales import store
from app.domains.sales.domain import SALES_DOMAIN_PLUGIN, SALES_MANIFEST
from tests.conftest import TEST_CTX

CONTACT = "jordan@example.com"
QUESTION = "Research jordan's company site and add it to their notes."


class _OneDictCache:
    """Read and write sides over the same store, scoped to the principal the way
    the real cache is (tenant + principal)."""

    def __init__(self):
        self.entries: dict[tuple, tuple[str, list]] = {}
        self.writes = 0

    @staticmethod
    def _scope(ctx):
        return (ctx["tenant"], ctx["principal"])

    async def get(self, ctx, query):
        return self.entries.get((*self._scope(ctx), query))

    async def set(self, ctx, query, answer, citations):
        self.writes += 1
        self.entries[(*self._scope(ctx), query)] = (answer, citations)


def _config(thread):
    return {"configurable": {"thread_id": thread, "ctx": TEST_CTX}}


def _enrich_call(call_id):
    return AIMessage(
        content="",
        tool_calls=[
            {"name": "enrich_lead_from_website", "args": {"contact": CONTACT, "url": "https://acme.example.com"}, "id": call_id}
        ],
    )


async def _run_turn(graph, thread):
    """One full turn, approving any pause, returning the final answer text."""
    result = await graph.ainvoke({"messages": [HumanMessage(content=QUESTION)]}, config=_config(thread))
    while (await graph.aget_state(_config(thread))).next:
        result = await graph.ainvoke(Command(resume=True), config=_config(thread))
    return result["messages"][-1].content


async def test_a_repeated_request_runs_the_action_again_instead_of_replaying_the_cached_answer(monkeypatch):
    writes: list[str] = []

    async def fake_get_lead(tenant, contact):
        return {"name": "Jordan", "contact": contact}

    async def fake_append(tenant, contact, note, tool_call_id=None):
        writes.append(tool_call_id)
        return True

    async def fake_render(url):
        return "Acme — mid-market SaaS."

    from app.domains.sales import tools as sales_tools

    monkeypatch.setattr(store, "get_lead", fake_get_lead)
    monkeypatch.setattr(store, "append_lead_note", fake_append)
    monkeypatch.setattr(sales_tools, "render_url_to_markdown", fake_render)

    cache = _OneDictCache()
    llm = GenericFakeChatModel(
        messages=iter(
            [
                _enrich_call("call-1"),
                AIMessage(content="Added research on Jordan's company to their notes."),
                _enrich_call("call-2"),  # the second request: the model must be asked, and must act
                AIMessage(content="Added research on Jordan's company to their notes."),
            ]
        )
    )
    graph = build_graph(
        GraphDeps(llm=llm, cache_get=cache.get, cache_set=cache.set),
        manifest=SALES_MANIFEST,
        domain=SALES_DOMAIN_PLUGIN,
    )

    first = await _run_turn(graph, "thread-1")
    second = await _run_turn(graph, "thread-2")  # a NEW conversation, same principal, same words

    assert first == second  # the same words come back either way...
    assert writes == ["call-1", "call-2"], (
        "...but the second request must have RUN the action (and so been approved again), "
        f"not been answered from the cache; the notes were written for: {writes}"
    )
    assert cache.writes == 0, "a turn that ran an approved write must not be cached at all"


# --- the rule itself, at the node ----------------------------------------------------


def _turn(*messages):
    return {"messages": [HumanMessage(content="do the thing"), *messages], "ctx": TEST_CTX, "used_citations": []}


def _called(name, call_id="c1"):
    return AIMessage(content="", tool_calls=[{"name": name, "args": {}, "id": call_id}])


async def _written_for(state, tool_capabilities=None):
    seen: list = []

    async def cache_set(ctx, query, answer, citations):
        seen.append((query, answer))

    kwargs = {} if tool_capabilities is None else {"tool_capabilities": tool_capabilities}
    node = make_write_semantic_cache_node(cache_set, **kwargs)
    await node(state)
    return seen


_CAPS = {"read_a_thing": "read_only", "change_a_thing": "mutating", "send_a_thing": "outward"}


@pytest.mark.parametrize("tool", ["change_a_thing", "send_a_thing", "an_undeclared_tool"])
async def test_a_turn_that_called_a_tool_that_is_not_read_only_is_not_cached(tool):
    state = _turn(_called(tool), ToolMessage(content="done", tool_call_id="c1"), AIMessage(content="All done."))

    assert await _written_for(state, _CAPS) == []


async def test_a_turn_that_called_only_read_only_tools_is_still_cached():
    state = _turn(_called("read_a_thing"), ToolMessage(content="42", tool_call_id="c1"), AIMessage(content="It is 42."))

    assert await _written_for(state, _CAPS) == [("do the thing", "It is 42.")]


async def test_a_turn_that_called_no_tool_is_still_cached():
    assert await _written_for(_turn(AIMessage(content="A plain answer.")), _CAPS) == [("do the thing", "A plain answer.")]


async def test_one_non_read_only_call_among_read_only_ones_is_enough_to_skip_the_write():
    state = _turn(
        _called("read_a_thing", "c1"),
        ToolMessage(content="42", tool_call_id="c1"),
        _called("change_a_thing", "c2"),
        ToolMessage(content="done", tool_call_id="c2"),
        AIMessage(content="Looked it up and changed it."),
    )

    assert await _written_for(state, _CAPS) == []


async def test_only_the_current_turn_counts_an_earlier_turns_write_does_not_block_a_later_plain_answer():
    state = {
        "messages": [
            HumanMessage(content="earlier request"),
            _called("change_a_thing"),
            ToolMessage(content="done", tool_call_id="c1"),
            AIMessage(content="Changed."),
            HumanMessage(content="what is the refund policy?"),
            AIMessage(content="Refunds take 5 days."),
        ],
        "ctx": TEST_CTX,
        "used_citations": [],
    }

    assert await _written_for(state, _CAPS) == [("what is the refund policy?", "Refunds take 5 days.")]


async def test_a_cache_served_turn_is_still_never_rewritten():
    state = {**_turn(AIMessage(content="cached")), "cache_hit": True}

    assert await _written_for(state, _CAPS) == []
