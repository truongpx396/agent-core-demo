"""The `WebSearch` adapter (`app/ports/web.py`): the client for serper.dev, a Google Search API, the backend of the `web_search` tool
(`app/domains/web_search.py`).

Measured against the live API (2026-10-28), because the rest of this module rests on it:
- `POST https://google.serper.dev/search`, key in an `X-API-KEY` header, body `{"q": ..., "num": N}`. A good
  call is 200 with `organic: [{title, link, snippet, position, date?}]`, `credits: 1` and rate-limit headers
  (`x-ratelimit-limit: 25`). Only `organic` is read: no `answerBox` or `knowledgeGraph` appeared in either
  probe, and parsing a shape never seen is how a client breaks quietly.
- A wrong key is HTTP 403 `{"message": "Unauthorized.", "statusCode": 403}`.
- A BLANK query is not an error: `"   "` returned 200 with unrelated results and `credits: 1`. The provider
  bills for it, so a blank query is refused here before any request (`blank_query`).
- NOT measured, so not claimed: what an account with no credits returns, and what a 429 looks like (probing
  the limit would spend the credits it protects). Both are mapped by status class, and the provider's own
  `message` goes to the log.

Failure policy (constitution Principle V). A search is READ-ONLY: it changes only the account's credit
balance.
- No key: `SearchNotConfigured`, no request. Web search is optional and off by default like Langfuse, so this
  is an answer, not an error (the tool turns it into a normal reply; `agent_tool_errors_total` is untouched).
- A refusal (4xx), a 5xx or an unreadable 200: `SearchFailed`, with a message written for the model and the
  person. The provider's body NEVER reaches either (it is logged, scrubbed, truncated). Not retried: the
  request landed.
- Only a connection that never opened (`ConnectError`, `ConnectTimeout`) is retried: three attempts under
  `app/core/resilience.py::CircuitBreaker`. A read timeout is NOT, although a search is idempotent in effect:
  it may have been processed and billed, and "retry only what provably never landed" is the rule for every
  retry here (Principle IV).

Logs carry the outcome and status, never the query. The key is read once from `SERPER_API_KEY`, sent only in
the header, and listed among the scrubber's bound secrets.
"""
import logging
from urllib.parse import urlparse

import httpx

from app.core import metrics
from app.core.config import SERPER_API_KEY, WEB_SEARCH_MAX_RESULTS
from app.core.resilience import CircuitBreaker, CircuitOpenError
from app.core.scrubbing import scrub
from app.ports.web import SearchFailed, SearchHit, SearchNotConfigured

logger = logging.getLogger(__name__)

SERPER_SEARCH_URL = "https://google.serper.dev/search"

# Inside the tool's own budget (`TOOL_TIMEOUT_SECONDS`, 15 s) with room for the retries: three connect attempts at 3 s each plus the
# backoff is ~11 s, and a call that connects has the rest of the 10 s read window.
_TIMEOUT = httpx.Timeout(10.0, connect=3.0)
_BREAKER = CircuitBreaker(name="serper", failure_threshold=3, cooldown_seconds=30.0)
_RETRY_ATTEMPTS = 3
_RETRY_BASE_DELAY_SECONDS = 0.5
# Only a connection that never opened provably did not land (see the module docstring).
_NEVER_LANDED = (httpx.ConnectError, httpx.ConnectTimeout)

_MAX_TITLE_CHARS = 200
_MAX_SNIPPET_CHARS = 400
_MAX_PROVIDER_MESSAGE_CHARS = 120  # logged only, never shown to the model


def _fail(outcome: str, message: str, *, status: int | None = None, provider_message: str = "") -> SearchFailed:
    """Count the outcome, log its metadata, and hand back the exception to raise (so the call site reads `raise _fail(...)`)."""
    metrics.agent_web_search_total.labels(outcome=outcome).inc()
    logger.warning(
        "web_search_failed",
        extra={"outcome": outcome, "status": status, "provider_message": scrub(provider_message)[:_MAX_PROVIDER_MESSAGE_CHARS]},
    )
    return SearchFailed(message)


