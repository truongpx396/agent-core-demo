"""Tests for app/agent/graph/utils.py's `friendly_tool_error` — ToolNode's
`handle_tool_errors` formatter, called with exactly the raised exception and
nothing else (see its own docstring on why it dispatches on the exception's
TYPE rather than a tool name/capability lookup).
"""
from app.agent.graph.utils import friendly_tool_error
from app.core.tool_idempotency import MutatingToolTimedOut


class TestFriendlyToolError:
    def test_a_generic_exception_gets_the_plain_message(self):
        message = friendly_tool_error(ValueError("bad input"))

        assert "ValueError" in message
        assert "bad input" in message
        assert "try a different approach" in message.lower()

    def test_a_mutating_tool_timeout_gets_a_steering_message_instead(self):
        error = MutatingToolTimedOut("create_ticket")

        message = friendly_tool_error(error)

        assert "create_ticket" in message
        assert "already have been" in message or "already applied" in message
        # The whole point: never encourage a blind retry here.
        assert "do not" in message.lower() or "don't" in message.lower()

    def test_a_plain_timeout_error_not_wrapped_by_idempotent_still_gets_the_generic_message(self):
        """Only the wrapped MutatingToolTimedOut type gets the special
        message — a bare TimeoutError from anywhere else (not every tool
        goes through tool_idempotency.idempotent(), e.g. read-only ones)
        stays on the generic path."""
        message = friendly_tool_error(TimeoutError("slow"))

        assert "TimeoutError" in message
        assert "try a different approach" in message.lower()


class TestADatabaseFailureDoesNotLeakItsStatement:
    """Langfuse trace `e0ca1456`: `check_ticket_status` raised `UndefinedTable` and the model was told
    `relation "support_ticket_comments" does not exist  LINE 1: ... FROM support_tickets t LEFT JOIN
    support_ti...`, which it paraphrased to the person. GRAPH_PATTERNS.md pattern 81."""

    def _error(self):
        import psycopg

        return psycopg.errors.UndefinedTable(
            'relation "support_ticket_comments" does not exist\n'
            "LINE 1: ...ted_at) AS notes FROM support_tickets t LEFT JOIN support_ti...\n"
        )

    def test_the_statement_and_the_names_in_it_stay_out_of_the_message(self):
        message = friendly_tool_error(self._error())

        assert "UndefinedTable" in message
        for leaked in ("support_ticket_comments", "support_tickets", "LINE 1", "LEFT JOIN"):
            assert leaked not in message

    def test_the_model_is_told_not_to_retry_and_to_say_the_lookup_is_unavailable(self):
        message = friendly_tool_error(self._error()).lower()

        assert "do not retry" in message
        assert "unavailable" in message
        assert "different approach" not in message

    def test_a_driver_error_of_any_kind_gets_it(self):
        import psycopg

        assert "do not retry" in friendly_tool_error(psycopg.OperationalError("connection refused: host=db")).lower()
        assert "host=db" not in friendly_tool_error(psycopg.OperationalError("connection refused: host=db"))

    def test_other_errors_keep_the_message_the_model_needs_to_fix_its_arguments(self):
        assert "bad input" in friendly_tool_error(ValueError("bad input"))
