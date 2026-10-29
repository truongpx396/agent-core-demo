"""A real crawl4ai-server round trip for app/ingestion/web_crawler.py (GRAPH_PATTERNS.md pattern 50): the
fake-free counterpart to tests/ingestion/test_web_crawler.py, which (correctly, for a fast hermetic suite)
mocks the `Crawl4aiDockerClient`/`_crawl` boundary. What that can't catch needs a real server, hence
`@pytest.mark.integration`: a real container launching a browser, navigating, and returning the
`result.markdown`/`result.success`/`result.error_message` shape this module depends on, over a real
authenticated HTTP round trip.

It moved here from `tests/live/`'s `crawl` marker on 2026-09-17, once `crawl4ai_server`
(tests/integration/conftest.py) went from a plain reachability probe against an already-running `docker
compose up -d crawl4ai` to self-provisioning an ephemeral container via
`tests/containers.py::ensure_crawl4ai()`. Nothing in this file needs an LLM, the graph or a browser E2E, so
`tests/live/` no longer fit; test_domain_crawl_tools_live.py stays there because it drives the real graph
through a human_approval interrupt. The fixture has the same contract as every testcontainers-managed fixture
here: skip only if Docker itself isn't reachable.

The target is `tests/containers.py::CRAWL_TARGET_URL` (IANA's reference page about example domains), not
https://example.com, which was redesigned into a JS-animated, heading-less page and no longer renders the text
this test looked for (see that constant's comment). No third-party page can be made fully immune to that, so
the URL is defined once, shared with tests/live/test_domain_crawl_tools_live.py, and asserted only on its
static heading.

Only the SUCCESS path is exercised live. A genuine browser-level navigation failure (unlike this app's
pre-flight SSRF guard, which never reaches a browser and is covered hermetically in
tests/ingestion/test_web_crawler.py) needs a target that resolves in DNS yet fails to load, which no stable
internet host can provide long-term. That path was verified once against a real `.invalid` TLD (guaranteed
non-resolving) during development (see GRAPH_PATTERNS.md pattern 50 on the resulting
`net::ERR_NAME_NOT_RESOLVED` and first-line-only `CrawlFailed` message) and is covered deterministically by
tests/ingestion/test_web_crawler.py::TestCrawl. This is the "verified once live, tested hermetically
thereafter" posture of tests/mcp/test_mcp_server.py's docstring for FastMCP's in-process call_tool.
"""
import pytest

from app.ingestion import web_crawler
from tests.containers import CRAWL_TARGET_HEADING, CRAWL_TARGET_URL

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _use_crawl4ai_server(monkeypatch, crawl4ai_server):
    """Points `web_crawler`'s own `CRAWL4AI_SERVER_URL`/`CRAWL4AI_API_TOKEN`
    module globals at the ephemeral container `crawl4ai_server`
    (tests/integration/conftest.py) just started, the same `monkeypatch.setattr`
    shape the deepeval files' `real_ollama_chat_model` fixtures already use
    for `graph_module.CHAT_MODEL` — required because the token is only
    known at container-start time, well after `app/core/config.py`'s
    pydantic-settings constants were already read once at import time (see
    `ensure_crawl4ai`'s own docstring)."""
    monkeypatch.setattr(web_crawler, "CRAWL4AI_SERVER_URL", crawl4ai_server["crawl4ai_server_url"])
    monkeypatch.setattr(web_crawler, "CRAWL4AI_API_TOKEN", crawl4ai_server["crawl4ai_api_token"])


async def test_renders_a_real_page_to_markdown():
    result = await web_crawler.render_url_to_markdown(CRAWL_TARGET_URL)

    assert CRAWL_TARGET_HEADING in result
