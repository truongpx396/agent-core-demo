"""The export worker's decisions as pure functions (app/billing/export.py): how long a failed event waits, and what
becomes of each event given what the provider said. No database and no provider, so every rule has its own test.

What the database and a real send then do with them (one delivery however many times it is retried, two workers never
double-sending, an event past the age limit expiring) is real-Postgres behaviour:
tests/integration/test_usage_export_real_postgres.py.
"""
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.billing import export
from app.billing.providers.base import ExportFailure, ExportResult, UsageEvent

BASE, CAP, MAX = 30, 3600, 5


def due(event_id: str, attempts: int = 0) -> export.Due:
    usage = UsageEvent(event_id, datetime(2026, 10, 1, tzinfo=UTC), Decimal("1"), Decimal("0.001"), "chat", 10)
    return export.Due(event_id, "acme", attempts, "cus_1", usage)


def decide(batch, result, error=None, **kwargs):
    return export.verdicts(batch, result, error, max_attempts=kwargs.get("max_attempts", MAX), base=BASE, cap=CAP)


class TestBackoff:
    @pytest.mark.parametrize("failures,expected", [(1, 30), (2, 60), (3, 120), (4, 240), (5, 480)])
    def test_it_doubles_after_each_failure_starting_at_the_base(self, failures, expected):
        assert export.backoff_seconds(failures, BASE, CAP) == expected

    def test_it_never_exceeds_the_cap_so_a_long_outage_still_retries_within_hours(self):
        assert export.backoff_seconds(10, BASE, CAP) == CAP

    def test_a_huge_attempt_count_is_capped_without_building_an_enormous_number(self):
        assert export.backoff_seconds(10**9, BASE, CAP) == CAP

    def test_zero_failures_is_treated_as_the_first(self):
        assert export.backoff_seconds(0, BASE, CAP) == BASE


class TestWhatTheProviderSaid:
    def test_accepted_is_sent(self):
        assert decide([due("a")], ExportResult(accepted=["a"]))["a"] == export.Verdict("sent")

    def test_a_reported_duplicate_is_success_not_a_failure(self):
        """A provider that says it already has the event (Polar does) is telling us the earlier send landed."""
        assert decide([due("a")], ExportResult(duplicate=["a"]))["a"].status == "sent"

    def test_a_retryable_failure_backs_off_and_stays_pending(self):
        verdict = decide([due("a", attempts=0)], ExportResult(failed=[ExportFailure("a", retryable=True)]))["a"]

        assert (verdict.status, verdict.delay_seconds, verdict.error) == ("pending", 30, "provider_retryable")

    def test_the_backoff_grows_with_the_attempts_already_made(self):
        verdict = decide([due("a", attempts=2)], ExportResult(failed=[ExportFailure("a", retryable=True)]))["a"]

        assert verdict.delay_seconds == 120

    def test_a_permanent_failure_is_final_at_once(self):
        verdict = decide([due("a")], ExportResult(failed=[ExportFailure("a", retryable=False)]))["a"]

        assert (verdict.status, verdict.error) == ("failed", "provider_permanent")

    def test_the_attempt_budget_ends_the_retrying(self):
        """The loop has a ceiling: the MAX-th failure is final, not another wait."""
        verdict = decide([due("a", attempts=MAX - 1)], ExportResult(failed=[ExportFailure("a", retryable=True)]))["a"]

        assert verdict.status == "failed" and verdict.error.startswith("attempts_exhausted")

    def test_an_event_the_provider_did_not_mention_is_retried_not_assumed_sent(self):
        """An adapter that drops an id must not make usage look delivered."""
        verdict = decide([due("a"), due("b")], ExportResult(accepted=["a"]))["b"]

        assert (verdict.status, verdict.error) == ("pending", "unreported")

    def test_an_id_reported_both_accepted_and_failed_is_read_as_not_landed(self):
        """Ambiguous, so the safe reading: a resend is harmless because the provider dedupes by the id."""
        verdict = decide([due("a")], ExportResult(accepted=["a"], failed=[ExportFailure("a", retryable=True)]))["a"]

        assert verdict.status == "pending"

    def test_every_event_in_the_batch_gets_a_verdict(self):
        verdicts = decide([due("a"), due("b"), due("c")], ExportResult(accepted=["a"], duplicate=["b"]))

        assert set(verdicts) == {"a", "b", "c"}


class TestWhenTheCallItselfFails:
    def test_every_event_is_retried_and_the_reason_is_only_the_exception_class(self):
        """Nothing is known to have landed, and a resend is safe. The text of the exception can carry a URL or a key."""
        verdicts = decide([due("a"), due("b")], None, RuntimeError("POST https://api.example/?key=sk_live_secret failed"))

        assert {v.status for v in verdicts.values()} == {"pending"}
        assert {v.error for v in verdicts.values()} == {"RuntimeError"}

    def test_a_deadline_is_a_retry_like_any_other_failure(self):
        assert decide([due("a")], None, TimeoutError())["a"].error == "TimeoutError"

    def test_the_attempt_budget_applies_to_a_failing_call_too(self):
        verdict = decide([due("a", attempts=MAX - 1)], None, ConnectionError())["a"]

        assert verdict.status == "failed"
