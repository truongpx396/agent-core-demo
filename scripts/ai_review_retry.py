"""When, and for how long, to wait before retrying a model call. Pure functions, stdlib only.

What the providers actually send is the part the docs under-specify, so each rule here says where it
comes from. NOTE: the Gemini details below come from reports in public issue trackers (vercel/ai,
inspect_ai), not from Google's own docs, and have not been captured from this repo's own key.

- Gemini puts the wait in the JSON BODY of a 429, as `google.rpc.RetryInfo.retryDelay` (`"34.4s"`),
  and sends NO `Retry-After` header. Many other OpenAI-compatible providers do send the header
  (seconds, or an HTTP date), so both are read.
- A per-DAY quota 429 still carries a short `retryDelay`, but retrying cannot help until the quota
  resets. The `quotaId` of its `google.rpc.QuotaFailure` names the window (`...PerDay...` vs
  `...PerMinute...`), so a daily one is not retried at all.
- Google's SDK guidance for 429 and 503: exponential backoff from about a second with jitter, a
  maximum single delay of 60 seconds, and up to four attempts.

The old policy waited about 2s, then 4s. A free tier's rate-limit window is a MINUTE, so three
attempts inside ten seconds could only ever fail; waiting as the server asks is what works.
"""
import email.utils
import json
import random
import re
import time
from collections.abc import Mapping

RETRY_STATUSES = frozenset({408, 429, 500, 502, 503, 504})
ATTEMPTS = 4  # Google's own SDK retries transient errors up to four times
BACKOFF_S = 2.0  # first wait for a 5xx or a dropped connection; it doubles, plus up to this much jitter
RATE_LIMIT_BACKOFF_S = 10.0  # for a 429 with no hint: quota windows are minutes, not seconds
MAX_WAIT_S = 60.0  # the longest single wait (Google's SDK example caps a delay at 60s)
WAIT_BUDGET_S = 120.0  # all waits together; the job's own limit is 10 minutes


def _body(raw: bytes) -> dict[str, object]:
    """The `error` object of a provider's error body, or {}. Google wraps some in a one-element list."""
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    if isinstance(data, list) and data:
        data = data[0]
    error = data.get("error") if isinstance(data, dict) else None
    return error if isinstance(error, dict) else {}


def _seconds(value: object) -> float | None:
    """A duration as seconds: `34`, `"34.4s"`, `"500ms"`, or protobuf's `{"seconds": 34, "nanos": 4e8}`."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value) if value >= 0 else None
    if isinstance(value, str):
        found = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(ms|s)?\s*", value)
        if found:
            return float(found.group(1)) / (1000 if found.group(2) == "ms" else 1)
    if isinstance(value, dict):
        seconds, nanos = _seconds(value.get("seconds", 0)), _seconds(value.get("nanos", 0))
        if seconds is not None and nanos is not None:
            return seconds + nanos / 1e9
    return None


def retry_hint(headers: Mapping[str, str] | None, raw: bytes, now: float | None = None) -> float | None:
    """How long the provider says to wait, in seconds, or None if it does not say.

    In order of authority: a `Retry-After` header (seconds or an HTTP date, RFC 9110) or
    `retry-after-ms`; Google's `RetryInfo.retryDelay` in the body; and, as a last resort, the
    "retry in 34.2s" that a provider writes into its message. Reading the message is parsing only;
    it is never logged.
    """
    lowered = {str(key).lower(): value for key, value in (headers or {}).items()}
    if "retry-after-ms" in lowered:
        millis = _seconds(str(lowered["retry-after-ms"]))
        if millis is not None:
            return millis / 1000
    if "retry-after" in lowered:
        text = str(lowered["retry-after"])
        seconds = _seconds(text)
        if seconds is not None:
            return seconds
        try:
            when = email.utils.parsedate_to_datetime(text).timestamp()
        except (TypeError, ValueError):
            when = None
        if when is not None:
            return max(0.0, when - (time.time() if now is None else now))
    error = _body(raw)
    details = error.get("details")
    for detail in details if isinstance(details, list) else []:
        if isinstance(detail, dict) and str(detail.get("@type", "")).endswith("RetryInfo"):
            seconds = _seconds(detail.get("retryDelay"))
            if seconds is not None:
                return seconds
    said = re.search(r"(?:retry|try again) in (\d+(?:\.\d+)?)\s*(ms|s)", str(error.get("message", "")), re.IGNORECASE)
    return _seconds(f"{said.group(1)}{said.group(2)}") if said else None


def is_daily_quota(raw: bytes) -> bool:
    """True for a 429 whose quota window is a day (`...PerDay...`): no wait this job can afford helps."""
    details = _body(raw).get("details")
    for detail in details if isinstance(details, list) else []:
        violations = detail.get("violations") if isinstance(detail, dict) else None
        for violation in violations if isinstance(violations, list) else []:
            quota = str(violation.get("quotaId", "")) if isinstance(violation, dict) else ""
            squashed = re.sub(r"[^a-z]", "", quota.lower())  # `GenerateRequestsPerDay-FreeTier` and `requests_per_day` alike
            if "perday" in squashed or "daily" in squashed:
                return True
    return False


def wait_before_retry(attempt: int, status: int, hint: float | None, waited: float) -> float | None:
    """Seconds to sleep before retry number `attempt` (1-based), or None to give up.

    A provider's own hint beats our guess: wait that long, plus up to a second of jitter so clients
    released together do not stampede. With no hint, exponential backoff with jitter, starting
    longer for a 429. Gives up if the provider asks for more than `MAX_WAIT_S` (a CI step cannot
    sit that long) or if this wait would push the total past `WAIT_BUDGET_S`.
    """
    if hint is not None:
        if hint > MAX_WAIT_S:
            return None
        wait = hint + random.uniform(0, 1.0)
    else:
        base = RATE_LIMIT_BACKOFF_S if status == 429 else BACKOFF_S
        wait = min(base * 2 ** (attempt - 1) + random.uniform(0, base), MAX_WAIT_S)
    return None if waited + wait > WAIT_BUDGET_S else wait
