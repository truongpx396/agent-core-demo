"""`HttpxPageFetcher` (`app/adapters/outbound/web/page_fetch.py`): one plain GET for the ingestion pipeline, following no redirect and bounding what it decodes."""
import httpx
import pytest

from app.adapters.outbound.web import page_fetch
from app.ports.outbound.web import PageFetchFailedError

REAL_CLIENT = httpx.AsyncClient


def _serve(monkeypatch, handler, seen=None):
    def make(**kwargs):
        if seen is not None:
            seen.update(kwargs)
        return REAL_CLIENT(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(page_fetch.httpx, "AsyncClient", make)


async def test_a_page_comes_back_with_its_status_type_text_and_size(monkeypatch):
    seen = {}
    _serve(monkeypatch, lambda request: httpx.Response(200, text="hello", headers={"content-type": "text/html"}), seen)

    page = await page_fetch.HttpxPageFetcher().fetch("https://example.com/a", max_bytes=1000)

    assert (page.status_code, page.content_type, page.text, page.size) == (200, "text/html", "hello", 5)
    assert seen["follow_redirects"] is False


async def test_a_redirect_is_returned_not_followed(monkeypatch):
    _serve(monkeypatch, lambda request: httpx.Response(302, headers={"location": "http://169.254.169.254/"}))

    page = await page_fetch.HttpxPageFetcher().fetch("https://example.com/a", max_bytes=1000)

    assert page.status_code == 302


async def test_a_body_over_the_limit_is_not_decoded_but_its_size_is_reported(monkeypatch):
    _serve(monkeypatch, lambda request: httpx.Response(200, content=b"x" * 50))

    page = await page_fetch.HttpxPageFetcher().fetch("https://example.com/a", max_bytes=10)

    assert (page.text, page.size) == ("", 50)


async def test_a_request_that_does_not_complete_raises_the_ports_error(monkeypatch):
    def refuse(request):
        raise httpx.ConnectError("refused")

    _serve(monkeypatch, refuse)

    with pytest.raises(PageFetchFailedError, match="refused"):
        await page_fetch.HttpxPageFetcher().fetch("https://example.com/a", max_bytes=10)


async def test_it_says_who_is_asking(monkeypatch):
    agents = []
    _serve(monkeypatch, lambda request: (agents.append(request.headers["user-agent"]), httpx.Response(200, text="ok"))[1])

    await page_fetch.HttpxPageFetcher().fetch("https://example.com/a", max_bytes=10)

    assert agents == ["agent-core-demo-ingestor/1.0"]
