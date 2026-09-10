"""crawl4ai-backed web rendering for anything in this app that needs a
LIVE, JS-rendered page turned into clean text mid-turn — not the bare
`httpx.get` + stdlib HTML-strip app/ingestion/ingestor.py::ingest_url
already does for static pages, but a real headless-Chromium render, for
the sites that don't work without one (client-rendered marketing sites,
SPA docs, anything gated behind a JS redirect).

Backs three domain tools, all declared "outward" wherever they're wired in
(app/agent/tools.py::TOOL_CAPABILITIES — reaches the open internet, so
app/agent/graph_routing.py::should_continue always routes them through mandatory
human approval, same tier as app/domains/ops/tools.py::post_to_team_channel):
- app/domains/sales/tools.py::enrich_lead_from_website — research a lead's
  company site before a rep calls them.
- app/domains/support/tools.py::fetch_external_reference — read a
  customer-linked third-party page live, for this turn's answer only.
- app/domains/ops/tools.py::check_vendor_status_page — correlate an
  anomaly against an upstream dependency's public status page.

Same SSRF guard as app/ingestion/ingestor.py (app/core/url_safety.py,
shared rather than duplicated) runs BEFORE crawl4ai ever launches a
browser against the URL — a JS-rendered fetch reaches the network exactly
like a bare httpx GET does, so it needs exactly the same guard, checked
here rather than trusted to crawl4ai (which has no concept of this app's
threat model at all).

Renders via crawl4ai's own dockerized server (`docker-compose.yml`'s
`crawl4ai` service, `unclecode/crawl4ai:0.9.3` — the browser now runs in
its own warm, pooled container, not launched fresh in THIS process on
every call), reached through crawl4ai's official `Crawl4aiDockerClient`
rather than hand-built HTTP calls — verified directly that
`CrawlerRunConfig.dump()`/`BrowserConfig.dump()` produce a non-trivial
nested `{"type": ..., "params": {...}}` shape, not a flat dict, so letting
the library serialize its own config objects is safer than reimplementing
that contract by hand. `browser_config`/`crawler_config` are still applied
per-request server-side exactly as before (confirmed in
`Crawl4aiDockerClient._prepare_request`), so none of THIS module's own
config logic changed, only the transport.

Auth: crawl4ai 0.9.0+ is secure-by-default — without a valid
`Authorization: Bearer` token matching the server's own
`CRAWL4AI_API_TOKEN`, it silently binds loopback-only inside its own
container (self-hosting.md). `Crawl4aiDockerClient` only exposes
token-setting via `.authenticate(email)`, a `/token`-issued-JWT flow this
app doesn't use (there's no login/email here, just a static pre-shared
secret) — so the header is set directly on the client's own httpx client
instead, verified empirically to work the same way `.authenticate()`
itself sets it internally.

No `browser_config` is sent at all — verified empirically against the real
running server (not assumed from docs) that this matters: `BrowserConfig`'s
own `.dump()` always includes a `headers` field (a `sec-ch-ua` fingerprint
default, even with nothing explicitly set), and crawl4ai 0.9.0+'s
server-side "strict trust boundary" (self-hosting.md's own phrase)
unconditionally 400s any request carrying it — `"field 'headers' is not
permitted on BrowserConfig from an untrusted request"`. Omitting
`browser_config` (server falls back to its own internal default, `{}` on
the wire) is what actually works; there is no in-between "just send
headless/verbose" option once ANY BrowserConfig object is dumped. Only
`crawler_config` is sent per-request.

Verified empirically against a real crawl (this module wasn't written
blind against crawl4ai's docs): `result.markdown` is a str-compatible
`StringCompatibleMarkdown`, not an object requiring `.raw_markdown`;
`result.success`/`result.error_message` are the right fields to check on
failure; a failed navigation's `error_message` is a multi-line, code-
context-including dump of crawl4ai's own internals — truncated to its
first line before it ever reaches a tool result/prompt, both to keep the
result readable and to avoid leaking crawl4ai server-side paths to the
model.
"""
import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor

from crawl4ai import CacheMode, CrawlerRunConfig
from crawl4ai.docker_client import ConnectionError as Crawl4aiConnectionError
from crawl4ai.docker_client import Crawl4aiDockerClient
from crawl4ai.docker_client import RequestError as Crawl4aiRequestError

from app.core.config import CRAWL4AI_API_TOKEN, CRAWL4AI_SERVER_URL
from app.core.url_safety import assert_safe_url

logger = logging.getLogger(__name__)