def _provider_message(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return ""
    return str(body.get("message", "")) if isinstance(body, dict) else ""


def _hits_from(data: object) -> list[SearchHit] | None:
    """The `organic` results as hits, or None when the body is not the documented shape. A single odd item is skipped, not fatal:
    one malformed result must not cost the person the other four."""
    if not isinstance(data, dict) or not isinstance(data.get("organic"), list):
        return None
    hits: list[SearchHit] = []
    for item in data["organic"]:
        if not isinstance(item, dict):
            continue
        title, link = item.get("title"), item.get("link")
        # http(s) only: the next step is a fetch tool that enforces https and the SSRF guard, but a result that is not even a web
        # address (a `javascript:` or `data:` link in a hostile index) has no business reaching the model as a "source".
        if not isinstance(title, str) or not isinstance(link, str) or urlparse(link).scheme not in ("http", "https"):
            continue
        snippet, date = item.get("snippet"), item.get("date")
        hits.append(
            SearchHit(
                title=" ".join(title.split())[:_MAX_TITLE_CHARS],
                url=link,
                snippet=" ".join(snippet.split())[:_MAX_SNIPPET_CHARS] if isinstance(snippet, str) else "",
                date=date if isinstance(date, str) else None,
            )
        )
    return hits[:WEB_SEARCH_MAX_RESULTS]


async def _search(client: httpx.AsyncClient, key: str, query: str) -> list[SearchHit]:
    async def post() -> httpx.Response:
        return await client.post(
            SERPER_SEARCH_URL, headers={"X-API-KEY": key}, json={"q": query, "num": WEB_SEARCH_MAX_RESULTS}
        )

    try:
        resp = await _BREAKER.call(
            post, retry_on=_NEVER_LANDED, attempts=_RETRY_ATTEMPTS, base_delay=_RETRY_BASE_DELAY_SECONDS
        )
    except CircuitOpenError:
        raise _fail(
            "circuit_open",
            "Web search is unreachable right now (repeated connection failures). Do not retry it; tell the person it is unavailable.",
        ) from None
    except httpx.HTTPError as exc:
        # The class name is enough for an operator and carries no request detail; an httpx message can name the URL.
        raise _fail(
            "unavailable",
            "Web search did not answer in time or could not be reached. Do not retry it; tell the person it is unavailable.",
            provider_message=type(exc).__name__,
        ) from None

    status = resp.status_code
    provider_message = _provider_message(resp)
    if status in (401, 403):
        raise _fail(
            "auth_failed",
            "The web search provider refused this deployment's API key, so web search is unavailable until an operator fixes it. "
            "Do not retry; tell the person it is unavailable.",
            status=status,
            provider_message=provider_message,
        )
    if status == 429:
        raise _fail(
            "rate_limited",
            "The web search provider is rate limiting this deployment. Do not retry now; tell the person to try again shortly.",
            status=status,
            provider_message=provider_message,
        )
    if 400 <= status < 500:
        raise _fail(
            "rejected",
            "The web search provider rejected the request, so web search is unavailable. "
            "Do not retry; tell the person it is unavailable.",
            status=status,
            provider_message=provider_message,
        )
    if status != 200:
        raise _fail(
            "unavailable",
            "The web search provider is having trouble. Do not retry now; tell the person it is unavailable.",
            status=status,
            provider_message=provider_message,
        )

    try:
        data = resp.json()
    except ValueError:
        data = None
    hits = _hits_from(data)
    if hits is None:
        raise _fail(
            "bad_response",
            "Web search returned something unreadable. Do not retry; tell the person it is unavailable.",
            status=status,
        )
    metrics.agent_web_search_total.labels(outcome="ok" if hits else "no_results").inc()
    return hits


async def search(query: str, *, client: httpx.AsyncClient | None = None) -> list[SearchHit]:
    """Up to `WEB_SEARCH_MAX_RESULTS` Google results for `query`, best first; an empty list means the search ran and found nothing.

    Raises `SearchNotConfigured` with no key, and `SearchFailed` for everything else that stops a result (see the module docstring).
    `client` is for tests, which hand in one on `httpx.MockTransport`; production opens one per call, like the rest of the app's
    short-lived HTTP callers (a search is rare and human-approved, so a pool would only hold sockets open for nothing).
    """
    cleaned = " ".join(query.split())
    if not cleaned:
        raise _fail("blank_query", "A web search needs some words to search for.")
    if not SERPER_API_KEY:
        metrics.agent_web_search_total.labels(outcome="not_configured").inc()
        raise SearchNotConfigured(
            "Web search is not set up on this deployment (no search API key is configured), so it cannot be used. "
            "Do not retry; tell the person web search is not available here."
        )
    if client is not None:
        return await _search(client, SERPER_API_KEY, cleaned)
    async with httpx.AsyncClient(timeout=_TIMEOUT) as owned:
        return await _search(owned, SERPER_API_KEY, cleaned)


class SerperWebSearch:
    """The `WebSearch` adapter (`app/ports/web.py`): `search` behind the port, looked up at CALL time so a test that patches the module function is seen."""

    async def search(self, query: str) -> list[SearchHit]:
        return await search(query)
