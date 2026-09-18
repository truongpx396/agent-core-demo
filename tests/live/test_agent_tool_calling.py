"""Fast, per-PR smoke checks that this app's REAL graph code (app/agent/graph.py)
correctly drives a REAL model's REAL native tool-calling — the one thing
every other test in this suite (`GenericFakeChatModel`, hand-scripted
responses) structurally cannot prove, because a fake model's tool calls are
authored by the test, not actually reasoned by anything. Deliberately the
release-gating counterpart's fast, cheap sibling, not a replacement for it:
`scripts/eval.py` runs its own (harder) golden cases `EVAL_REPETITIONS`
times each against the REAL, full-size `CHAT_MODEL` this deployment
actually ships, gated by a statistical pass threshold — deliberately kept
OUT of CI (see .github/workflows/ci.yml's own comment) because that's
real, maintainer-run, pre-release scanning. This file runs ONE repetition
of the simplest case against a deliberately SMALL model, in CI, on every
push — a smoke test that the real model/graph integration hasn't broken,
not a quality gate on how well it answers.

(`scripts/eval.py`'s own `calculator_basic` — the golden case this file's
own comment below used to point at by name — was retired 2026-09-16,
folded into tests/deepeval/test_tool_correctness_deepeval.py's own
`test_calculator_tool_call_is_correct_and_well_argued` instead; this
file's own smoke-test role is unaffected, since it's testing something
that golden case never was: a deliberately SMALL, CI-speed model, not
`scripts/eval.py`'s full-size production one.)

`build_graph()` is called directly — no `app.agent.runtime` wrapper, no
Postgres/Redis/Qdrant needed at all (only `ollama_endpoint`, the cheapest of
tests/live/conftest.py's two fixtures) — the exact same "assert the real
dependency, mock everything orthogonal" scoping
tests/agent/test_durable_checkpoint.py already uses for Postgres. Retrieval/
the semantic cache are already mocked for every test in this whole suite by
tests/conftest.py's autouse fixtures; nothing here needs them anyway, since
neither scenario below calls search_docs.
"""
import uuid

import pytest
from langchain_core.messages import HumanMessage

from app.agent import graph as graph_module
from app.agent.graph import GraphDeps
from app.agent.graph_build import build_graph
from tests.conftest import TEST_CTX
from tests.seeding import seed_thread

pytestmark = pytest.mark.llm


@pytest.fixture(autouse=True)
def real_ollama_chat_model(monkeypatch, ollama_endpoint):
    """Points app/agent/graph_utils.py's OWN `_make_llm` at the real Ollama
    container — `graph.CHAT_MODEL`/`graph.OPENAI_API_BASE` specifically
    (that module's `from app.core.config import CHAT_MODEL, OPENAI_API_BASE`
    bindings, not `app.core.config`'s own — same "a `from X import Y`
    binding is a separate reference" reasoning tests/conftest.py's docstring
    already spells out for `get_connection`), so `build_graph(GraphDeps())`
    below — `deps.llm` left unset — falls through to a REAL, tool-bound
    `ChatOpenAI` exactly the way production does, no hand-duplicated client
    construction here."""
    monkeypatch.setattr(graph_module, "CHAT_MODEL", ollama_endpoint["model"])
    monkeypatch.setattr(graph_module, "OPENAI_API_BASE", ollama_endpoint["openai_api_base"])


async def _invoke_async(text: str) -> dict:
    graph = build_graph(GraphDeps())
    config = {"configurable": {"thread_id": str(uuid.uuid4()), "ctx": TEST_CTX}}
    # Seed the system prompt before the first real turn — see
    # tests/seeding.py's own `seed_thread` docstring for why this is
    # required for a test that calls `build_graph().ainvoke()` directly,
    # bypassing app/agent/runtime.py's own seeding every production path
    # relies on. Real, disclosed finding: this file's own docstring above
    # frames "no app.agent.runtime wrapper" as a deliberate minimal-deps
    # choice, but never accounted for that wrapper also being what seeds
    # the model's tool-routing guidance in real usage.
    await seed_thread(graph, config["configurable"]["thread_id"])
    return await graph.ainvoke({"messages": [HumanMessage(content=text)]}, config=config)


async def _invoke(text: str) -> dict:
    # asyncio.run(...), not the sync .invoke() this used to be: the graph's
    # agent/retrieve_context/etc. nodes are async def now (real LLM/Redis/
    # Qdrant I/O — see app/agent/graph.py), and LangGraph's sync Pregel loop
    # can't run an async-only node at all.
    return await _invoke_async(text)


async def test_real_model_uses_the_calculator_tool_and_returns_the_right_answer():
    """Used to mirror scripts/eval.py's GOLDEN_CASES `calculator_basic`
    case, retired from there 2026-09-16 (see this file's own module
    docstring) — this smoke test stays regardless, since it's testing a
    different thing (the CI-speed model, on every push) than that golden
    case ever was."""
    from langchain_core.messages import AIMessage

    result = await _invoke("what is 21 * 2? Use the calculator tool.")

    tool_calls_made = {
        tc["name"]
        for m in result["messages"]
        if isinstance(m, AIMessage)
        for tc in (m.tool_calls or [])
    }
    answer = result["messages"][-1].content

    assert "calculator" in tool_calls_made, f"expected a real calculator tool call; got: {tool_calls_made}"
    assert "42" in answer, f"expected '42' in the final answer; got: {answer!r}"


async def test_real_model_answers_a_plain_question_without_a_tool_call():
    """Mirrors scripts/eval.py's GOLDEN_CASES `general_knowledge_no_tool_needed`
    case — proves the real model doesn't reach for a tool it doesn't need,
    not just that it CAN call one when it does."""
    result = await _invoke("In one sentence, what is the capital of France?")

    answer = result["messages"][-1].content

    assert "paris" in answer.lower(), f"expected 'Paris' in the final answer; got: {answer!r}"
