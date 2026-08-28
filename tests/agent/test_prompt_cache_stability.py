"""Prompt-cache stability: the frozen instruction prefix must be
byte-identical across turns AND across principals.

This is the assertion that actually catches the classic cache-buster — a
prefix embedding `principal`, `tenant`, `trace_id`, or `datetime.now()` is
perfectly stable *within* one run and shares nothing across runs, silently
converting a discount-eligible prefix into a full-price one on every
provider that offers prompt caching (Anthropic, OpenAI, ...). Checking it
across turns alone would not catch this — the whole point is the SAME
conversation state, rendered for two DIFFERENT principals, must produce an
identical message list, not just a self-consistent one.

Two properties, checked independently:
1. `agent()`'s assembled message list is byte-identical for two different
   ctx values, given the same messages/context — proving ctx never enters
   the message list through app/agent/graph.py's own code.
2. No message anywhere in that list contains either ctx's tenant or
   principal string — a leak-detection sweep, the same shape as the
   AR-020-style "sentinel string never appears in a log record" check
   node telemetry gets (GRAPH_PATTERNS.md pattern 14), applied here to the
   prompt instead of a log line.

Both matter: (1) alone wouldn't catch a leak that happened to be identical
garbage in both cases; (2) alone wouldn't catch a *different* kind of
instability (e.g. a timestamp) that isn't ctx-shaped. Together they're the
actual guarantee GRAPH_PATTERNS.md documents.
"""
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage

from app.agent.graph import SYSTEM_PROMPT, make_agent_node

CTX_A = {"tenant": "acme-corp", "principal": "alice", "claims": {"role": "admin"}}
CTX_B = {"tenant": "globex-inc", "principal": "bob", "claims": {"role": "viewer"}}


class _RecordingFakeLLM:
    """Wraps GenericFakeChatModel and remembers every message list it was
    invoked with — see tests/agent/test_agent_node.py for why this is a plain
    wrapper rather than a subclass."""

    def __init__(self):
        self._inner = GenericFakeChatModel(messages=iter([AIMessage(content="answer")]))
        self.seen_messages: list = []

    def invoke(self, messages, *args, **kwargs):
        self.seen_messages = list(messages)
        return self._inner.invoke(messages, *args, **kwargs)


def _render(ctx: dict) -> list:
    llm = _RecordingFakeLLM()
    agent = make_agent_node(llm)
    state = {
        "messages": [HumanMessage(content="What is a checkpointer?")],
        "context": "doc: checkpointers persist graph state.",
        "ctx": ctx,
        "iterations": 0,
        "total_tokens": 0,
    }
    agent(state)
    return llm.seen_messages


class TestPrefixIdenticalAcrossPrincipals:
    def test_identical_message_list_for_two_different_ctx_values(self):
        rendered_a = _render(CTX_A)
        rendered_b = _render(CTX_B)

        assert len(rendered_a) == len(rendered_b)
        for msg_a, msg_b in zip(rendered_a, rendered_b, strict=True):
            assert type(msg_a) is type(msg_b)
            assert msg_a.content == msg_b.content

    def test_system_prompt_constant_itself_has_no_ctx_placeholders(self):
        """SYSTEM_PROMPT is a plain string constant, not an f-string
        assembled per-request — this is what makes the property above
        true by construction rather than by coincidence. A regression
        here (someone changing it to `f"...for {ctx['principal']}..."`)
        would break this trivial check before it ever reached a live
        prompt-cache economics problem."""
        assert isinstance(SYSTEM_PROMPT, str)
        for token in ("{tenant", "{principal", "{ctx", "%(tenant", "%(principal"):
            assert token not in SYSTEM_PROMPT


class TestNoCtxLeakIntoMessageContent:
    def test_neither_ctxs_tenant_or_principal_appears_in_rendered_messages(self):
        """Leak-detection sweep: even if the two message lists happened to
        match by coincidence, this independently proves neither ctx's
        identity strings made it into the text sent to the model."""
        for ctx in (CTX_A, CTX_B):
            rendered = _render(ctx)
            blob = "\n".join(
                m.content for m in rendered if isinstance(m.content, str)
            )
            other = CTX_B if ctx is CTX_A else CTX_A
            for sentinel in (ctx["tenant"], ctx["principal"], other["tenant"], other["principal"]):
                assert sentinel not in blob, (
                    f"ctx value {sentinel!r} leaked into a message sent to the LLM"
                )

    def test_claims_dict_never_leaks_into_message_content(self):
        """`claims` is the opaque, domain-specific bag (app/core/security.py) —
        nothing in app/agent/graph.py should ever stringify it into a prompt."""
        rendered = _render(CTX_A)
        blob = "\n".join(m.content for m in rendered if isinstance(m.content, str))
        assert "admin" not in blob
