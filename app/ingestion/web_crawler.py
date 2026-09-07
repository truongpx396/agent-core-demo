"""crawl4ai-backed web rendering for anything in this app that needs a
LIVE, JS-rendered page turned into clean text mid-turn — not the bare
`httpx.get` + stdlib HTML-strip app/ingestion/ingestor.py::ingest_url
already does for static pages, but a real headless-Chromium render, for
the sites that don't work without one (client-rendered marketing sites,
SPA docs, anything gated behind a JS redirect).

Backs three domain tools, all declared "outward" wherever they're wired in
(app/agent/tools.py::TOOL_CAPABILITIES — reaches the open internet, so
app/agent/graph.py::should_continue always routes them through mandatory
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

Verified empirically against a real crawl (this module wasn't written
blind against crawl4ai's docs): `result.markdown` is a str-compatible
`StringCompatibleMarkdown`, not an object requiring `.raw_markdown`;
`result.success`/`result.error_message` are the right fields to check on
failure; a failed navigation's `error_message` is a multi-line, code-
context-including dump of crawl4ai's own internals — truncated to its
first line before it ever reaches a tool result/prompt, both to keep the
result readable and to avoid leaking this process's local file paths to
the model. `AsyncWebCrawler`'s own console progress logging (`[FETCH]`,
`[SCRAPE]`, `[COMPLETE]` lines) is independent of `BrowserConfig.verbose`
— confirmed empirically that `verbose=False` there does NOT silence it —
so a dedicated quiet `AsyncLogger` is passed explicitly instead, keeping
this app's own structured logging as the only thing writing to stdout.
"""
import asyncio
import logging

from crawl4ai import AsyncWebCrawler, BrowserConfig, CacheMode, CrawlerRunConfig
from crawl4ai.async_logger import AsyncLogger

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

_BROWSER_CONFIG = BrowserConfig(headless=True, verbose=False)
_QUIET_LOGGER = AsyncLogger(verbose=False)


class CrawlFailed(Exception):
    """A URL that passed the SSRF guard but couldn't actually be rendered
    (navigation error, timeout, no content) — an expected, caller-facing
    outcome, not a bug to let propagate as crawl4ai's own exception type.
    Propagates up through the calling tool exactly like any other
    exception (app/agent/graph.py's handle_tool_errors turns it into a
    ToolMessage the agent sees) — no domain tool here catches it itself,
    same posture every existing domain tool already takes toward its own
    store.py's exceptions."""


async def _crawl(url: str) -> str:
    run_config = CrawlerRunConfig(
        cache_mode=CacheMode.BYPASS,
        page_timeout=CRAWL_TIMEOUT_SECONDS * 1000,  # crawl4ai takes milliseconds
        verbose=False,
    )
    async with AsyncWebCrawler(config=_BROWSER_CONFIG, logger=_QUIET_LOGGER) as crawler:
        result = await crawler.arun(url=url, config=run_config)
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
    text = asyncio.run(_crawl(url))
    if len(text) > _MAX_MARKDOWN_CHARS:
        text = text[:_MAX_MARKDOWN_CHARS] + "\n\n[truncated: page content exceeds the fetch limit]"
    return text
