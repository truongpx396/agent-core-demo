"""Tests for scripts/ai_review_retry.py: how long to wait before retrying a model call.

The bodies below follow what public issue trackers report Gemini sending on a 429 (a `RetryInfo`
detail with `retryDelay`, a `QuotaFailure` whose `quotaId` names the window, and NO Retry-After
header). They have not been captured from this repo's own key; the real-run check in the PR does that.
"""
import json
import random

import pytest

from scripts import ai_review_retry as r

RETRY_INFO = "type.googleapis.com/google.rpc.RetryInfo"
QUOTA_FAILURE = "type.googleapis.com/google.rpc.QuotaFailure"


def body(*details, message="quota", wrap=False) -> bytes:
    error = {"error": {"code": 429, "message": message, "status": "RESOURCE_EXHAUSTED", "details": list(details)}}
    return json.dumps([error] if wrap else error).encode()


def quota(quota_id):
    return {"@type": QUOTA_FAILURE, "violations": [{"quotaId": quota_id, "quotaMetric": "x"}]}


# --- retry_hint -------------------------------------------------------------------------------


@pytest.mark.parametrize("value, seconds", [("7", 7.0), (" 3.5 ", 3.5), ("0", 0.0)])
def test_retry_hint_reads_a_retry_after_header_in_seconds_whatever_its_case(value, seconds):
    assert r.retry_hint({"Retry-After": value}, b"") == seconds
    assert r.retry_hint({"retry-after": value}, b"") == seconds
    assert r.retry_hint({"RETRY-AFTER": value}, b"") == seconds


def test_retry_hint_reads_a_retry_after_http_date_relative_to_now():
    date = "Wed, 21 Oct 2026 07:28:00 GMT"
    when = 1792567680.0  # that instant, in epoch seconds
    assert r.retry_hint({"Retry-After": date}, b"", now=when - 30) == 30.0
    assert r.retry_hint({"Retry-After": date}, b"", now=when + 90) == 0.0  # already past: retry now, never negative


def test_retry_hint_reads_retry_after_ms():
    assert r.retry_hint({"retry-after-ms": "1500"}, b"") == 1.5


@pytest.mark.parametrize("delay, seconds", [("34s", 34.0), ("34.4s", 34.4), ("500ms", 0.5), ("12", 12.0), (7, 7.0)])
def test_retry_hint_reads_googles_retry_info_from_the_body(delay, seconds):
    assert r.retry_hint({}, body(quota("x"), {"@type": RETRY_INFO, "retryDelay": delay})) == pytest.approx(seconds)


def test_retry_hint_reads_a_list_wrapped_body_and_a_protobuf_duration_object():
    wrapped = body({"@type": RETRY_INFO, "retryDelay": "9s"}, wrap=True)
    assert r.retry_hint({}, wrapped) == 9.0  # Google wraps some errors in a one-element list
    as_object = body({"@type": RETRY_INFO, "retryDelay": {"seconds": "34", "nanos": 500_000_000}})
    assert r.retry_hint({}, as_object) == pytest.approx(34.5)


def test_retry_hint_falls_back_to_the_retry_in_text_of_the_message():
    assert r.retry_hint({}, body(message="Quota exceeded. Please retry in 34.178s.")) == pytest.approx(34.178)
    assert r.retry_hint({}, body(message="Rate limit. Please try again in 250ms.")) == pytest.approx(0.25)


def test_retry_hint_prefers_a_header_over_the_body_over_the_message():
    both = body({"@type": RETRY_INFO, "retryDelay": "40s"}, message="retry in 99s")
    assert r.retry_hint({"Retry-After": "5"}, both) == 5.0
    assert r.retry_hint({}, both) == 40.0
    assert r.retry_hint({}, body(message="retry in 99s")) == 99.0


