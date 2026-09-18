"""Tests for app/ingestion/ingestor.py — mocks embed_texts/embed_sparse_batch/
qdrant_store.upsert (same boundary tests/agent/test_tools.py mocks for the
single-item embed_text/embed_sparse), plus socket.getaddrinfo/httpx for
ingest_url, so these stay hermetic (no live network, no live Qdrant) like the
rest of the suite.

`ingest_text`/`ingest_file`/`ingest_url` are `async def` now (they await
`embed_texts`/`qdrant_store.upsert`, both real I/O against
`AsyncOpenAI`/`AsyncQdrantClient` clients now), so every call below runs
through `asyncio.run(...)`, this repo's established pattern for exercising
async code from a plain `def test_...`. `embed_sparse_batch` stays a plain
sync mock — that leg is genuinely local ONNX compute, unchanged.
"""

import httpx
import pytest

from app.ingestion import ingestor
from app.retrieval import qdrant_store
from tests.conftest import TEST_CTX


def _mock_embeddings(monkeypatch):
    async def fake_embed_texts(texts):
        return [[0.1, 0.2] for _ in texts]

    monkeypatch.setattr(ingestor, "embed_texts", fake_embed_texts)
    monkeypatch.setattr(
        ingestor, "embed_sparse_batch", lambda texts: [([1, 2], [0.5, 0.5]) for _ in texts]
    )


def _mock_upsert(monkeypatch, captured, key="points"):
    async def fake_upsert(points):
        captured[key] = points

    monkeypatch.setattr(qdrant_store, "upsert", fake_upsert)


class TestIngestText:
    async def test_refuses_without_ctx(self, monkeypatch):
        _mock_embeddings(monkeypatch)
        with pytest.raises(ingestor.IngestRefused):
            await ingestor.ingest_text("some content", title="T", ctx=None)

    async def test_blank_text_writes_nothing_and_is_not_an_error(self, monkeypatch):
        _mock_embeddings(monkeypatch)
        captured = []

        async def fake_upsert(points):
            captured.append(points)

        monkeypatch.setattr(qdrant_store, "upsert", fake_upsert)

        count = await ingestor.ingest_text("   ", title="T", ctx=TEST_CTX)

        assert count == 0
        assert captured == []

    async def test_writes_one_point_per_child_chunk_with_parent_fields(self, monkeypatch):
        _mock_embeddings(monkeypatch)
        captured = {}
        _mock_upsert(monkeypatch, captured)

        text = "Paragraph one about checkpointers.\n\nParagraph two about Qdrant."
        count = await ingestor.ingest_text(text, title="My Doc", ctx=TEST_CTX, source="text", topic="langgraph")

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

    async def test_sparse_embedding_failure_degrades_to_dense_only(self, monkeypatch):
        async def fake_embed_texts(texts):
            return [[0.1] for _ in texts]

        monkeypatch.setattr(ingestor, "embed_texts", fake_embed_texts)

        def failing_sparse_batch(texts):
            raise RuntimeError("model not loaded")

        monkeypatch.setattr(ingestor, "embed_sparse_batch", failing_sparse_batch)
        captured = {}
        _mock_upsert(monkeypatch, captured)

        await ingestor.ingest_text("some content to ingest", title="T", ctx=TEST_CTX)

        assert "sparse" not in captured["points"][0].vector
        assert captured["points"][0].vector["dense"] == [0.1]

    async def test_embeds_all_chunks_in_one_batched_call_each(self, monkeypatch):
        """The actual point of this change — proves the speedup, not just
        the shape: however many chunks a document produces, embed_texts/
        embed_sparse_batch are each called exactly ONCE, not once per
        chunk (see embed_texts's own docstring for why that matters)."""
        dense_calls = []
        sparse_calls = []

        async def fake_embed_texts(texts):
            dense_calls.append(texts)
            return [[0.1] for _ in texts]

        monkeypatch.setattr(ingestor, "embed_texts", fake_embed_texts)
        monkeypatch.setattr(
            ingestor,
            "embed_sparse_batch",
            lambda texts: sparse_calls.append(texts) or [([1], [0.5]) for _ in texts],
        )

        async def fake_upsert(points):
            pass

        monkeypatch.setattr(qdrant_store, "upsert", fake_upsert)

        text = "Paragraph one about checkpointers.\n\nParagraph two about Qdrant."
        count = await ingestor.ingest_text(text, title="T", ctx=TEST_CTX)

        assert len(dense_calls) == 1
        assert len(sparse_calls) == 1
        assert len(dense_calls[0]) == count
        assert len(sparse_calls[0]) == count


