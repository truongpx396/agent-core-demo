"""`ensure_seeded_async` keeps exactly one system prompt per thread, and it is the CURRENT one.

Real bug: it only asked "is a SystemMessage with the current text already here?" and appended
one if not, so every prompt edit left the old prompt in every existing thread and added the new
one mid-conversation (`System(v1), Human, AI, System(v2), Human, AI`). `_messages_to_trim` never
trims a SystemMessage, so both stayed for the thread's life (GRAPH_PATTERNS.md pattern 19).
"""
import uuid

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.checkpoint.memory import MemorySaver

from app.agent.graph.build import GraphDeps
from app.agent.manifest import AgentManifest
from app.agent.runtime import core as runtime_module
from tests.agent.fakes import cfg
from tests.ecorp import build_graph


async def _seed_and_ask(saver, thread, prompt, question):
    g = build_graph(
        GraphDeps(llm=GenericFakeChatModel(messages=iter([AIMessage(content="A sufficiently long answer here.")]))),
        checkpointer=saver,
        manifest=AgentManifest("x", prompt),
    )
    runtime_module._seeded.clear()  # a fresh process after a deploy
    await runtime_module.ensure_seeded_async(g, thread)
    await g.ainvoke({"messages": [HumanMessage(content=question)]}, config=cfg(thread))
    return g


async def test_editing_the_system_prompt_replaces_it_in_an_existing_thread():
    saver, thread = MemorySaver(), str(uuid.uuid4())
    await _seed_and_ask(saver, thread, "You are v1 of the prompt.", "first question here")
    g = await _seed_and_ask(saver, thread, "You are v2 of the prompt.", "second question here")
    messages = (await g.aget_state(cfg(thread))).values["messages"]
    assert [m.content for m in messages if isinstance(m, SystemMessage)] == ["You are v2 of the prompt."]
    assert isinstance(messages[0], SystemMessage)


async def test_a_thread_that_already_has_a_duplicated_prompt_is_healed():
    saver, thread = MemorySaver(), str(uuid.uuid4())
    g = await _seed_and_ask(saver, thread, "You are v1 of the prompt.", "first question here")
    # What the old seeding left behind after a prompt edit: a second prompt mid-thread.
    await g.aupdate_state(cfg(thread), {"messages": [SystemMessage(content="You are v2 of the prompt.")]})
    g = await _seed_and_ask(saver, thread, "You are v2 of the prompt.", "second question here")
    messages = (await g.aget_state(cfg(thread))).values["messages"]
    assert [m.content for m in messages if isinstance(m, SystemMessage)] == ["You are v2 of the prompt."]
    assert isinstance(messages[0], SystemMessage)


async def test_an_unchanged_prompt_writes_nothing_to_the_thread(monkeypatch):
    saver, thread = MemorySaver(), str(uuid.uuid4())
    g = await _seed_and_ask(saver, thread, "You are v1 of the prompt.", "first question here")
    updates = []
    real_aupdate_state = g.aupdate_state

    async def counting_aupdate_state(*args, **kwargs):
        updates.append(args)
        return await real_aupdate_state(*args, **kwargs)

    monkeypatch.setattr(g, "aupdate_state", counting_aupdate_state)
    runtime_module._seeded.clear()
    await runtime_module.ensure_seeded_async(g, thread)
    assert updates == []
