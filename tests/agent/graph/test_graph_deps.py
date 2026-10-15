"""The injection points on `GraphDeps` (`llm_factory`, `interrupt`, `model_alias`) and on
`make_llm`: what tests used to reach by monkeypatching `CHAT_MODEL`, `OPENAI_API_BASE` or
`interrupt` onto a graph module. Each is now an argument, so these tests need no patching."""
import functools

from langchain_core.messages import AIMessage, HumanMessage

from app.agent.graph.build import GraphDeps
from app.agent.graph.utils import make_llm
from tests.agent.fakes import call, cfg, queue_llm
from tests.ecorp import ECORP_TOOL_NAMES, build_graph
from tests.ollama import ollama_deps


def test_make_llm_binds_the_tools_to_the_endpoint_it_is_given():
    bound = make_llm([], model="some-model", base_url="http://llm.example:4000/v1", api_key="k")
    client = bound.bound  # type: ignore[attr-defined]  # RunnableBinding around the ChatOpenAI
    assert client.model_name == "some-model"
    assert client.openai_api_base == "http://llm.example:4000/v1"


def test_ollama_deps_points_the_factory_and_the_pricing_alias_at_the_endpoint():
    deps = ollama_deps({"model": "m", "openai_api_base": "http://ollama:11434/v1"}, search_docs=None)
    assert deps.model_alias == "m"
    assert isinstance(deps.llm_factory, functools.partial)
    assert deps.llm_factory.keywords == {"model": "m", "base_url": "http://ollama:11434/v1"}


async def test_llm_factory_is_called_with_the_domains_tools_and_its_client_runs_the_turn():
    offered = []

    def factory(tools):
        offered.append({t.name for t in tools})
        return queue_llm(AIMessage(content="The factory's client answered this turn."))

    g = build_graph(GraphDeps(llm_factory=factory))
    result = await g.ainvoke({"messages": [HumanMessage(content="hello there")]}, config=cfg())

    assert offered == [set(ECORP_TOOL_NAMES)]
    assert result["messages"][-1].content == "The factory's client answered this turn."


async def test_an_explicit_llm_wins_over_the_factory():
    def factory(tools):  # pragma: no cover - must not be called
        raise AssertionError("llm_factory must not be used when llm is given")

    llm = queue_llm(AIMessage(content="The explicit client answered this turn."))
    g = build_graph(GraphDeps(llm=llm, llm_factory=factory))
    result = await g.ainvoke({"messages": [HumanMessage(content="hello there")]}, config=cfg())
    assert result["messages"][-1].content == "The explicit client answered this turn."


async def test_an_injected_interrupt_decides_a_mandatory_approval_without_pausing(monkeypatch):
    """`add_note` is `mutating`, so the mandatory gate sends it to `human_approval`. With a stand-in
    for `interrupt` the node gets its decision immediately, and the graph runs on to the tool."""
    from app.domains.ecorp import store as ecorp_store
    from app.retrieval import qdrant_store

    upserts = []

    async def fake_embed_text(text):
        return [0.0]

    async def fake_upsert(points):
        upserts.append(points)

    monkeypatch.setattr(ecorp_store, "embed_text", fake_embed_text)
    monkeypatch.setattr(qdrant_store, "upsert", fake_upsert)

    payloads = []

    def approve(payload):
        payloads.append(payload)
        return True

    llm = queue_llm(
        call("add_note", {"title": "Refunds", "content": "30 days.", "topic": "company"}, "n1"),
        AIMessage(content="I've added a note about the refund policy."),
    )
    g = build_graph(GraphDeps(llm=llm, interrupt=approve))
    result = await g.ainvoke({"messages": [HumanMessage(content="note the refund policy")]}, config=cfg())

    assert [p["action"] for p in payloads] == ["approve_tool_calls"]
    assert payloads[0]["tool_calls"][0]["name"] == "add_note"
    assert len(upserts) == 1  # the approved tool really ran, in one invocation
    assert result["messages"][-1].content == "I've added a note about the refund policy."
