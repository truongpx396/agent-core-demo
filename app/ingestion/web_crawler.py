"""crawl4ai-backed web rendering, for anything needing a LIVE, JS-rendered page turned into clean text mid-turn:
a headless-Chromium render for SPAs, client-rendered sites and JS-redirect gates, unlike the bare `httpx.get`
+ HTML-strip of `ingestor.py::ingest_url` (static pages).

Backs three "outward" domain tools (they reach the open internet, so `should_continue` forces human approval):
`sales/tools.py::enrich_lead_from_website`, `support/tools.py::fetch_external_reference`,
`ops/tools.py::check_vendor_status_page`.

The same SSRF guard as `ingestor.py` (`app/core/url_safety.py`) runs BEFORE crawl4ai launches a browser: a
JS-rendered fetch reaches the network like a bare GET, and crawl4ai has no concept of this app's threat model.

It renders through crawl4ai's dockerized server (the `crawl4ai` compose service, `unclecode/crawl4ai:0.9.3`, a
warm pooled browser) via the official `Crawl4aiDockerClient`, not hand-built HTTP
(`CrawlerRunConfig.dump()`/`BrowserConfig.dump()` produce a nested `{"type", "params"}` shape not worth
reimplementing).

Auth: crawl4ai 0.9.0+ is secure-by-default (loopback-only without a bearer token). The client only sets a
token through `.authenticate(email)`, a `/token` JWT flow this app doesn't use, so the `Authorization` header
is set directly on the client's httpx client.

Gotcha: no `browser_config` is sent. `BrowserConfig.dump()` always includes a `headers` field (a `sec-ch-ua`
default), and 0.9.0+'s server-side trust boundary 400s any request carrying it ("field 'headers' is not
permitted on BrowserConfig from an untrusted request"). Omitting it (the server uses its own default) is the
only thing that works; only `crawler_config` is sent.

`result.markdown` is str-compatible (no `.raw_markdown`); on failure check
`result.success`/`result.error_message`. A failed navigation's `error_message` is a multi-line internals dump,
so only its first line reaches a tool result or prompt, keeping server-side paths from the model.

`_crawl` retries a bare connection failure against the crawl4ai container (never a per-URL navigation failure,
which is a real answer: `result.success=False`) and, via `app/core/resilience.py::CircuitBreaker`, fails fast
after enough in a row, so calls don't each pay the connect timeout while the container is down.
"""
import logging

from crawl4ai import CacheMode, CrawlerRunConfig
from crawl4ai.docker_client import ConnectionError as Crawl4aiConnectionError
from crawl4ai.docker_client import Crawl4aiDockerClient
from crawl4ai.docker_client import RequestError as Crawl4aiRequestError

from app.core.config import CRAWL4AI_API_TOKEN, CRAWL4AI_SERVER_URL
from app.core.resilience import CircuitBreaker, CircuitOpenError
from app.core.url_safety import assert_safe_url_async

logger = logging.getLogger(__name__)

_CRAWL4AI_BREAKER = CircuitBreaker(name="crawl4ai", failure_threshold=3, cooldown_seconds=30.0)
_CRAWL4AI_RETRY_ATTEMPTS = 3  # 1 initial + 2 retries — a render is read-only
# (nothing to duplicate by trying again), so retrying a bare connection
# failure is unambiguously safe here, unlike a sandbox command_run (see
# app/core/resilience.py's own module docstring for why THAT isn't retried).
_CRAWL4AI_RETRY_BASE_DELAY_SECONDS = 0.5

CRAWL_TIMEOUT_SECONDS = 30  # headless-browser render is slower than a bare
# GET (ingestor.py's _URL_TIMEOUT_SECONDS=10). Passed to crawl4ai as
# page_timeout, so a hung page fails cleanly INSIDE _crawl (a real
# CrawlFailed) rather than only via the outer thread-kill below.

CRAWL_TOOL_TIMEOUT_SECONDS = CRAWL_TIMEOUT_SECONDS + 10  # the
# `_timeout_seconds` every domain tool passes to
# app/core/tool_runtime.py::run_with_timeout — larger than CRAWL_TIMEOUT_SECONDS
# so crawl4ai's own page_timeout fires first, giving a clean CrawlFailed
# message instead of the outer wrapper's blunter cutoff.

_MAX_MARKDOWN_CHARS = 20_000  # bounded like ingestor.py's _MAX_URL_BYTES —
# a full page's markdown could otherwise blow past what's reasonable in a
# tool result/prompt.


class CrawlFailed(Exception):
    """A URL that passed the SSRF guard but couldn't be rendered
    (navigation error, timeout, no content, server unreachable/
    unauthorized) — expected and caller-facing, not a bug. Propagates up
    like any other exception (`app/agent/graph/core.py`'s handle_tool_errors turns it into
    a ToolMessage) — no domain tool catches it itself."""


async def _crawl(url: str) -> str:
    run_config = CrawlerRunConfig(
        cache_mode=CacheMode.BYPASS,
        page_timeout=CRAWL_TIMEOUT_SECONDS * 1000,  # crawl4ai takes milliseconds
        verbose=False,
    )

    async def _attempt():
        # A fresh client (and connection pool) per attempt — a retry after
        # a dropped connection shouldn't reuse whatever socket just failed.
        async with Crawl4aiDockerClient(
            base_url=CRAWL4AI_SERVER_URL, timeout=CRAWL_TOOL_TIMEOUT_SECONDS, verbose=False
        ) as client:
            # Static pre-shared token, set directly (see module docstring) —
            # not through .authenticate()'s unrelated /token+email flow.
            client._http_client.headers["Authorization"] = f"Bearer {CRAWL4AI_API_TOKEN}"
            return await client.crawl([url], crawler_config=run_config)

    try:
        result = await _CRAWL4AI_BREAKER.call(
            _attempt,
            # Only a bare connection failure is retried — Crawl4aiRequestError
            # means the server DID respond, just with an error status (a bad
            # request on our end, or an internal crawl4ai failure), which a
            # second identical request won't fix.
            retry_on=(Crawl4aiConnectionError,),
            attempts=_CRAWL4AI_RETRY_ATTEMPTS,
            base_delay=_CRAWL4AI_RETRY_BASE_DELAY_SECONDS,
        )
    except (Crawl4aiConnectionError, Crawl4aiRequestError) as exc:
        raise CrawlFailed(f"could not reach the crawl4ai server for {url}: {exc}") from exc
    except CircuitOpenError as exc:
        raise CrawlFailed(str(exc)) from exc
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
    await assert_safe_url_async(url)
    text = await _crawl(url)
    if len(text) > _MAX_MARKDOWN_CHARS:
        text = text[:_MAX_MARKDOWN_CHARS] + "\n\n[truncated: page content exceeds the fetch limit]"
    return text
