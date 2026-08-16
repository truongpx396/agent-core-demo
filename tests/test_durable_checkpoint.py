"""Tests for the durable checkpointer machinery in app/agent.py
(init_graph_sync/init_graph_async) and app/graph.py's resumability_error.

Everything here uses a real AsyncSqliteSaver against a tmp_path file — no
network, no Qdrant/LiteLLM — so it stays inside this suite's "no live
services" contract while actually exercising the real dependency (the
aiosqlite/langgraph-checkpoint-sqlite version pairing this needed pinning
for — see requirements.txt — is exactly the kind of thing that breaks
silently without a real test).

`app.agent._graph`/`_checkpointer_cm` are module-level singletons; the
`reset_agent_singleton` fixture below resets them before and after every
test in this file so tests don't leak state into each other (or into other
test modules, which never touch app.agent and would otherwise be affected
by whichever checkpointer happened to win the singleton race).
"""
import asyncio
import uuid

import pytest
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from app import agent as agent_module
from app import metrics
from app.graph import STATE_SCHEMA_VERSION, GraphDeps, build_graph, resumability_error


def _count(counter, **labels):
    target = counter.labels(**labels) if labels else counter
    return target._value.get()


@pytest.fixture(autouse=True)
def reset_agent_singleton(monkeypatch, tmp_path):
    monkeypatch.setattr(agent_module, "CHECKPOINT_DB_PATH", str(tmp_path / "cp.sqlite3"))
    agent_module._graph = None
    agent_module._checkpointer_cm = None
    yield
    agent_module._graph = None
    agent_module._checkpointer_cm = None


def _fake_llm():
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage

    return GenericFakeChatModel(
        messages=iter([AIMessage(content="A sufficiently long final answer.")])
    )


class TestInitGraphSync:
    def test_builds_a_working_graph(self):
        agent_module.init_graph_sync()
        graph = agent_module.get_graph()
        config = {"configurable": {"thread_id": str(uuid.uuid4())}}
        # The singleton graph binds the real ChatOpenAI client — just prove
        # it's a real compiled graph with a durable (non-Memory) checkpointer
        # wired in, rather than driving a live LLM call here.
        from langgraph.checkpoint.memory import MemorySaver

        assert not isinstance(graph.checkpointer, MemorySaver)

    def test_second_call_is_a_noop_reusing_the_singleton(self):
        agent_module.init_graph_sync()
        first = agent_module.get_graph()
        agent_module.init_graph_sync()
        assert agent_module.get_graph() is first

    def test_get_graph_falls_back_to_sync_init(self):
        assert agent_module._graph is None
        graph = agent_module.get_graph()
        assert graph is not None
        assert agent_module._graph is graph

    def test_checkpointer_open_failure_propagates_and_leaves_singleton_unset(
        self, monkeypatch
    ):
        """Regression test: the background thread must not fall through to
        loop.run_forever() on a failed checkpointer open — that would leave
        an orphaned thread running forever for a loop nothing will ever use
        (a real bug caught while writing this)."""

        async def failing_open():
            raise RuntimeError("disk full")

        monkeypatch.setattr(agent_module, "_open_checkpointer", failing_open)

        with pytest.raises(RuntimeError, match="disk full"):
            agent_module.init_graph_sync()

        assert agent_module._graph is None


class TestInitGraphAsync:
    def test_builds_a_working_graph(self):
        async def _check():
            graph = await agent_module.init_graph_async()
            from langgraph.checkpoint.memory import MemorySaver

            assert not isinstance(graph.checkpointer, MemorySaver)

        asyncio.run(_check())

    def test_second_call_is_a_noop_reusing_the_singleton(self):
        async def _check():
            first = await agent_module.init_graph_async()
            second = await agent_module.init_graph_async()
            assert second is first

        asyncio.run(_check())


