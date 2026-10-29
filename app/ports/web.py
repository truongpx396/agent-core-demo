"""The open web, as the domains reach it: searching it and reading a page.

Both are outward calls to a third party the app does not run, and both are made only from a human-approved tool. The tools (`app/domains/web_search.py`,
the page-reading tools of the support, ops and sales domains) own what to do with the answer (frame it as untrusted, scrub it, bound it); the adapters
own how the answer is obtained (`app/adapters/web/serper.py` for Google results through serper.dev, `app/ingestion/web_crawler.py` for a rendered
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


class WebSearch(Protocol):
    async def search(self, query: str) -> list[SearchHit]:
        """Up to the configured number of results for `query`, best first; an empty list means the search ran and found nothing.

        Raises `SearchNotConfigured` with no key, and `SearchFailed` for everything else that stops a result."""
        ...


class PageRenderer(Protocol):
    async def render(self, url: str) -> str:
        """The page at `url` as bounded markdown. Raises `CrawlFailed` for a URL the SSRF guard refuses or a page that cannot be rendered."""
        ...


web_search: Slot[WebSearch] = Slot("web_search", WebSearch)
page_renderer: Slot[PageRenderer] = Slot("page_renderer", PageRenderer)
