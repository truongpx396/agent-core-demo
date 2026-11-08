"""The `PageFetcher` adapter (`app/ports/outbound/web.py`): one plain GET of a public URL for the ingestion pipeline (`app/ingestion/ingestor.py::ingest_url`).

Not the crawler (`app/adapters/outbound/crawl4ai/web_crawler.py` renders a page in a browser): this fetches the bytes and nothing else. The SSRF check is the
caller's, run before this; `follow_redirects=False` is what keeps a validated URL from leading to an unvalidated one.
"""
import httpx

from app.ports.outbound.web import FetchedPage, PageFetchFailedError

_TIMEOUT_SECONDS = 10
_USER_AGENT = "agent-core-demo-ingestor/1.0"


class HttpxPageFetcher:
    async def fetch(self, url: str, *, max_bytes: int) -> FetchedPage:
        try:
            async with httpx.AsyncClient(follow_redirects=False, timeout=_TIMEOUT_SECONDS) as client:
                response = await client.get(url, headers={"User-Agent": _USER_AGENT})
        except httpx.HTTPError as exc:
            raise PageFetchFailedError(str(exc)) from exc
        size = len(response.content)
        return FetchedPage(
            status_code=response.status_code,
            content_type=response.headers.get("content-type", ""),
            text=response.text if size <= max_bytes else "",
            size=size,
        )
