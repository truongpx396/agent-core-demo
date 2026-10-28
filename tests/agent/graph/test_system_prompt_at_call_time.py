"""The system prompt is the first message of every model call, taken from the manifest at call time.

It is not stored in the thread. That removes three problems the old per-thread seeding had:
- editing a prompt left the old one in every existing conversation and added the new one after it
  (`System(v1), Human, AI, System(v2), ...`), since trimming never removes a SystemMessage;
- a graph built directly (a test, a script) and never seeded was an UN-GUIDED agent, silently: a
  test suite ran that way for weeks before anyone noticed;
- every first turn of every thread paid an extra checkpoint read and write.
(GRAPH_PATTERNS.md pattern 19.)
"""
import uuid

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.checkpoint.memory import MemorySaver

from app.agent.graph.build import GraphDeps
from app.agent.graph.messages import COMPACTION_MARKER_KEY
from app.agent.manifest import AgentManifest
from tests.agent.fakes import call, cfg, queue_llm, scripted_llm
from tests.ecorp import build_graph

LONG_ANSWER = AIMessage(content="A sufficiently long answer here.")


def _graph(prompt, llm, saver=None, **deps):
    return build_graph(
        GraphDeps(llm=llm, **deps), checkpointer=saver or MemorySaver(), manifest=AgentManifest("x", prompt)
    )


def _system_texts(messages):
    return [m.content for m in messages if isinstance(m, SystemMessage)]


async def test_a_graph_built_directly_is_never_an_unguided_agent():
    seen = []
    g = _graph("You are the domain's prompt.", scripted_llm(lambda _m: LONG_ANSWER, seen))
    await g.ainvoke({"messages": [HumanMessage(content="a question here")]}, config=cfg())
    assert isinstance(seen[0][0], SystemMessage)
    assert seen[0][0].content == "You are the domain's prompt."


async def test_every_call_of_a_multi_call_turn_starts_with_the_prompt():
    seen = []
    llm = queue_llm(
        call("calculator", {"expression": "1+1"}, "c1"),
        AIMessage(content="The answer is two, as computed."),
        seen=seen,
    )
    g = _graph("You are the domain's prompt.", llm)
    await g.ainvoke({"messages": [HumanMessage(content="what is 1+1 please")]}, config=cfg())
    assert len(seen) == 2
    assert [call_[0].content for call_ in seen] == ["You are the domain's prompt."] * 2


async def test_the_prompt_is_not_written_into_the_thread():
    g = _graph("You are the domain's prompt.", scripted_llm(lambda _m: LONG_ANSWER))
    config = cfg()
    await g.ainvoke({"messages": [HumanMessage(content="a question here")]}, config=config)
    stored = (await g.aget_state(config)).values["messages"]
    assert _system_texts(stored) == []


async def test_editing_the_prompt_reaches_an_existing_thread_with_nothing_to_migrate():
    saver, thread = MemorySaver(), str(uuid.uuid4())
    seen = []
    llm = scripted_llm(lambda _m: LONG_ANSWER, seen)
    await _graph("You are v1 of the prompt.", llm, saver).ainvoke(
        {"messages": [HumanMessage(content="first question here")]}, config=cfg(thread)
    )
    g2 = _graph("You are v2 of the prompt.", llm, saver)  # a new process after a deploy
    await g2.ainvoke({"messages": [HumanMessage(content="second question here")]}, config=cfg(thread))

    assert [_system_texts(call_) for call_ in seen] == [["You are v1 of the prompt."], ["You are v2 of the prompt."]]
    assert _system_texts((await g2.aget_state(cfg(thread))).values["messages"]) == []


async def test_a_thread_that_still_holds_prompts_from_an_older_build_is_sent_exactly_one():
    """Threads created before this change hold the seeded prompt as their first message, and
    after a prompt edit under the old seeding, a second one mid-thread. Both are ignored, never
    shown to the model, and the current prompt is the only one it sees."""
    saver, thread = MemorySaver(), str(uuid.uuid4())
    seen = []
    g = _graph("You are the CURRENT prompt.", scripted_llm(lambda _m: LONG_ANSWER, seen), saver)
    await g.aupdate_state(cfg(thread), {"messages": [SystemMessage(content="You are an OLD prompt.")]})
    await g.ainvoke({"messages": [HumanMessage(content="first question here")]}, config=cfg(thread))
    await g.aupdate_state(cfg(thread), {"messages": [SystemMessage(content="You are ANOTHER old prompt.")]})
    await g.ainvoke({"messages": [HumanMessage(content="second question here")]}, config=cfg(thread))

    assert [_system_texts(call_) for call_ in seen] == [["You are the CURRENT prompt."]] * 2


async def test_a_compaction_breadcrumb_is_not_mistaken_for_a_legacy_prompt():
    saver, thread = MemorySaver(), str(uuid.uuid4())
    seen = []
    g = _graph("You are the CURRENT prompt.", scripted_llm(lambda _m: LONG_ANSWER, seen), saver)
    breadcrumb = SystemMessage(
        content="[2 earlier turns summarized to keep this conversation within budget.]",
        additional_kwargs={COMPACTION_MARKER_KEY: True},
    )
    await g.aupdate_state(cfg(thread), {"messages": [breadcrumb]})
    await g.ainvoke({"messages": [HumanMessage(content="a question here")]}, config=cfg(thread))
    assert _system_texts(seen[0]) == ["You are the CURRENT prompt.", breadcrumb.content]


async def test_dropping_a_legacy_prompt_does_not_move_the_context_off_the_question():
    """Retrieved context rides on the turn's question, which is found by an index recorded before the
    call is built. Removing messages ahead of it shifts that index, so it is re-located."""
    saver, thread = MemorySaver(), str(uuid.uuid4())
    seen = []

    async def search(query, ctx):
        return "[1] REFUND POLICY TEXT", [{"marker": "[1]", "text": "REFUND POLICY TEXT"}]

    llm = scripted_llm(lambda _m: AIMessage(content="The refund policy text says so, as cited [1]."), seen)
    g = _graph("You are the CURRENT prompt.", llm, saver, search_docs=search)
    await g.aupdate_state(cfg(thread), {"messages": [SystemMessage(content="You are an OLD prompt.")]})
    await g.ainvoke({"messages": [HumanMessage(content="what is the refund policy")]}, config=cfg(thread))

    call_ = seen[0]
    question = next(m for m in call_ if isinstance(m, HumanMessage))
    assert call_[0].content == "You are the CURRENT prompt."
    assert "REFUND POLICY TEXT" in question.content and question.content.index("REFUND POLICY TEXT") < question.content.index("what is the refund policy")
