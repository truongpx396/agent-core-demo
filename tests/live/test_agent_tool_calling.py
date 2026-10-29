"""Fast per-PR smoke checks that this app's REAL graph code (app/agent/graph/core.py) correctly drives a REAL
model's native tool-calling: the one thing every other test here (`GenericFakeChatModel`, hand-scripted
responses) can't prove, because a fake model's tool calls are authored by the test, not reasoned.

It is the fast, cheap sibling of the release gate, not a replacement. `scripts/eval.py` runs its own, harder
golden cases `EVAL_REPETITIONS` times each against the full-size `CHAT_MODEL` the deployment ships, gated by a
statistical pass threshold, and is kept OUT of CI on purpose (see .github/workflows/ci.yml's comment) as
maintainer-run pre-release scanning. This file runs ONE repetition of the simplest case against a deliberately
SMALL model, in CI, on every push: a smoke test that the model/graph integration hasn't broken, not a quality
gate.

(`scripts/eval.py`'s `calculator_basic`, the golden case a comment below used to cite, was retired 2026-09-16
into tests/deepeval/test_tool_correctness_deepeval.py's
`test_calculator_tool_call_is_correct_and_well_argued`. This file's role is unaffected: it tests what that
case never did, a SMALL CI-speed model rather than `scripts/eval.py`'s production one.)

`build_graph()` is called directly, with no `app.agent.runtime.core` wrapper and no Postgres/Redis/Qdrant
(only `ollama_endpoint`, the cheaper of tests/live/conftest.py's fixtures): the "assert the real dependency,
mock everything orthogonal" scoping tests/agent/runtime/test_durable_checkpoint.py uses for Postgres.
Retrieval and the semantic cache are already mocked suite-wide by tests/conftest.py's autouse fixtures, and
neither scenario below calls search_docs anyway.
"""
import uuid

import pytest
from langchain_core.messages import HumanMessage

from tests.conftest import TEST_CTX
from tests.ecorp import build_graph
from tests.ollama import ollama_deps

pytestmark = pytest.mark.llm


async def _invoke_async(endpoint: dict, text: str) -> dict:
    graph = build_graph(ollama_deps(endpoint))
    config = {"configurable": {"thread_id": str(uuid.uuid4()), "ctx": TEST_CTX}}
    return await graph.ainvoke({"messages": [HumanMessage(content=text)]}, config=config)


async def _invoke(endpoint: dict, text: str) -> dict:
    # asyncio.run(...), not the sync .invoke() this used to be: the graph's
    # agent/retrieve_context/etc. nodes are async def now (real LLM/Redis/
    # Qdrant I/O — see app/agent/graph/core.py), and LangGraph's sync Pregel loop
    # can't run an async-only node at all.
    return await _invoke_async(endpoint, text)


async def test_real_model_uses_the_calculator_tool_and_returns_the_right_answer(ollama_endpoint):
    """Used to mirror scripts/eval.py's GOLDEN_CASES `calculator_basic`
    case, retired from there 2026-09-16 (see this file's own module
    docstring) — this smoke test stays regardless, since it's testing a
    different thing (the CI-speed model, on every push) than that golden
    case ever was."""
    from langchain_core.messages import AIMessage

    result = await _invoke(ollama_endpoint, "what is 21 * 2? Use the calculator tool.")

    tool_calls_made = {
        tc["name"]
        for m in result["messages"]
        if isinstance(m, AIMessage)
        for tc in (m.tool_calls or [])
    }
    answer = result["messages"][-1].content

    assert "calculator" in tool_calls_made, f"expected a real calculator tool call; got: {tool_calls_made}"
    assert "42" in answer, f"expected '42' in the final answer; got: {answer!r}"


async def test_real_model_answers_a_plain_question_without_a_tool_call(ollama_endpoint):
    """Mirrors scripts/eval.py's GOLDEN_CASES `general_knowledge_no_tool_needed`
    case — proves the real model doesn't reach for a tool it doesn't need,
    not just that it CAN call one when it does."""
    result = await _invoke(ollama_endpoint, "In one sentence, what is the capital of France?")

    answer = result["messages"][-1].content

    assert "paris" in answer.lower(), f"expected 'Paris' in the final answer; got: {answer!r}"
