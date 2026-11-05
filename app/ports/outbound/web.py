"""The open web, as the domains reach it: searching it and reading a page.

Both are outward calls to a third party the app does not run, and both are made only from a human-approved tool. The tools (`app/domains/web_search.py`,
the page-reading tools of the support, ops and sales domains) own what to do with the answer (frame it as untrusted, scrub it, bound it); the adapters
own how the answer is obtained (`app/adapters/outbound/web/serper.py` for Google results through serper.dev, `app/adapters/outbound/crawl4ai/web_crawler.py` for a rendered
page through a crawl4ai server) and the guards that belong to the transport: the SSRF check on a URL, a retry only for a call that never landed, a
circuit breaker.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.ports.slot import Slot

# The deadline a tool gives one render: the crawler's own render budget plus the time to reach the server and read the answer.
CRAWL_RENDER_TIMEOUT_SECONDS = 30
CRAWL_TOOL_TIMEOUT_SECONDS = CRAWL_RENDER_TIMEOUT_SECONDS + 10


@dataclass(frozen=True)
class SearchHit:
    title: str
    url: str
    snippet: str
    date: str | None


class SearchFailed(Exception):
    """A search that did not produce results. The message is written for the model and the person, so it names no provider detail."""


class SearchNotConfigured(SearchFailed):
    """No search API key. Expected on a deployment that never opted in, so the tool answers it instead of raising."""


class CrawlFailed(Exception):
    """A page that could not be rendered. The message is written for the model and the person."""


class PageFetchFailed(Exception):
    """The request did not complete (a network error or a timeout). A 4xx or 5xx answer is a `FetchedPage`, not this."""


@dataclass(frozen=True)
class FetchedPage:
    """`text` is empty when the body was larger than the `max_bytes` asked for (`size` still says how large it was), so a caller never decodes what it will refuse."""

    status_code: int
    content_type: str
    text: str
    size: int


class WebSearch(Protocol):
    async def search(self, query: str) -> list[SearchHit]:
        """Up to the configured number of results for `query`, best first; an empty list means the search ran and found nothing.

        Raises `SearchNotConfigured` with no key, and `SearchFailed` for everything else that stops a result."""
        ...


class PageRenderer(Protocol):
    async def render(self, url: str) -> str:
        """The page at `url` as bounded markdown. Raises `CrawlFailed` for a URL the SSRF guard refuses or a page that cannot be rendered."""
        ...


class PageFetcher(Protocol):
    async def fetch(self, url: str, *, max_bytes: int) -> FetchedPage:
        """GETs `url` once, following no redirect (a validated URL redirecting to an unvalidated one would reopen the hole the caller's SSRF guard closes), within
        a short timeout. The caller runs that guard first. Raises `PageFetchFailed` when the request does not complete."""
        ...


web_search: Slot[WebSearch] = Slot("web_search", WebSearch)
page_fetcher: Slot[PageFetcher] = Slot("page_fetcher", PageFetcher)
page_renderer: Slot[PageRenderer] = Slot("page_renderer", PageRenderer)