class TestIngestFile:
    async def test_refuses_unsupported_file_types(self, tmp_path):
        p = tmp_path / "doc.pdf"
        p.write_bytes(b"%PDF-1.4 fake")
        with pytest.raises(ingestor.IngestRefused):
            await ingestor.ingest_file(str(p), ctx=TEST_CTX)

    async def test_ingests_a_txt_file(self, tmp_path, monkeypatch):
        _mock_embeddings(monkeypatch)
        captured = {}
        _mock_upsert(monkeypatch, captured)

        p = tmp_path / "notes.txt"
        p.write_text("Some notes about the project.")
        count = await ingestor.ingest_file(str(p), ctx=TEST_CTX)

        assert count >= 1
        assert captured["points"][0].payload["source"] == "file:notes.txt"
        assert captured["points"][0].payload["title"] == "notes"

    async def test_ingests_a_md_file(self, tmp_path, monkeypatch):
        _mock_embeddings(monkeypatch)

        async def fake_upsert(points):
            pass

        monkeypatch.setattr(qdrant_store, "upsert", fake_upsert)

        p = tmp_path / "readme.md"
        p.write_text("# Title\n\nSome markdown content.")
        count = await ingestor.ingest_file(str(p), ctx=TEST_CTX)

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

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, headers=None):
        if self._raise_on_get:
            raise self._raise_on_get
        return self._response


class TestIngestUrl:
    async def test_refuses_before_any_fetch_when_url_is_unsafe(self, monkeypatch):
        def always_unsafe(url):
            raise ingestor.IngestRefused("blocked")

        monkeypatch.setattr(ingestor, "_assert_safe_url", always_unsafe)
        with pytest.raises(ingestor.IngestRefused):
            await ingestor.ingest_url("https://blocked.example.com", ctx=TEST_CTX)

    async def test_strips_html_before_ingesting(self, monkeypatch):
        monkeypatch.setattr(ingestor, "_assert_safe_url", lambda url: None)
        html = "<html><body><script>evil()</script><p>Real content here.</p></body></html>"
        fake_response = _FakeResponse(
            status_code=200,
            content=html.encode(),
            text=html,
            headers={"content-type": "text/html"},
        )
        monkeypatch.setattr(
            ingestor.httpx, "AsyncClient", lambda **kw: _FakeClient(fake_response)
        )
        captured = {}

        async def fake_ingest_text(text, title, ctx, source="text", topic=None):
            captured["text"] = text
            captured["title"] = title
            captured["source"] = source
            return 1

        monkeypatch.setattr(ingestor, "ingest_text", fake_ingest_text)

        await ingestor.ingest_url("https://example.com/page", ctx=TEST_CTX)

        assert "Real content here." in captured["text"]
        assert "evil()" not in captured["text"]
        assert captured["source"] == "url:https://example.com/page"

    async def test_refuses_when_response_exceeds_size_limit(self, monkeypatch):
        monkeypatch.setattr(ingestor, "_assert_safe_url", lambda url: None)
        big = b"x" * (ingestor._MAX_URL_BYTES + 1)
        fake_response = _FakeResponse(status_code=200, content=big, text="x", headers={})
        monkeypatch.setattr(
            ingestor.httpx, "AsyncClient", lambda **kw: _FakeClient(fake_response)
        )

        with pytest.raises(ingestor.IngestRefused):
            await ingestor.ingest_url("https://example.com/huge", ctx=TEST_CTX)

    async def test_refuses_on_http_error_status(self, monkeypatch):
        monkeypatch.setattr(ingestor, "_assert_safe_url", lambda url: None)
        fake_response = _FakeResponse(status_code=404, content=b"", text="", headers={})
        monkeypatch.setattr(
            ingestor.httpx, "AsyncClient", lambda **kw: _FakeClient(fake_response)
        )

        with pytest.raises(ingestor.IngestRefused):
            await ingestor.ingest_url("https://example.com/missing", ctx=TEST_CTX)

    async def test_refuses_on_transport_error(self, monkeypatch):
        monkeypatch.setattr(ingestor, "_assert_safe_url", lambda url: None)
        monkeypatch.setattr(
            ingestor.httpx,
            "AsyncClient",
            lambda **kw: _FakeClient(None, raise_on_get=httpx.ConnectError("refused")),
        )

        with pytest.raises(ingestor.IngestRefused):
            await ingestor.ingest_url("https://example.com/down", ctx=TEST_CTX)

    async def test_does_not_follow_redirects(self, monkeypatch):
        """SSRF-relevant: a validated URL redirecting to an unvalidated one
        must not be silently followed — see _assert_safe_url's docstring."""
        monkeypatch.setattr(ingestor, "_assert_safe_url", lambda url: None)
        captured_kwargs = {}

        def fake_client(**kwargs):
            captured_kwargs.update(kwargs)
            return _FakeClient(_FakeResponse(status_code=200, content=b"x", text="x"))

        monkeypatch.setattr(ingestor.httpx, "AsyncClient", fake_client)

        async def fake_ingest_text(*a, **kw):
            return 0

        monkeypatch.setattr(ingestor, "ingest_text", fake_ingest_text)

        await ingestor.ingest_url("https://example.com/page", ctx=TEST_CTX)

        assert captured_kwargs["follow_redirects"] is False
