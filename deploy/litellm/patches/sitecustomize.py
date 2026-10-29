# Auto-imported at interpreter startup (the stdlib `site` imports a module named `sitecustomize`;
# docker-compose.yml puts this directory on PYTHONPATH for the `litellm` container). It runs before litellm's
# CLI, so the stock ghcr.io/berriai/litellm:main-stable image and entrypoint stay unchanged.
#
# Root cause it works around (verified live): `OllamaChatCompletionResponseIterator.chunk_parser()` runs once
# per top-level Ollama NDJSON chunk and builds a FRESH `Delta`, whose `__init__` numbers each tool_call's
# "index" by its position within THAT chunk's list, restarting at 0 every time (litellm/types/utils.py).
#
# When an `ollama_chat` model (qwen2.5:3b here) answers with two tool calls, Ollama streams them as two
# SEPARATE chunks, so the proxy emits two events BOTH carrying `"index":0` (captured from the raw SSE). A
# spec-correct OpenAI client (including langchain_openai's ChatOpenAI) treats chunks sharing an index as
# fragments of ONE call and concatenates them: name `check_vendor_status_pagerun_python_in_sandbox`, arguments
# `{"url": ...}{"script": ...}`. That is unparseable, never a real tool, so the agent fails the call and burns
# a retry believing the tool doesn't exist.
#
# Evidence: Langfuse traces 3c6ed3b0 (2026-09-09) and fc0a31db/dbd2c02b (2026-09-08), the incident `_make_llm`
# first tried to fix with `parallel_tool_calls=False`. That is a no-op here: litellm's ollama_chat doesn't
# list the parameter and `drop_params: true` silently discards it.
#
# The fix belongs where it is wrong: one globally increasing index per response. This pre-stamps each
# tool_call dict's "index" with a counter on the iterator INSTANCE (one per streamed response, so nothing
# leaks across requests) before `Delta.__init__` sees the list; `Delta` only invents an index when none is
# present.
#
# Deliberately defensive: `main-stable` is a floating tag, so the internals patched here may shift on a
# `docker compose pull`. If so, this fails LOUD (a stderr line in `docker logs litellm`) rather than silently
# no-op-ing back into the original bug.
import sys

try:
    import litellm.llms.ollama.chat.transformation as _ollama_chat_transform

    _Iterator = _ollama_chat_transform.OllamaChatCompletionResponseIterator
    _original_chunk_parser = _Iterator.chunk_parser

    def _patched_chunk_parser(self, chunk):
        try:
            tool_calls = (chunk.get("message") or {}).get("tool_calls")
            if tool_calls:
                counter = getattr(self, "_ollama_tool_call_index_counter", 0)
                for tool_call in tool_calls:
                    if isinstance(tool_call, dict) and tool_call.get("index") is None:
                        tool_call["index"] = counter
                        counter += 1
                self._ollama_tool_call_index_counter = counter
        except Exception as exc:  # noqa: BLE001 - never let the patch itself break a real response
            print(
                f"[litellm-patches] ollama_chat index patch raised, "
                f"falling back to stock behavior for this chunk: {exc!r}",
                file=sys.stderr,
            )
        return _original_chunk_parser(self, chunk)

    _Iterator.chunk_parser = _patched_chunk_parser
    print(
        "[litellm-patches] applied ollama_chat multi-tool-call index fix "
        "(see deploy/litellm/patches/sitecustomize.py for why)",
        file=sys.stderr,
    )
except Exception as exc:  # noqa: BLE001 - fail loud to stderr, never crash proxy startup
    print(
        f"[litellm-patches] FAILED to apply ollama_chat tool_call index fix: {exc!r} "
        "-- a turn where the model calls more than one tool at once will silently "
        "corrupt both calls into one unusable tool_call again (see "
        "deploy/litellm/patches/sitecustomize.py and app/agent/graph/utils.py's _make_llm comment)",
        file=sys.stderr,
    )
