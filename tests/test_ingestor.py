"""Tests for app/ingestor.py — mocks embed_text/embed_sparse/qdrant_store.upsert
(same boundary tests/test_tools.py mocks), plus socket.getaddrinfo/httpx for
ingest_url, so these stay hermetic (no live network, no live Qdrant) like the
rest of the suite.
"""
import httpx
import pytest

from app import ingestor, qdrant_store
from tests.conftest import TEST_CTX


def _mock_embeddings(monkeypatch):
    monkeypatch.setattr(ingestor, "embed_text", lambda text: [0.1, 0.2])
    monkeypatch.setattr(ingestor, "embed_sparse", lambda text: ([1, 2], [0.5, 0.5]))


class TestIngestText:
    def test_refuses_without_ctx(self, monkeypatch):
        _mock_embeddings(monkeypatch)
        with pytest.raises(ingestor.IngestRefused):
            ingestor.ingest_text("some content", title="T", ctx=None)

    def test_blank_text_writes_nothing_and_is_not_an_error(self, monkeypatch):
        _mock_embeddings(monkeypatch)
        captured = []
        monkeypatch.setattr(qdrant_store, "upsert", lambda points: captured.append(points))

        count = ingestor.ingest_text("   ", title="T", ctx=TEST_CTX)

        assert count == 0
        assert captured == []

    def test_writes_one_point_per_child_chunk_with_parent_fields(self, monkeypatch):
        _mock_embeddings(monkeypatch)
        captured = {}
        monkeypatch.setattr(qdrant_store, "upsert", lambda points: captured.update(points=points))

        text = "Paragraph one about checkpointers.\n\nParagraph two about Qdrant."
        count = ingestor.ingest_text(
            text, title="My Doc", ctx=TEST_CTX, source="text", topic="langgraph"
        )

        assert count == len(captured["points"])
        assert count >= 1
        for point in captured["points"]:
            assert point.payload["tenant"] == TEST_CTX["tenant"]
            assert point.payload["ingested_by"] == TEST_CTX["principal"]
            assert point.payload["kind"] == "document"
            assert point.payload["title"] == "My Doc"
            assert point.payload["topic"] == "langgraph"
            assert "parent_id" in point.payload
            assert "parent_text" in point.payload

    def test_sparse_embedding_failure_degrades_to_dense_only(self, monkeypatch):
        monkeypatch.setattr(ingestor, "embed_text", lambda text: [0.1])

        def failing_sparse(text):
            raise RuntimeError("model not loaded")

        monkeypatch.setattr(ingestor, "embed_sparse", failing_sparse)
        captured = {}
        monkeypatch.setattr(qdrant_store, "upsert", lambda points: captured.update(points=points))

        ingestor.ingest_text("some content to ingest", title="T", ctx=TEST_CTX)

        assert "sparse" not in captured["points"][0].vector
        assert captured["points"][0].vector["dense"] == [0.1]


class TestIngestFile:
    def test_refuses_unsupported_file_types(self, tmp_path):
        p = tmp_path / "doc.pdf"
        p.write_bytes(b"%PDF-1.4 fake")
        with pytest.raises(ingestor.IngestRefused):
            ingestor.ingest_file(str(p), ctx=TEST_CTX)

    def test_ingests_a_txt_file(self, tmp_path, monkeypatch):
        _mock_embeddings(monkeypatch)
        captured = {}
        monkeypatch.setattr(qdrant_store, "upsert", lambda points: captured.update(points=points))

        p = tmp_path / "notes.txt"
        p.write_text("Some notes about the project.")
        count = ingestor.ingest_file(str(p), ctx=TEST_CTX)

        assert count >= 1
        assert captured["points"][0].payload["source"] == "file:notes.txt"
        assert captured["points"][0].payload["title"] == "notes"

    def test_ingests_a_md_file(self, tmp_path, monkeypatch):
        _mock_embeddings(monkeypatch)
        monkeypatch.setattr(qdrant_store, "upsert", lambda points: None)

        p = tmp_path / "readme.md"
        p.write_text("# Title\n\nSome markdown content.")
        count = ingestor.ingest_file(str(p), ctx=TEST_CTX)

        assert count >= 1


class TestAssertSafeUrl:
    def test_rejects_non_https_scheme(self):
        with pytest.raises(ingestor.IngestRefused):
            ingestor._assert_safe_url("http://example.com")

    def test_rejects_a_url_resolving_to_a_private_address(self, monkeypatch):
        monkeypatch.setattr(
            ingestor.socket,
            "getaddrinfo",
            lambda host, port: [(2, 1, 6, "", ("10.0.0.5", 0))],
        )
        with pytest.raises(ingestor.IngestRefused):
            ingestor._assert_safe_url("https://internal.example.com")

    def test_rejects_a_url_resolving_to_loopback(self, monkeypatch):
        monkeypatch.setattr(
            ingestor.socket,
            "getaddrinfo",
            lambda host, port: [(2, 1, 6, "", ("127.0.0.1", 0))],
        )
        with pytest.raises(ingestor.IngestRefused):
            ingestor._assert_safe_url("https://localhost.example.com")

    def test_rejects_if_any_resolved_address_is_private_even_if_others_are_public(
        self, monkeypatch
    ):
        monkeypatch.setattr(
            ingestor.socket,
            "getaddrinfo",
            lambda host, port: [
                (2, 1, 6, "", ("93.184.216.34", 0)),  # public
                (2, 1, 6, "", ("192.168.1.1", 0)),  # private
            ],
        )
        with pytest.raises(ingestor.IngestRefused):
            ingestor._assert_safe_url("https://mixed.example.com")

    def test_allows_a_url_resolving_only_to_public_addresses(self, monkeypatch):
        monkeypatch.setattr(
            ingestor.socket,
            "getaddrinfo",
            lambda host, port: [(2, 1, 6, "", ("93.184.216.34", 0))],
        )
        ingestor._assert_safe_url("https://example.com")  # must not raise

    def test_rejects_unresolvable_host(self, monkeypatch):
        import socket as socket_module

        def raise_gaierror(host, port):
            raise socket_module.gaierror("nodename nor servname provided")

        monkeypatch.setattr(ingestor.socket, "getaddrinfo", raise_gaierror)
        with pytest.raises(ingestor.IngestRefused):
            ingestor._assert_safe_url("https://nonexistent.invalid")


