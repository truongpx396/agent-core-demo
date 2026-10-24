"""A message with a link, in a domain that cannot open one, carries a reminder behind the question.

Langfuse trace `889b5405`: the page was described, not opened. The same sentence in the system prompt
did nothing when measured (6,000 tokens behind the question, qwen2.5:7b described the page anyway);
as a reminder right after the question the model said it cannot open links. GRAPH_PATTERNS.md pattern 79.
"""
import pytest
from langchain_core.messages import AIMessage, HumanMessage

from app.agent.graph.agent_node import CITATION_REMINDER
from app.agent.graph.build import GraphDeps
from app.agent.graph.links import LINK_REMINDER, cannot_open_links, mentions_link
from tests.agent.fakes import cfg, make_agent_node, scripted_llm
from tests.agent.graph.test_agent_node import _RecordingFakeLLM
from tests.ecorp import build_graph

URL_QUESTION = "run a subAgent to check what this is https://jsonviewer.stack.hu/"


@pytest.mark.parametrize(
    "text",
    ["what is this https://jsonviewer.stack.hu/", "see http://example.com/a?b=1.", "go to www.example.org now", "HTTPS://EXAMPLE.COM"],
)
def test_it_sees_a_link(text):
    assert mentions_link(text)


@pytest.mark.parametrize("text", ["what is a checkpointer?", "the https protocol is secure", "e.g. example.com", ""])
def test_it_does_not_see_a_link_in_ordinary_text(text):
    assert not mentions_link(text)


def test_a_domain_with_a_tool_that_reaches_an_address_can_open_links():
    assert cannot_open_links({"search_docs", "calculator"})
    assert not cannot_open_links({"search_docs", "fetch_external_reference"})


async def test_the_reminder_is_the_last_thing_the_model_reads_after_the_question():
    fake_llm = _RecordingFakeLLM(messages=iter([AIMessage(content="answer")]))
    state = {"messages": [HumanMessage(content=URL_QUESTION)], "context": "[1] Qdrant stores vectors.", "context_anchor_index": 0}

    await make_agent_node(fake_llm, cannot_open_links=True)(state)

    text = fake_llm.seen_messages[0].content
    assert text.endswith(LINK_REMINDER)
    assert text.index(URL_QUESTION) < text.index(CITATION_REMINDER) < text.index(LINK_REMINDER)


async def test_it_is_there_with_no_retrieved_context_too():
    fake_llm = _RecordingFakeLLM(messages=iter([AIMessage(content="answer")]))

    await make_agent_node(fake_llm, cannot_open_links=True)({"messages": [HumanMessage(content=URL_QUESTION)]})

    assert fake_llm.seen_messages[0].content == f"{URL_QUESTION}\n\n{LINK_REMINDER}"


async def test_no_link_no_reminder():
    fake_llm = _RecordingFakeLLM(messages=iter([AIMessage(content="answer")]))

    await make_agent_node(fake_llm, cannot_open_links=True)({"messages": [HumanMessage(content="what is a checkpointer?")]})

    assert fake_llm.seen_messages[0].content == "what is a checkpointer?"


async def test_a_domain_that_can_open_links_is_never_told_it_cannot():
    fake_llm = _RecordingFakeLLM(messages=iter([AIMessage(content="answer")]))

    await make_agent_node(fake_llm, cannot_open_links=False)({"messages": [HumanMessage(content=URL_QUESTION)]})

    assert fake_llm.seen_messages[0].content == URL_QUESTION


async def test_an_earlier_turns_link_does_not_trigger_it():
    """Only the question being answered counts, not a link somewhere in the thread."""
    fake_llm = _RecordingFakeLLM(messages=iter([AIMessage(content="answer")]))
    state = {
        "messages": [
            HumanMessage(content=URL_QUESTION, id="h1"),
            AIMessage(content="I cannot open links.", id="a1"),
            HumanMessage(content="what is a checkpointer?", id="h2"),
        ]
    }

    await make_agent_node(fake_llm, cannot_open_links=True)(state)

    assert fake_llm.seen_messages[0].content == URL_QUESTION
    assert fake_llm.seen_messages[2].content == "what is a checkpointer?"


async def test_the_ecorp_graph_wires_it_end_to_end():
    seen: list = []
    graph = build_graph(GraphDeps(llm=scripted_llm(lambda _m: AIMessage(content="I cannot open links, so I cannot say what that page is."), seen)))

    await graph.ainvoke({"messages": [HumanMessage(content=URL_QUESTION)]}, config=cfg("link-thread"))

    question = next(m for m in seen[0] if isinstance(m, HumanMessage))
    assert question.content.endswith(LINK_REMINDER)
