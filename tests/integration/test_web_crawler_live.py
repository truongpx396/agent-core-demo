"""A real crawl4ai-server round trip for app/ingestion/web_crawler.py
(GRAPH_PATTERNS.md pattern 50) — the fake-free counterpart to
tests/ingestion/test_web_crawler.py, which (correctly, for a fast/hermetic
suite) mocks crawl4ai's own `Crawl4aiDockerClient`/`_crawl` boundary. What
that can't catch — a real crawl4ai container actually launching a browser,
actually navigating, and returning the `result.markdown`/`result.success`/
`result.error_message` shape this module's own code depends on, over a
real authenticated HTTP round trip — needs a real running server, hence
`@pytest.mark.integration` (moved here from `tests/live/`'s own `crawl`
marker 2026-09-17, once `crawl4ai_server` below switched from a plain
reachability probe against an already-running `docker compose up -d
crawl4ai` to self-provisioning its own ephemeral container via
`tests/containers.py::ensure_crawl4ai()` — no LLM/graph/browser-E2E
dependency of this file's own ever justified `tests/live/` specifically,
unlike test_domain_crawl_tools_live.py, which stays there since it drives
the real graph through a human_approval interrupt). `crawl4ai_server`
(tests/integration/conftest.py) starts its own ephemeral container, same
"self-skip only if Docker itself isn't reachable" contract every other
testcontainers-managed fixture in this suite already has.

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
import pytest

from app.ingestion import web_crawler

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
    result = await web_crawler.render_url_to_markdown("https://example.com")

    assert "Example Domain" in result