class _FakeResponse:
    def __init__(self, status_code=200, content=b"", text="", headers=None):
        self.status_code = status_code
        self.content = content
        self.text = text
        self.headers = headers or {}


class _FakeClient:
    def __init__(self, response, *, raise_on_get=None):
        self._response = response
        self._raise_on_get = raise_on_get

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url, headers=None):
        if self._raise_on_get:
            raise self._raise_on_get
        return self._response


class TestIngestUrl:
    def test_refuses_before_any_fetch_when_url_is_unsafe(self, monkeypatch):
        def always_unsafe(url):
            raise ingestor.IngestRefused("blocked")

        monkeypatch.setattr(ingestor, "_assert_safe_url", always_unsafe)
        with pytest.raises(ingestor.IngestRefused):
            ingestor.ingest_url("https://blocked.example.com", ctx=TEST_CTX)

    def test_strips_html_before_ingesting(self, monkeypatch):
        monkeypatch.setattr(ingestor, "_assert_safe_url", lambda url: None)
        html = "<html><body><script>evil()</script><p>Real content here.</p></body></html>"
        fake_response = _FakeResponse(
            status_code=200,
            content=html.encode(),
            text=html,
            headers={"content-type": "text/html"},
        )
        monkeypatch.setattr(
            ingestor.httpx, "Client", lambda **kw: _FakeClient(fake_response)
        )
        captured = {}

        def fake_ingest_text(text, title, ctx, source="text", topic=None):
            captured["text"] = text
            captured["title"] = title
            captured["source"] = source
            return 1

        monkeypatch.setattr(ingestor, "ingest_text", fake_ingest_text)

        ingestor.ingest_url("https://example.com/page", ctx=TEST_CTX)

        assert "Real content here." in captured["text"]
        assert "evil()" not in captured["text"]
        assert captured["source"] == "url:https://example.com/page"

    def test_refuses_when_response_exceeds_size_limit(self, monkeypatch):
        monkeypatch.setattr(ingestor, "_assert_safe_url", lambda url: None)
        big = b"x" * (ingestor._MAX_URL_BYTES + 1)
        fake_response = _FakeResponse(status_code=200, content=big, text="x", headers={})
        monkeypatch.setattr(
            ingestor.httpx, "Client", lambda **kw: _FakeClient(fake_response)
        )

        with pytest.raises(ingestor.IngestRefused):
            ingestor.ingest_url("https://example.com/huge", ctx=TEST_CTX)

    def test_refuses_on_http_error_status(self, monkeypatch):
        monkeypatch.setattr(ingestor, "_assert_safe_url", lambda url: None)
        fake_response = _FakeResponse(status_code=404, content=b"", text="", headers={})
        monkeypatch.setattr(
            ingestor.httpx, "Client", lambda **kw: _FakeClient(fake_response)
        )

        with pytest.raises(ingestor.IngestRefused):
            ingestor.ingest_url("https://example.com/missing", ctx=TEST_CTX)

    def test_refuses_on_transport_error(self, monkeypatch):
        monkeypatch.setattr(ingestor, "_assert_safe_url", lambda url: None)
        monkeypatch.setattr(
            ingestor.httpx,
            "Client",
            lambda **kw: _FakeClient(None, raise_on_get=httpx.ConnectError("refused")),
        )

        with pytest.raises(ingestor.IngestRefused):
            ingestor.ingest_url("https://example.com/down", ctx=TEST_CTX)

    def test_does_not_follow_redirects(self, monkeypatch):
        """SSRF-relevant: a validated URL redirecting to an unvalidated one
        must not be silently followed — see _assert_safe_url's docstring."""
        monkeypatch.setattr(ingestor, "_assert_safe_url", lambda url: None)
        captured_kwargs = {}

        def fake_client(**kwargs):
            captured_kwargs.update(kwargs)
            return _FakeClient(_FakeResponse(status_code=200, content=b"x", text="x"))

        monkeypatch.setattr(ingestor.httpx, "Client", fake_client)
        monkeypatch.setattr(ingestor, "ingest_text", lambda *a, **kw: 0)

        ingestor.ingest_url("https://example.com/page", ctx=TEST_CTX)

        assert captured_kwargs["follow_redirects"] is False
