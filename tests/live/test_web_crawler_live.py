"""A real crawl4ai-server round trip for app/ingestion/web_crawler.py
(GRAPH_PATTERNS.md pattern 50) — the fake-free counterpart to
tests/ingestion/test_web_crawler.py, which (correctly, for a fast/hermetic
suite) mocks crawl4ai's own `Crawl4aiDockerClient`/`_crawl` boundary. What
that can't catch — a real `docker-compose.yml` `crawl4ai` container
actually launching a browser, actually navigating, and returning the
`result.markdown`/`result.success`/`result.error_message` shape this
module's own code depends on, over a real authenticated HTTP round trip —
needs a real running server, hence `@pytest.mark.crawl` (self-skips if the
server isn't reachable, same "skip cleanly, don't fail" contract every
other live-service marker in this suite already has).

https://example.com is used as the fixed target — IANA's own minimal,
stable reference page, not expected to change shape or rate-limit, the
same kind of fixed external target this repo's own tests/ingestion/
test_ingestor.py docstrings assume is safe to rely on for a real fetch.

Only the SUCCESS path is exercised live here — a genuine browser-level
navigation failure (as opposed to this app's own pre-flight SSRF guard,
which never reaches a browser at all and is already covered hermetically
in tests/ingestion/test_web_crawler.py) needs a real target that resolves
in DNS but still fails to load, which isn't something this suite can pin
to a stable, always-reproducible internet host long-term. That path was
verified once, empirically, against a real `.invalid` TLD (guaranteed
non-resolving) during development — see GRAPH_PATTERNS.md pattern 50's own
note on the resulting `net::ERR_NAME_NOT_RESOLVED`/first-line-only
`CrawlFailed` message — and is otherwise covered deterministically by
tests/ingestion/test_web_crawler.py::TestCrawl's mocked-result tests, the
same "verified once live, tested hermetically thereafter" posture
tests/mcp/test_mcp_server.py's own docstring already uses for FastMCP's
in-process call_tool.
"""
import httpx
import pytest

from app.core.config import CRAWL4AI_SERVER_URL
from app.ingestion import web_crawler

pytestmark = pytest.mark.crawl


def _crawl4ai_server_reachable() -> bool:
    try:
        # /health needs no auth (crawl4ai's own self-hosting.md) — a plain
        # reachability probe, not a full round trip.
        response = httpx.get(f"{CRAWL4AI_SERVER_URL}/health", timeout=3)
        return response.status_code == 200
    except httpx.HTTPError:
        return False


@pytest.fixture(autouse=True)
def _require_crawl4ai_server():
    """`docker-compose.yml`'s `crawl4ai` service (`docker compose up -d
    crawl4ai`, part of the default `make up` profile). Skips cleanly rather
    than failing with an inscrutable connection error when it isn't up."""
    if not _crawl4ai_server_reachable():
        pytest.skip(f"crawl4ai server not reachable at {CRAWL4AI_SERVER_URL} — run `docker compose up -d crawl4ai`")


def test_renders_a_real_page_to_markdown():
    result = web_crawler.render_url_to_markdown("https://example.com")

    assert "Example Domain" in result
