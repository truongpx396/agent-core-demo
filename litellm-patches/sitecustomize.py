# Auto-imported by Python at interpreter startup (the stdlib `site` module
# imports a module named exactly `sitecustomize` if one is importable, which
# it is here purely because docker-compose.yml puts this directory on
# PYTHONPATH for the `litellm` proxy container) — this runs before litellm's
# own CLI does anything, with no changes to the stock
# ghcr.io/berriai/litellm:main-stable image or its entrypoint.
#
# Root cause this works around (verified live, not a guess):
#
# litellm.llms.ollama.chat.transformation.OllamaChatCompletionResponseIterator
# .chunk_parser() is called once per top-level Ollama NDJSON stream chunk and
# builds a FRESH litellm.types.utils.Delta object each time. Delta.__init__
# auto-assigns each tool_call dict's "index" by enumerating its POSITION
# WITHIN THAT chunk's own tool_calls list, starting a fresh counter at 0
# every call (litellm/types/utils.py, Delta.__init__): it has no memory of
# indices it already handed out for earlier chunks in the same response.
#
# When an ollama_chat model (this app uses qwen2.5:3b, see
# litellm-config.yaml) answers with two tool calls in one turn, Ollama's own
# API delivers them as two SEPARATE top-level stream chunks, not bundled
# into one delta's tool_calls list (confirmed by capturing the raw SSE this
# proxy emits for a deliberate two-tool-call prompt: two "data:" events, one
# per tool, BOTH carrying "index":0). Any OpenAI-compatible client —
# including this app's langchain_openai ChatOpenAI — is spec-correct to
# treat two tool_call chunks sharing one index as fragments of a SINGLE
# call's streamed arguments and string-concatenate their name/arguments
# fields. Two distinct, complete tool calls sharing index 0 therefore get
# glued into one corrupted entry: "check_vendor_status_pagerun_python_in_
# sandbox" as the name, `{"url": "..."}{"script": "..."}` as the arguments
# — unparseable, and never a real tool, so the agent always fails the call
# and burns a retry believing it "picked a tool that doesn't exist."
#
# Live evidence this actually happens against this stack: Langfuse traces
# 3c6ed3b0 (2026-09-09) and fc0a31db/dbd2c02b (2026-09-08, the incident
# app/agent/graph.py's _make_llm originally tried to fix with
# `parallel_tool_calls=False` — a no-op here, since litellm's ollama_chat
# get_supported_openai_params() doesn't list parallel_tool_calls at all, and
# litellm-config.yaml's `drop_params: true` makes litellm silently discard
# it instead of erroring, so the parameter never reaches Ollama).
#
# The fix belongs at the layer that's actually wrong: chunk_parser should
# hand out one globally-increasing index per response, not per chunk. This
# patches exactly that, pre-stamping each tool_call dict's "index" with a
# counter kept on the iterator INSTANCE (which already lives for the full
# lifetime of one streamed HTTP response — a fresh instance is created per
# request via OllamaChatCompletionResponseIterator's own factory, so this
# never leaks a count across turns or requests) before Delta.__init__ ever
# sees the tool_calls list. Delta.__init__ only invents an index when one
# isn't already present, so pre-stamping the correct one here is enough —
# no other code path needs to change.
#
# Deliberately defensive: `main-stable` is a floating tag, so the exact
# internals this patches could shift under a future `docker compose pull`.
# If that happens this fails LOUD (a stderr line any `docker logs litellm`
# will show) rather than silently no-op-ing back into the original bug.
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
        "(see litellm-patches/sitecustomize.py for why)",
        file=sys.stderr,
    )
except Exception as exc:  # noqa: BLE001 - fail loud to stderr, never crash proxy startup
    print(
        f"[litellm-patches] FAILED to apply ollama_chat tool_call index fix: {exc!r} "
        "-- a turn where the model calls more than one tool at once will silently "
        "corrupt both calls into one unusable tool_call again (see "
        "litellm-patches/sitecustomize.py and app/agent/graph_utils.py's _make_llm comment)",
        file=sys.stderr,
    )
