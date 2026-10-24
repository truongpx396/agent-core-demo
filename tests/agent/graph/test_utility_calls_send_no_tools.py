"""The follow-up suggestions and the history summary are one prompt in, plain text out. They used to
be sent through the chat client with the domain's tool schemas still bound (Langfuse trace `475b073a`:
a one-sentence follow-up prompt cost 2,869 prompt tokens, 15 schemas' worth, and its prefix shared
nothing with the chat calls', so the server read it cold: 15 of that turn's 40 seconds, after the
answer was complete). GRAPH_PATTERNS.md pattern 79."""
import uuid

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableBinding
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI

from app.agent.graph.compaction import make_compact_history_node
from app.agent.graph.followups import make_suggest_followups_node
from app.agent.graph.utils import make_llm, without_tools
from tests.agent.fakes import cfg, scripted_llm


@tool
def ping(host: str) -> str:
    """Ping a host."""
    return host


def _bound_fake(respond):
    """A scripted chat model with a tool schema bound the way `make_llm` binds one, and the keywords
    each call reached the model with."""
    kwargs_seen: list[dict] = []
    fake = scripted_llm(respond, None, kwargs_seen)
    return fake.bind(tools=[{"type": "function", "function": {"name": "ping", "parameters": {}}}]), kwargs_seen


async def test_the_followup_call_carries_no_tool_schemas():
    llm, kwargs_seen = _bound_fake(lambda _m: AIMessage(content="What else?\nAnd then?"))
    state = {"messages": [AIMessage(content="Support is open nine to five [1].")], "used_citations": [{"marker": "[1]"}]}

    result = await make_suggest_followups_node(llm)(state, cfg(str(uuid.uuid4())))

    assert result["followups"] == ["What else?", "And then?"]
    assert len(kwargs_seen) == 1 and "tools" not in kwargs_seen[0]


async def test_the_chat_call_through_the_same_client_still_does():
    """The control: the client really did carry the schema, so the test above is not vacuous."""
    llm, kwargs_seen = _bound_fake(lambda _m: AIMessage(content="hello"))

    await llm.ainvoke([HumanMessage(content="hi")])

    assert "tools" in kwargs_seen[0]


async def test_the_history_summary_call_carries_no_tool_schemas():
    llm, kwargs_seen = _bound_fake(lambda _m: AIMessage(content="A SUMMARY"))
    history = []
    for i in range(6):
        history.append(HumanMessage(content=f"an earlier question number {i}, " + "with plenty of words " * 8, id=f"h{i}"))
        history.append(AIMessage(content=f"an earlier answer number {i}, " + "with plenty of words " * 8, id=f"a{i}"))
    node = make_compact_history_node(llm, ceiling=300, floor=100)

    result = await node({"messages": history}, cfg(str(uuid.uuid4())))

    assert result["history_summary"] == "A SUMMARY"
    assert len(kwargs_seen) == 1 and "tools" not in kwargs_seen[0]


def test_without_tools_takes_the_real_clients_tools_off_the_request():
    """Against the production client, not a fake: the request the SDK would send."""
    llm = make_llm([ping], base_url="http://gateway.invalid", api_key="k", strict=False)
    plain = without_tools(llm)

    chat_payload = llm.bound._get_request_payload([HumanMessage(content="hi")], **llm.kwargs)
    plain_payload = plain._get_request_payload([HumanMessage(content="hi")])

    assert [t["function"]["name"] for t in chat_payload["tools"]] == ["ping"]
    assert isinstance(plain, ChatOpenAI) and not isinstance(plain, RunnableBinding)
    assert "tools" not in plain_payload


def test_without_tools_keeps_a_binding_that_is_not_about_tools():
    llm = make_llm([ping], base_url="http://gateway.invalid", api_key="k", strict=False).bind(stop=["END"])

    plain = without_tools(llm)

    assert plain.kwargs == {"stop": ["END"]}


def test_a_client_that_is_not_a_binding_is_returned_as_it_is():
    fake = scripted_llm(lambda _m: AIMessage(content="x"))
    assert without_tools(fake) is fake