@pytest.mark.parametrize(
    "headers, raw",
    [
        (None, b""),
        ({}, b"not json"),
        ({"Retry-After": "soon"}, b"{}"),
        ({"Retry-After": "-5"}, b"{}"),  # a negative wait is not a wait
        ({}, body({"@type": RETRY_INFO, "retryDelay": "-3s"})),
        ({}, body({"@type": RETRY_INFO, "retryDelay": -3})),  # a JSON number, which no regex screens out
        ({}, body({"@type": "other.Type", "retryDelay": "30s"})),  # only a RetryInfo counts
        ({}, body({"@type": RETRY_INFO, "retryDelay": True})),  # a bool is not a duration
        ({}, b'{"error": "a plain string"}'),
        ({}, b"[]"),
    ],
)
def test_retry_hint_is_none_when_the_provider_gives_no_usable_hint(headers, raw):
    assert r.retry_hint(headers, raw) is None


# --- is_daily_quota ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "quota_id, daily",
    [
        ("GenerateRequestsPerDayPerProjectPerModel-FreeTier", True),
        ("generate_requests_per_day", True),
        ("DailyTokens", True),
        ("GenerateRequestsPerMinutePerProjectPerModel-FreeTier", False),
        ("GenerateContentInputTokensPerModelPerMinute", False),
    ],
)
def test_is_daily_quota_tells_a_day_window_from_a_minute_window(quota_id, daily):
    assert r.is_daily_quota(body(quota(quota_id), {"@type": RETRY_INFO, "retryDelay": "34s"})) is daily  # same short retryDelay either way
    assert r.is_daily_quota(body(quota(quota_id), wrap=True)) is daily


@pytest.mark.parametrize("raw", [b"", b"nope", b"{}", body(), body("not a dict"), body({"@type": QUOTA_FAILURE, "violations": "x"})])
def test_is_daily_quota_is_false_for_anything_it_cannot_read(raw):
    assert r.is_daily_quota(raw) is False


# --- wait_before_retry ------------------------------------------------------------------------


@pytest.fixture
def worst_jitter(monkeypatch):
    """random.uniform returns its upper bound, so each wait is the top of its range."""
    monkeypatch.setattr(r.random, "uniform", lambda low, high: high)


def test_a_provider_hint_is_waited_in_full_plus_a_second_of_jitter(worst_jitter):
    assert r.wait_before_retry(1, 429, 34.0, 0.0) == 35.0
    assert r.wait_before_retry(3, 503, 0.0, 0.0) == 1.0  # a hint of zero still gets the jitter, never a stampede


def test_a_hint_longer_than_the_most_we_wait_gives_up_instead_of_sleeping_through_it():
    assert r.wait_before_retry(1, 429, r.MAX_WAIT_S, 0.0) is not None  # exactly the cap is fine
    assert r.wait_before_retry(1, 429, r.MAX_WAIT_S + 1, 0.0) is None


def test_without_a_hint_a_5xx_backs_off_from_two_seconds(worst_jitter):
    assert [r.wait_before_retry(n, 503, None, 0.0) for n in (1, 2, 3)] == [4.0, 6.0, 10.0]  # 2*2^(n-1) + up to 2 of jitter
    assert r.wait_before_retry(1, 0, None, 0.0) == 4.0  # a dropped connection (status 0) is treated like a 5xx


def test_without_a_hint_a_429_starts_longer_because_quota_windows_are_minutes(worst_jitter):
    assert [r.wait_before_retry(n, 429, None, 0.0) for n in (1, 2, 3)] == [20.0, 30.0, 50.0]  # 10*2^(n-1) + up to 10
    assert r.wait_before_retry(4, 429, None, 0.0) == r.MAX_WAIT_S  # 80+10 would be 90; capped at the 60s maximum


def test_the_unhinted_wait_stays_inside_its_jitter_range():
    random.seed(7)
    for _ in range(300):
        assert 2.0 <= r.wait_before_retry(1, 503, None, 0.0) < 4.0
        assert 10.0 <= r.wait_before_retry(1, 429, None, 0.0) < 20.0
        assert 34.0 <= r.wait_before_retry(2, 429, 34.0, 0.0) < 35.0


def test_the_total_wait_budget_is_a_hard_ceiling(worst_jitter):
    assert r.wait_before_retry(2, 429, 14.0, 100.0) == 15.0  # 100 + 15 = 115, inside the 120s budget
    assert r.wait_before_retry(2, 429, 30.0, 100.0) is None  # 100 + 31 = 131 would blow it
    assert r.wait_before_retry(1, 503, None, 119.0) is None
