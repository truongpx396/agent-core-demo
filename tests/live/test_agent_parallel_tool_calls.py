"""Smoke test for genuine parallel tool calls against the REAL model/graph —
the scenario app/agent/graph.py's `_make_llm` removed `parallel_tool_calls=
False` for (2026-09-09). Sibling of test_agent_tool_calling.py (same fixture,
same "one repetition against the small CI model" scope), but this one
specifically drives a REAL two-tool-call turn (calculator + add_note) through
the REAL litellm/Ollama streaming path, because that's exactly the path a
real, live-verified bug used to corrupt: two simultaneous tool calls from an
ollama_chat model got glued into one malformed tool_calls entry (litellm's
OllamaChatCompletionResponseIterator.chunk_parser assigning duplicate
"index":0 across separate stream chunks — see litellm-patches/
sitecustomize.py and graph_utils.py's _make_llm comment for the full writeup). A
fake-model test (tests/agent/test_graph_integration.py's
TestMandatoryCapabilityGate) already covers should_continue/human_approval's
OWN batch-handling logic in isolation; this proves the real model's real
streamed output survives the round trip into that logic at all.

calculator (read_only) + add_note (mutating) is a deliberate pair: mixing
capabilities forces should_continue's mandatory gate (_mandatory_gate_reason)
to route the WHOLE batch to human_approval, exercising the exact "one
approve/reject decision covers every call in the batch" behavior the removal
comment documents as a deliberate trade-off, not just "the model can emit two
tool_calls."
"""
import uuid

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.types import Command

from app.agent import graph as graph_module
from app.agent.graph import GraphDeps
from app.agent.graph_build import build_graph
from tests.conftest import TEST_CTX

pytestmark = pytest.mark.llm


@pytest.fixture(autouse=True)
def real_ollama_chat_model(monkeypatch, ollama_endpoint):
    """Same wiring as test_agent_tool_calling.py's fixture of the same
    name — see that file's docstring for why this patches graph_module's
    OWN bindings rather than app.core.config's."""
    monkeypatch.setattr(graph_module, "CHAT_MODEL", ollama_endpoint["model"])
    monkeypatch.setattr(graph_module, "OPENAI_API_BASE", ollama_endpoint["openai_api_base"])


@pytest.fixture(autouse=True)
def _stub_add_note_io(monkeypatch):
    """add_note's own I/O (embedding + Qdrant upsert) isn't what this test
    is about — stub it the same way tests/agent/test_graph_integration.py's
    TestMandatoryCapabilityGate does, so a real APPROVED add_note call can
    actually run inside ToolNode without needing a live Qdrant."""
    from app.agent import tools as tools_module
    from app.retrieval import qdrant_store

    monkeypatch.setattr(tools_module, "embed_text", lambda text: [0.0])
    monkeypatch.setattr(qdrant_store, "upsert", lambda points: None)


def _invoke_and_approve(text: str):
    graph = build_graph(GraphDeps())
    config = {"configurable": {"thread_id": str(uuid.uuid4()), "ctx": TEST_CTX}}
    graph.invoke({"messages": [HumanMessage(content=text)]}, config=config)
    paused_state = graph.get_state(config)
    result = graph.invoke(Command(resume=True), config=config)
    return graph, config, paused_state, result


def test_real_model_calls_two_tools_in_one_turn_and_both_run_after_approval():
    graph, config, paused_state, result = _invoke_and_approve(
        "What is 15 * 3? Use the calculator tool for that. Also use the "
        "add_note tool right now to save a note with title 'Demo', topic "
        "'company', content 'multi-tool-call test'. Call BOTH tools in "
        "this same response, do not wait or ask first."
    )

    # Paused at human_approval with BOTH calls bundled into one pending batch.
    assert paused_state.next == ("human_approval",), (
        f"expected a pause at human_approval for a batch containing a "
        f"mutating call; graph was at {paused_state.next!r}"
    )
    first_ai = next(m for m in paused_state.values["messages"] if isinstance(m, AIMessage) and m.tool_calls)
    tool_names_requested = {tc["name"] for tc in first_ai.tool_calls}
    assert tool_names_requested == {"calculator", "add_note"}, (
        f"expected exactly one real calculator call and one real add_note "
        f"call, not a corrupted/merged batch; got: {first_ai.tool_calls!r}"
    )

    # Approving the batch must run BOTH tools — not just one, not a glued
    # third "tool" that doesn't exist.
    tool_messages = [m for m in result["messages"] if isinstance(m, ToolMessage)]
    tool_ids_called = {tc["id"] for tc in first_ai.tool_calls}
    tool_ids_answered = {tm.tool_call_id for tm in tool_messages}
    assert tool_ids_called <= tool_ids_answered, (
        "every tool_call from the approved batch needs a matching "
        "ToolMessage, or the next LLM call would fail validation"
    )
    calculator_result = next(
        tm.content for tm in tool_messages
        if tm.tool_call_id == next(tc["id"] for tc in first_ai.tool_calls if tc["name"] == "calculator")
    )
    assert "45" in calculator_result
    assert not graph.get_state(config).next, "turn must finish cleanly, not leave a second pause dangling"