class TestDurabilityAcrossRestart:
    """The whole point of swapping MemorySaver for AsyncSqliteSaver: state
    written by one checkpointer instance must be readable by a completely
    separate instance pointed at the same file — simulating a process
    restart without actually restarting a process.

    Deliberately bypasses app.agent's singleton/threading machinery (that's
    what TestInitGraphSync/TestInitGraphAsync above already prove works) and
    talks to build_graph()+AsyncSqliteSaver directly with a fake LLM, each
    "instance" on its own short-lived asyncio.run() loop — the cleanest way
    to actually simulate two separate processes sharing one file.
    """

    def test_checkpoint_survives_a_fresh_instance_at_the_same_path(self, tmp_path):
        db_path = str(tmp_path / "durable.sqlite3")
        thread_id = str(uuid.uuid4())

        async def _write():
            async with AsyncSqliteSaver.from_conn_string(db_path) as saver:
                await saver.setup()
                graph = build_graph(GraphDeps(llm=_fake_llm()), checkpointer=saver)
                await graph.ainvoke(
                    {"messages": [HumanMessage(content="remember this")]},
                    config={"configurable": {"thread_id": thread_id}},
                )

        async def _read():
            async with AsyncSqliteSaver.from_conn_string(db_path) as saver:
                graph = build_graph(GraphDeps(llm=_fake_llm()), checkpointer=saver)
                state = await graph.aget_state(
                    {"configurable": {"thread_id": thread_id}}
                )
                return [m.content for m in state.values["messages"]]

        asyncio.run(_write())
        contents = asyncio.run(_read())
        assert "remember this" in contents


class TestResumabilityError:
    def _paused_graph_and_config(self, monkeypatch):
        """A graph paused at human_approval (require_approval=True, a
        tool call pending) — the real "waiting for a human" state
        resumability_error is meant to guard."""
        from langchain_core.messages import AIMessage
        from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

        llm = GenericFakeChatModel(
            messages=iter(
                [
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "calculator",
                                "args": {"expression": "1+1"},
                                "id": "c1",
                            }
                        ],
                    )
                ]
            )
        )
        graph = build_graph(GraphDeps(llm=llm))
        config = {"configurable": {"thread_id": str(uuid.uuid4())}}
        graph.invoke(
            {"messages": [HumanMessage(content="hi")], "require_approval": True},
            config=config,
        )
        return graph, config

    def test_no_paused_run_is_checkpoint_lost(self):
        graph = build_graph(GraphDeps(llm=_fake_llm()))
        config = {"configurable": {"thread_id": str(uuid.uuid4())}}
        before = _count(metrics.agent_checkpoint_issue_total, reason="checkpoint_lost")

        # Never invoked — nothing paused, nothing exists for this thread.
        error = resumability_error(graph, config)

        assert error is not None
        assert "checkpoint_lost" in error
        assert (
            _count(metrics.agent_checkpoint_issue_total, reason="checkpoint_lost")
            == before + 1
        )

    def test_matching_schema_version_is_resumable(self, monkeypatch):
        graph, config = self._paused_graph_and_config(monkeypatch)
        assert resumability_error(graph, config) is None

    def test_mismatched_schema_version_is_checkpoint_incompatible(self, monkeypatch):
        graph, config = self._paused_graph_and_config(monkeypatch)
        monkeypatch.setattr("app.graph.STATE_SCHEMA_VERSION", STATE_SCHEMA_VERSION + 1)
        before = _count(
            metrics.agent_checkpoint_issue_total, reason="checkpoint_incompatible"
        )

        error = resumability_error(graph, config)

        assert error is not None
        assert "checkpoint_incompatible" in error
        assert (
            _count(
                metrics.agent_checkpoint_issue_total, reason="checkpoint_incompatible"
            )
            == before + 1
        )

    def test_differing_graph_version_alone_is_not_an_error(self, monkeypatch):
        """graph_version (build SHA) is recorded but never compared — only
        state_schema_version gates resumability (see resumability_error's
        docstring: ordinary deploys change the SHA constantly)."""
        graph, config = self._paused_graph_and_config(monkeypatch)
        state = graph.get_state(config)
        assert state.values.get("graph_version")  # was stamped
        assert resumability_error(graph, config) is None
