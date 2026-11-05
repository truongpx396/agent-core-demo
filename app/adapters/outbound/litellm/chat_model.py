"""The `ChatModels` adapter (`app/ports/llm.py`): LangChain's `ChatOpenAI` pointed at the LiteLLM gateway's OpenAI-compatible endpoint.

`temperature=0`, and `stream_usage=True`: `astream_events` (app/agent/runtime/stream.py's `astream_events_turn`) forces this model through its
streaming code path even though the agent node calls `.invoke()`, and OpenAI-compatible streaming only includes token usage in the final chunk
when explicitly requested. Without it `response.usage_metadata` is silently None under streaming: MAX_TOKENS_PER_TURN never trips and Langfuse
shows 0 tokens.

No `parallel_tool_calls=False` here (removed 2026-09-09): genuine multi-tool-call turns are supported.

History: two simultaneous tool calls used to come back from litellm's ollama_chat provider as ONE malformed tool_calls entry with concatenated
ids/names/args (Langfuse `fc0a31db`/`dbd2c02b`, 2026-09-08). `parallel_tool_calls=False` looked like a fix but was a proven no-op
(ollama_chat doesn't support it; litellm silently drops it): the corruption recurred (`3c6ed3b0`).

Root cause was in litellm: OllamaChatCompletionResponseIterator's chunk_parser restarts its tool-call index at 0 per chunk instead of per
response, so two calls in separate chunks both get index 0 and any OpenAI-compatible client string-concatenates them. Fixed at that layer
(deploy/litellm/patches/sitecustomize.py, loaded via PYTHONPATH in docker-compose.yml) with a globally increasing index.

The flag was then removed rather than kept as defense in depth: approval and routing already handled the full tool_calls list, just untested
with a real batch, now verified live (tests/live/test_agent_parallel_tool_calls.py). A batch needing approval is approved or rejected as ONE
decision covering every call, trading granularity for round trips, not visibility.
"""
from typing import Any

from langchain_openai import ChatOpenAI
from pydantic import SecretStr


class OpenAiChatModels:
    def build(self, tools: list, *, model: str, base_url: str, api_key: str) -> Any:
        return ChatOpenAI(
            model=model,
            base_url=base_url,
            api_key=SecretStr(api_key),
            temperature=0,
            stream_usage=True,
        ).bind_tools(tools)
