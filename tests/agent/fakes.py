"""Fakes shared by the graph regression tests that need a scripted model.

`GenericFakeChatModel` replays a fixed queue of replies, which is enough until a test has to
see what the graph SENT (the messages a call received) or answer differently depending on it.
`scripted_llm` is that: a chat model whose reply is a function of its input, recording every
input it was given.
"""
import uuid

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from app.agent.graph.core import MAX_ITERATIONS
from tests.conftest import TEST_CTX


def cfg(thread_id=None):
    return {
        "configurable": {"thread_id": thread_id or str(uuid.uuid4()), "ctx": TEST_CTX},
        "recursion_limit": MAX_ITERATIONS * 2 + 15,
    }


def call(name, args, call_id):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id}])


def orphan_tool_call_ids(messages):
    """Ids of tool calls in `messages` that no ToolMessage answers. The next LLM call on such
    a history is rejected by OpenAI-style APIs."""
    answered = {m.tool_call_id for m in messages if isinstance(m, ToolMessage)}
    return [
        tc["id"]
        for m in messages
        if isinstance(m, AIMessage)
        for tc in (m.tool_calls or [])
        if tc["id"] not in answered
    ]


def scripted_llm(respond, seen=None):
    """A fake chat model whose reply is a function of its input; records every input in `seen`.
    disable_streaming=True: under astream_events, GenericFakeChatModel's stream path raises
    "No generations found in stream" for a tool-call-only AIMessage."""

    class _Model(GenericFakeChatModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            if seen is not None:
                seen.append(list(messages))
            return ChatResult(generations=[ChatGeneration(message=respond(messages))])

        def bind_tools(self, tools, **kwargs):
            return self

    return _Model(messages=iter([]), disable_streaming=True)


def queue_llm(*replies, seen=None):
    queue = list(replies)
    return scripted_llm(lambda _messages: queue.pop(0), seen)


class Domain:
    """A minimal DomainPlugin for wiring tests."""

    def __init__(self, tools, capabilities):
        self._tools, self._capabilities = tools, capabilities

    def tools(self):
        return list(self._tools)

    def tool_capabilities(self):
        return dict(self._capabilities)

    def policy(self):
        return None
