"""A real headless-Chromium round trip for app/ingestion/web_crawler.py
(GRAPH_PATTERNS.md pattern 50) — the fake-free counterpart to
tests/ingestion/test_web_crawler.py, which (correctly, for a fast/hermetic
suite) mocks crawl4ai's own `AsyncWebCrawler`/`_crawl` boundary. What that
can't catch — a real crawl4ai/Playwright version actually launching a
browser, actually navigating, and returning the `result.markdown`/
`result.success`/`result.error_message` shape this module's own code
depends on — needs a real browser, hence `@pytest.mark.crawl` (self-skips
if Playwright's Chromium isn't installed, same "skip cleanly, don't fail"
contract every other live-service marker in this suite already has).

`crawl4ai-setup`'s DB init isn't a hard prerequisite for `@pytest.mark.crawl`
— verified directly (removed `~/.crawl4ai`, re-ran a real crawl, it
lazily recreated it on first use): only the actual Chromium browser binary
(`playwright install --with-deps chromium`, already a required step for
this file's `e2e` sibling) needs to exist ahead of time.

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
import os

import pytest

from app.ingestion import web_crawler

pytestmark = pytest.mark.crawl


def _chromium_installed() -> bool:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            return os.path.exists(p.chromium.executable_path)
    except Exception:  # noqa: BLE001 - any failure here just means "not installed"
        return False


@pytest.fixture(autouse=True)
def _require_chromium():
    """Chromium's own browser binary (not just the `playwright` Python
    package, which requirements-lock.txt already installs unconditionally)
    has to be downloaded separately — `playwright install --with-deps
    chromium` (`make crawl4ai-setup`). Skips cleanly rather than failing
    with an inscrutable browser-launch error when that step hasn't run."""
    if not _chromium_installed():
        pytest.skip("Chromium not installed — run `make crawl4ai-setup` first")


def test_renders_a_real_page_to_markdown():
    result = web_crawler.render_url_to_markdown("https://example.com")

    assert "Example Domain" in result