CRAWL_TIMEOUT_SECONDS = 30  # a real headless-browser navigate+render is
# slower than a bare HTTP GET (app/ingestion/ingestor.py's
# _URL_TIMEOUT_SECONDS=10) — its own named budget, same "this legitimately
# takes longer" reasoning app/agent/tools.py's SUBAGENT_TIMEOUT_SECONDS
# already gives for being larger than the default TOOL_TIMEOUT_SECONDS.
# Passed to crawl4ai itself as page_timeout, so a hung page fails cleanly
# INSIDE _crawl (a real CrawlFailed) rather than only ever being caught by
# CRAWL_TOOL_TIMEOUT_SECONDS's outer thread-kill below.

CRAWL_TOOL_TIMEOUT_SECONDS = CRAWL_TIMEOUT_SECONDS + 10  # the
# `_timeout_seconds` every domain tool built on render_url_to_markdown
# passes to app/agent/tools.py::_run_with_timeout — deliberately larger
# than CRAWL_TIMEOUT_SECONDS itself, so crawl4ai's own page_timeout is what
# actually fires on a hung page (producing a clean, readable CrawlFailed
# message) rather than the outer wrapper's blunter "exceeded the Ns
# timeout" cutting it off first.

_MAX_MARKDOWN_CHARS = 20_000  # bounded like app/ingestion/ingestor.py's
# _MAX_URL_BYTES — a full page's rendered markdown could otherwise blow
# well past what's reasonable to fold into a tool result/prompt.


class CrawlFailed(Exception):
    """A URL that passed the SSRF guard but couldn't actually be rendered
    (navigation error, timeout, no content, or the crawl4ai server itself
    unreachable/unauthorized) — an expected, caller-facing outcome, not a
    bug to let propagate as crawl4ai's own exception type. Propagates up
    through the calling tool exactly like any other exception
    (app/agent/graph.py's handle_tool_errors turns it into a ToolMessage
    the agent sees) — no domain tool here catches it itself, same posture
    every existing domain tool already takes toward its own store.py's
    exceptions."""


async def _crawl(url: str) -> str:
    run_config = CrawlerRunConfig(
        cache_mode=CacheMode.BYPASS,
        page_timeout=CRAWL_TIMEOUT_SECONDS * 1000,  # crawl4ai takes milliseconds
        verbose=False,
    )
    async with Crawl4aiDockerClient(
        base_url=CRAWL4AI_SERVER_URL, timeout=CRAWL_TOOL_TIMEOUT_SECONDS, verbose=False
    ) as client:
        # See module docstring: a static pre-shared token, set directly
        # rather than through .authenticate()'s unrelated /token+email flow.
        client._http_client.headers["Authorization"] = f"Bearer {CRAWL4AI_API_TOKEN}"
        try:
            result = await client.crawl([url], crawler_config=run_config)
        except (Crawl4aiConnectionError, Crawl4aiRequestError) as exc:
            raise CrawlFailed(f"could not reach the crawl4ai server for {url}: {exc}") from exc
    if not result.success:
        first_line = (result.error_message or "unknown error").strip().splitlines()[0]
        raise CrawlFailed(f"could not render {url}: {first_line}")
    return str(result.markdown or "")


def render_url_to_markdown(url: str) -> str:
    """SSRF-guard `url`, render it with a real headless browser, and return
    clean Markdown — truncated to _MAX_MARKDOWN_CHARS, marked when it is.

    Raises UnsafeURLError (app/core/url_safety.py) before ever launching a
    browser, or CrawlFailed if the render itself fails. Both are plain
    exceptions, not wrapped in this module's own refusal type the way
    app/ingestion/ingestor.py wraps SSRF refusals as IngestRefused — this
    isn't an "ingest" (nothing here writes to Qdrant), so there's no
    ingest-specific refusal type for it to become; the calling tool's
    normal exception handling is enough (see CrawlFailed's own docstring).
    """
    assert_safe_url(url)
    text = _run_crawl_sync(url)
    if len(text) > _MAX_MARKDOWN_CHARS:
        text = text[:_MAX_MARKDOWN_CHARS] + "\n\n[truncated: page content exceeds the fetch limit]"
    return text


def _run_crawl_sync(url: str) -> str:
    """`asyncio.run(_crawl(url))`, except also correct when the CALLING
    thread already has a running event loop — verified directly this is a
    real case, not theoretical: CI hit `RuntimeError: asyncio.run() cannot
    be called from a running event loop` here (some other async work
    sharing this pytest-xdist worker's thread, not this function's own
    fault — `asyncio.run()` checks for a running loop before it ever
    touches `_crawl` at all). The fast, common path (no loop already
    running) is unchanged; the fallback runs `_crawl` in its own thread
    with a fresh loop instead of fighting over the calling thread's."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_crawl(url))
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, _crawl(url)).result()
