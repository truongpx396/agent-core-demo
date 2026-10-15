"""Tests for app/agent/graph_utils.py's `_friendly_tool_error` — ToolNode's
`handle_tool_errors` formatter, called with exactly the raised exception and
nothing else (see its own docstring on why it dispatches on the exception's
TYPE rather than a tool name/capability lookup).
"""
from app.agent.graph_utils import _friendly_tool_error
from app.agent.tool_idempotency import MutatingToolTimedOut


class TestFriendlyToolError:
    def test_a_generic_exception_gets_the_plain_message(self):
        message = _friendly_tool_error(ValueError("bad input"))

        assert "ValueError" in message
        assert "bad input" in message
        assert "try a different approach" in message.lower()

    def test_a_mutating_tool_timeout_gets_a_steering_message_instead(self):
        error = MutatingToolTimedOut("create_ticket")

        message = _friendly_tool_error(error)

        assert "create_ticket" in message
        assert "already have been" in message or "already applied" in message
        # The whole point: never encourage a blind retry here.
        assert "do not" in message.lower() or "don't" in message.lower()

    def test_a_plain_timeout_error_not_wrapped_by_idempotent_still_gets_the_generic_message(self):
        """Only the wrapped MutatingToolTimedOut type gets the special
        message — a bare TimeoutError from anywhere else (not every tool
        goes through tool_idempotency.idempotent(), e.g. read-only ones)
        stays on the generic path."""
        message = _friendly_tool_error(TimeoutError("slow"))

        assert "TimeoutError" in message
        assert "try a different approach" in message.lower()
