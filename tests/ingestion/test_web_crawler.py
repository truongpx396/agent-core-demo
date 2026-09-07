"""Tests for app/ingestion/web_crawler.py. Two boundaries are mocked,
matching this suite's general "mock the boundary, not the whole library"
convention (e.g. tests/mcp/test_mcp_client.py mocks `_list_remote_tools`/
`_call_remote_tool`, not the stdio transport underneath them):

- `render_url_to_markdown`'s own tests patch `web_crawler._crawl` (the
  async function actually launching a browser) — proves the SSRF guard
  runs BEFORE any crawl is attempted, and that truncation applies after.
- `_crawl`'s own tests patch `web_crawler.AsyncWebCrawler` with a fake
  async context manager — proves the CrawlFailed/success-flag handling
  this module adds on top of crawl4ai's own result object, without a real
  browser. That result shape (`result.success`, `result.error_message`,
  a str-compatible `result.markdown`) was verified empirically against a
  real crawl before this module was written — see its own docstring.
"""
import asyncio

from app.core.url_safety import UnsafeURLError
from app.ingestion import web_crawler


class _FakeResult:
    def __init__(self, success=True, markdown="", error_message=""):
        self.success = success
        self.markdown = markdown
        self.error_message = error_message


class _FakeCrawler:
    def __init__(self, result):
        self._result = result

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def arun(self, url, config):
        return self._result


def _fake_crawler_class(result):
    def factory(*args, **kwargs):
        return _FakeCrawler(result)

    return factory


class TestRenderUrlToMarkdown:
    def test_refuses_unsafe_urls_before_ever_crawling(self, monkeypatch):
        def poison_pill(url):
            raise AssertionError("must not crawl a URL that failed the SSRF guard")

        monkeypatch.setattr(web_crawler, "_crawl", lambda url: poison_pill(url))

        try:
            web_crawler.render_url_to_markdown("http://example.com")
        except UnsafeURLError:
            pass
        else:
            raise AssertionError("expected UnsafeURLError")

    def test_returns_the_crawled_markdown(self, monkeypatch):
        async def fake_crawl(url):
            return "# Hello"

        monkeypatch.setattr(web_crawler, "_crawl", fake_crawl)

        result = web_crawler.render_url_to_markdown("https://example.com")

        assert result == "# Hello"

    def test_truncates_markdown_past_the_size_cap(self, monkeypatch):
        long_text = "x" * (web_crawler._MAX_MARKDOWN_CHARS + 500)

        async def fake_crawl(url):
            return long_text

        monkeypatch.setattr(web_crawler, "_crawl", fake_crawl)

        result = web_crawler.render_url_to_markdown("https://example.com")

        assert len(result) < len(long_text)
        assert "[truncated" in result

    def test_does_not_truncate_markdown_under_the_size_cap(self, monkeypatch):
        short_text = "hello world"

        async def fake_crawl(url):
            return short_text

        monkeypatch.setattr(web_crawler, "_crawl", fake_crawl)

        result = web_crawler.render_url_to_markdown("https://example.com")

        assert result == short_text


class TestCrawl:
    def test_returns_markdown_on_success(self, monkeypatch):
        monkeypatch.setattr(
            web_crawler,
            "AsyncWebCrawler",
            _fake_crawler_class(_FakeResult(success=True, markdown="# Example")),
        )

        result = asyncio.run(web_crawler._crawl("https://example.com"))

        assert result == "# Example"

    def test_raises_crawl_failed_on_a_failed_render(self, monkeypatch):
        monkeypatch.setattr(
            web_crawler,
            "AsyncWebCrawler",
            _fake_crawler_class(
                _FakeResult(success=False, error_message="net::ERR_NAME_NOT_RESOLVED\nmore detail")
            ),
        )

        try:
            asyncio.run(web_crawler._crawl("https://nonexistent.invalid"))
        except web_crawler.CrawlFailed as exc:
            assert "ERR_NAME_NOT_RESOLVED" in str(exc)
            assert "more detail" not in str(exc)  # only the first line is surfaced
        else:
            raise AssertionError("expected CrawlFailed")

    def test_treats_a_missing_error_message_as_unknown_error(self, monkeypatch):
        monkeypatch.setattr(
            web_crawler,
            "AsyncWebCrawler",
            _fake_crawler_class(_FakeResult(success=False, error_message="")),
        )

        try:
            asyncio.run(web_crawler._crawl("https://example.com"))
        except web_crawler.CrawlFailed as exc:
            assert "unknown error" in str(exc)
        else:
            raise AssertionError("expected CrawlFailed")
