"""crawl4ai-backed web rendering for anything needing a LIVE, JS-rendered
page turned into clean text mid-turn — not the bare `httpx.get` + stdlib
HTML-strip `ingestor.py::ingest_url` does for static pages, but a real
headless-Chromium render for sites that need one (SPAs, client-rendered
marketing sites, JS-redirect gates).

Backs three domain tools, all "outward" capability (reaches the open
internet -> mandatory human approval via `should_continue`):
`sales/tools.py::enrich_lead_from_website`,
`support/tools.py::fetch_external_reference`,
`ops/tools.py::check_vendor_status_page`.

Same SSRF guard as `ingestor.py` (`app/core/url_safety.py`, shared not
duplicated) runs BEFORE crawl4ai launches a browser — a JS-rendered fetch
reaches the network exactly like a bare GET, and crawl4ai itself has no
concept of this app's threat model.

Renders via crawl4ai's dockerized server (`docker-compose.yml`'s
`crawl4ai` service, `unclecode/crawl4ai:0.9.3` — a warm, pooled browser
container, not launched fresh per call), reached through the official
`Crawl4aiDockerClient` rather than hand-built HTTP calls (its
`CrawlerRunConfig.dump()`/`BrowserConfig.dump()` produce a nested
`{"type", "params"}` shape not worth reimplementing by hand).

Auth: crawl4ai 0.9.0+ is secure-by-default (binds loopback-only inside its
container without a valid bearer token). `Crawl4aiDockerClient` only
exposes token-setting via `.authenticate(email)` (a `/token` JWT flow this
app doesn't use), so the `Authorization` header is set directly on the
client's own httpx client instead.

Gotcha: no `browser_config` is sent at all. `BrowserConfig.dump()` always
includes a `headers` field (a `sec-ch-ua` fingerprint default even with
nothing set), and crawl4ai 0.9.0+'s server-side trust boundary
unconditionally 400s any request carrying it
("`field 'headers' is not permitted on BrowserConfig from an untrusted
request`"). Omitting `browser_config` entirely (server falls back to its
own default) is the only thing that works — only `crawler_config` is sent.

`result.markdown` is str-compatible directly (no `.raw_markdown` needed);
check `result.success`/`result.error_message` on failure. A failed
navigation's `error_message` is a multi-line internals dump — truncated to
its first line before reaching a tool result/prompt, both for readability
and to avoid leaking server-side paths to the model.
"""
import logging

from crawl4ai import CacheMode, CrawlerRunConfig
from crawl4ai.docker_client import ConnectionError as Crawl4aiConnectionError
from crawl4ai.docker_client import Crawl4aiDockerClient
from crawl4ai.docker_client import RequestError as Crawl4aiRequestError

from app.core.config import CRAWL4AI_API_TOKEN, CRAWL4AI_SERVER_URL
from app.core.url_safety import assert_safe_url

logger = logging.getLogger(__name__)

CRAWL_TIMEOUT_SECONDS = 30  # headless-browser render is slower than a bare
# GET (ingestor.py's _URL_TIMEOUT_SECONDS=10). Passed to crawl4ai as
# page_timeout, so a hung page fails cleanly INSIDE _crawl (a real
# CrawlFailed) rather than only via the outer thread-kill below.

CRAWL_TOOL_TIMEOUT_SECONDS = CRAWL_TIMEOUT_SECONDS + 10  # the
# `_timeout_seconds` every domain tool passes to
# app/agent/tools.py::_run_with_timeout — larger than CRAWL_TIMEOUT_SECONDS
# so crawl4ai's own page_timeout fires first, giving a clean CrawlFailed
# message instead of the outer wrapper's blunter cutoff.

_MAX_MARKDOWN_CHARS = 20_000  # bounded like ingestor.py's _MAX_URL_BYTES —
# a full page's markdown could otherwise blow past what's reasonable in a
# tool result/prompt.


class CrawlFailed(Exception):
    """A URL that passed the SSRF guard but couldn't be rendered
    (navigation error, timeout, no content, server unreachable/
    unauthorized) — expected and caller-facing, not a bug. Propagates up
    like any other exception (`graph.py`'s handle_tool_errors turns it into
    a ToolMessage) — no domain tool catches it itself."""


async def _crawl(url: str) -> str:
    run_config = CrawlerRunConfig(
        cache_mode=CacheMode.BYPASS,
        page_timeout=CRAWL_TIMEOUT_SECONDS * 1000,  # crawl4ai takes milliseconds
        verbose=False,
    )
    async with Crawl4aiDockerClient(
        base_url=CRAWL4AI_SERVER_URL, timeout=CRAWL_TOOL_TIMEOUT_SECONDS, verbose=False
    ) as client:
        # Static pre-shared token, set directly (see module docstring) —
        # not through .authenticate()'s unrelated /token+email flow.
        client._http_client.headers["Authorization"] = f"Bearer {CRAWL4AI_API_TOKEN}"
        try:
            result = await client.crawl([url], crawler_config=run_config)
        except (Crawl4aiConnectionError, Crawl4aiRequestError) as exc:
            raise CrawlFailed(f"could not reach the crawl4ai server for {url}: {exc}") from exc
    if not result.success:
        first_line = (result.error_message or "unknown error").strip().splitlines()[0]
        raise CrawlFailed(f"could not render {url}: {first_line}")
    return str(result.markdown or "")


async def render_url_to_markdown(url: str) -> str:
    """SSRF-guard `url`, render it with a real headless browser, and return
    clean Markdown — truncated to `_MAX_MARKDOWN_CHARS`, marked when it is.

    Raises `UnsafeURLError` (app/core/url_safety.py) before launching a
    browser, or `CrawlFailed` if the render fails. Unlike `ingestor.py`,
    neither is wrapped in a module-specific refusal type — this isn't an
    "ingest" (nothing writes to Qdrant), so the calling tool's normal
    exception handling is enough.
    """
    assert_safe_url(url)
    text = await _crawl(url)
    if len(text) > _MAX_MARKDOWN_CHARS:
        text = text[:_MAX_MARKDOWN_CHARS] + "\n\n[truncated: page content exceeds the fetch limit]"
    return text
