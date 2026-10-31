"""The chat model, as the agent uses it: a client to call, and a way to read why a call failed.

`ChatModels.build` returns a LangChain chat model bound to the domain's tools, pointed at the LiteLLM gateway (`app/adapters/litellm/chat_model.py`).
The graph, the compaction and follow-up calls and the nested subagent runs all build theirs through it, and a test hands the graph its own
(`GraphDeps.llm`, `GraphDeps.llm_factory`) without touching it. Which alias to call, whether the tool schemas are strict and what a turn may spend
are the application's (`app/agent/graph/utils.py::make_llm`, `app/agent/cost/`).

`LlmFailures` reads a failure raised by that client. What the application does with it (never retry a budget stop, answer a guardrail refusal
instead of failing, fall back to the local model when the routed one is unreachable) is policy in the graph and `app/core/gateway.py`; what
the SDK's exceptions LOOK like is the adapter's (`app/adapters/litellm/failures.py`), so no SDK class is named outside it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from app.ports.slot import Slot


@dataclass(frozen=True)
class ContentBlock:
    """A content guardrail's refusal. Only `mode` is kept: the guardrail's name, its category and the phrase it matched are the operator's
    (they are on the gateway's own log), not the caller's."""

    mode: str  # pre_call | post_call | during_call | other


class LlmFailures(Protocol):
    def is_budget_exceeded(self, exc: BaseException) -> bool:
        """The gateway refusing a call because a budget it enforces is spent. Every gateway budget (key, end user, team, tag) raises the same
        type, so this does not say WHICH ran out."""
        ...

    def is_permanent_client_error(self, exc: BaseException) -> bool:
        """The gateway or the provider rejecting the request itself (a 4xx other than a timeout, conflict, "too early" or rate limit), so
        sending the identical request again cannot succeed."""
        ...

    def is_unreachable(self, exc: BaseException) -> bool:
        """The model was not reached, or failed on its side (connection error, timeout, 5xx), and not because of a budget stop or a refusal of
        the request: the failures a different model could paper over."""
        ...

    def content_block(self, exc: BaseException) -> ContentBlock | None:
        """The guardrail refusal in `exc`, or None if it is anything else."""
        ...


class ChatModels(Protocol):
    def build(self, tools: list, *, model: str, base_url: str, api_key: str) -> Any:
        """A chat model for `model` at `base_url`, bound to `tools` (already shaped as the caller wants them bound). Streams token usage, so the
        per-turn token budget and the traces see real numbers."""
        ...


llm_failures: Slot[LlmFailures] = Slot("llm_failures", LlmFailures)
chat_models: Slot[ChatModels] = Slot("chat_models", ChatModels)
