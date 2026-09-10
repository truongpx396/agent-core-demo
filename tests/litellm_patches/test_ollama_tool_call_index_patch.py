"""Regression test for litellm-patches/sitecustomize.py, the workaround for
a live-verified litellm bug: OllamaChatCompletionResponseIterator.chunk_parser
builds a fresh Delta per top-level Ollama stream chunk, and Delta's own
auto-indexing (litellm/types/utils.py) restarts its counter at 0 for every
chunk instead of tracking it across the whole response. Two tool calls in one
turn arrive as two SEPARATE top-level Ollama chunks (confirmed against the
live litellm proxy: both chunks came back stamped "index":0), so an
OpenAI-compatible client string-concatenates their name/arguments as if they
were fragments of one call — see app/agent/graph_utils.py's _make_llm comment and
Langfuse trace 3c6ed3b0 (2026-09-09) for the corrupted result this produced.

The first test below documents the bug against the STOCK, unpatched
litellm import this project's own dependencies provide. The second proves
litellm-patches/sitecustomize.py actually fixes it. If litellm's internals
shift under a future image pull and this stops importing cleanly, that's a
signal to revisit the patch, not a reason to skip it.
"""
import importlib.util
from pathlib import Path

import pytest

litellm_chat = pytest.importorskip(
    "litellm.llms.ollama.chat.transformation",
    reason="needs the litellm package this project's dependencies vendor",
)

OllamaChatCompletionResponseIterator = litellm_chat.OllamaChatCompletionResponseIterator

PATCH_PATH = Path(__file__).resolve().parents[2] / "litellm-patches" / "sitecustomize.py"


def _load_patch_module():
    spec = importlib.util.spec_from_file_location("_ollama_tool_call_index_patch", PATCH_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_iterator():
    return OllamaChatCompletionResponseIterator(streaming_response=iter([]), sync_stream=True)


def _two_tool_call_chunks():
    """Shaped exactly like the raw SSE captured live from the litellm proxy
    for a deliberate two-tool-call prompt: each tool call is its own
    top-level chunk, neither pre-stamped with an index."""
    chunk_a = {
        "model": "chat",
        "message": {
            "role": "assistant",
            "tool_calls": [{"function": {"name": "get_weather", "arguments": {"city": "Paris"}}}],
        },
        "done": False,
    }
    chunk_b = {
        "model": "chat",
        "message": {
            "tool_calls": [{"function": {"name": "get_time", "arguments": {"city": "Tokyo"}}}],
        },
        "done": True,
        "done_reason": "stop",
    }
    return chunk_a, chunk_b


@pytest.fixture(autouse=True)
def _restore_chunk_parser():
    """Every test starts from whatever chunk_parser currently is, and
    restores it afterward — makes the two tests order-independent instead
    of relying on file-execution order to see the "before" state."""
    original = OllamaChatCompletionResponseIterator.chunk_parser
    yield
    OllamaChatCompletionResponseIterator.chunk_parser = original


def test_unpatched_ollama_chunk_parser_collides_on_index_zero():
    it = _make_iterator()
    chunk_a, chunk_b = _two_tool_call_chunks()

    delta_a = it.chunk_parser(chunk_a).choices[0].delta
    delta_b = it.chunk_parser(chunk_b).choices[0].delta

    assert delta_a.tool_calls[0].index == 0
    # The bug: this should be 1 (a distinct tool call), not 0 again.
    assert delta_b.tool_calls[0].index == 0


def test_patched_ollama_chunk_parser_assigns_distinct_indices():
    _load_patch_module()  # applies the monkeypatch as a side effect, like sitecustomize.py does on interpreter startup

    it = _make_iterator()
    chunk_a, chunk_b = _two_tool_call_chunks()

    delta_a = it.chunk_parser(chunk_a).choices[0].delta
    delta_b = it.chunk_parser(chunk_b).choices[0].delta

    assert delta_a.tool_calls[0].index == 0
    assert delta_b.tool_calls[0].index == 1
    assert delta_a.tool_calls[0].function.name == "get_weather"
    assert delta_b.tool_calls[0].function.name == "get_time"


def test_patched_parser_leaves_a_single_tool_call_response_untouched():
    """A normal, single-tool-call turn (the common case) must still work
    exactly as before — this patch only pre-stamps a missing index, it
    never invents a second tool call or renumbers an already-present one."""
    _load_patch_module()

    it = _make_iterator()
    chunk = {
        "model": "chat",
        "message": {
            "role": "assistant",
            "tool_calls": [{"function": {"name": "get_weather", "arguments": {"city": "Paris"}}}],
        },
        "done": True,
        "done_reason": "stop",
    }

    delta = it.chunk_parser(chunk).choices[0].delta

    assert len(delta.tool_calls) == 1
    assert delta.tool_calls[0].index == 0
    assert delta.tool_calls[0].function.name == "get_weather"
