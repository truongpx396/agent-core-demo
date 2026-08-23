"""Tests for app/object_store.py — the MinIO wrapper backing the
production ingestion pipeline (GRAPH_PATTERNS.md roadmap item #1). Same
"assert the call, not a live service result" approach
tests/test_sql_store.py already uses for Postgres, applied to MinIO
instead — no live MinIO needed for these; live verification happens
separately (`make up` + a real upload round trip).
"""
import io

import pytest

from app import object_store


class _FakeResponse:
    def __init__(self, data: bytes):
        self._data = data
        self.closed = False
        self.released = False

    def read(self):
        return self._data

    def close(self):
        self.closed = True

    def release_conn(self):
        self.released = True


class _FakeMinioClient:
    def __init__(self, bucket_exists=True):
        self._bucket_exists = bucket_exists
        self.made_buckets = []
        self.put_calls = []
        self.get_calls = []
        self._stored: dict[str, bytes] = {}

    def bucket_exists(self, name):
        return self._bucket_exists

    def make_bucket(self, name):
        self.made_buckets.append(name)
        self._bucket_exists = True

    def put_object(self, bucket, object_name, data, length, content_type=None, **kw):
        content = data.read() if hasattr(data, "read") else data
        self.put_calls.append(
            {"bucket": bucket, "object_name": object_name, "length": length, "content_type": content_type}
        )
        self._stored[object_name] = content

    def get_object(self, bucket, object_name, **kw):
        self.get_calls.append({"bucket": bucket, "object_name": object_name})
        return _FakeResponse(self._stored.get(object_name, b""))


@pytest.fixture(autouse=True)
def reset_singletons(monkeypatch):
    monkeypatch.setattr(object_store, "_client", None)
    monkeypatch.setattr(object_store, "_bucket_ready", False)


class TestEnsureBucket:
    def test_creates_the_bucket_when_it_does_not_exist(self, monkeypatch):
        fake = _FakeMinioClient(bucket_exists=False)
        monkeypatch.setattr(object_store, "get_client", lambda: fake)

        object_store.ensure_bucket(fake)

        assert fake.made_buckets == [object_store.MINIO_BUCKET]

    def test_does_not_recreate_an_existing_bucket(self, monkeypatch):
        fake = _FakeMinioClient(bucket_exists=True)
        monkeypatch.setattr(object_store, "get_client", lambda: fake)

        object_store.ensure_bucket(fake)

        assert fake.made_buckets == []

    def test_is_idempotent_within_one_process_only_checks_once(self, monkeypatch):
        """Second call shouldn't even call bucket_exists again — matches
        app/qdrant_store.py::ensure_collection / app/semantic_cache.py's
        _ensure_index "checked once per process" shape."""
        fake = _FakeMinioClient(bucket_exists=False)
        calls = []
        original = fake.bucket_exists
        fake.bucket_exists = lambda name: (calls.append(name), original(name))[1]
        monkeypatch.setattr(object_store, "get_client", lambda: fake)

        object_store.ensure_bucket(fake)
        object_store.ensure_bucket(fake)

        assert len(calls) == 1


class TestUploadBytes:
    def test_writes_to_the_shared_bucket_with_the_given_key_and_content_type(self, monkeypatch):
        fake = _FakeMinioClient(bucket_exists=True)
        monkeypatch.setattr(object_store, "get_client", lambda: fake)

        object_store.upload_bytes("acme/abc123-report.pdf", b"pdf-bytes-here", "application/pdf")

        assert len(fake.put_calls) == 1
        call = fake.put_calls[0]
        assert call["bucket"] == object_store.MINIO_BUCKET
        assert call["object_name"] == "acme/abc123-report.pdf"
        assert call["length"] == len(b"pdf-bytes-here")
        assert call["content_type"] == "application/pdf"

    def test_ensures_the_bucket_exists_before_writing(self, monkeypatch):
        fake = _FakeMinioClient(bucket_exists=False)
        monkeypatch.setattr(object_store, "get_client", lambda: fake)

        object_store.upload_bytes("k", b"data")

        assert fake.made_buckets == [object_store.MINIO_BUCKET]
        assert len(fake.put_calls) == 1


class TestDownloadBytes:
    def test_returns_the_stored_bytes(self, monkeypatch):
        fake = _FakeMinioClient(bucket_exists=True)
        monkeypatch.setattr(object_store, "get_client", lambda: fake)
        object_store.upload_bytes("k1", b"hello world")

        result = object_store.download_bytes("k1")

        assert result == b"hello world"

    def test_closes_and_releases_the_connection_even_on_success(self, monkeypatch):
        fake = _FakeMinioClient(bucket_exists=True)
        monkeypatch.setattr(object_store, "get_client", lambda: fake)
        object_store.upload_bytes("k1", b"data")

        # Capture the actual response object to inspect its cleanup calls.
        captured = {}
        real_get_object = fake.get_object

        def spying_get_object(bucket, object_name, **kw):
            resp = real_get_object(bucket, object_name, **kw)
            captured["resp"] = resp
            return resp

        fake.get_object = spying_get_object

        object_store.download_bytes("k1")

        assert captured["resp"].closed is True
        assert captured["resp"].released is True

    def test_closes_and_releases_the_connection_even_on_a_read_failure(self, monkeypatch):
        class _RaisingResponse(_FakeResponse):
            def read(self):
                raise RuntimeError("connection reset mid-read")

        class _ClientReturningRaisingResponse(_FakeMinioClient):
            def get_object(self, bucket, object_name, **kw):
                self._last = _RaisingResponse(b"")
                return self._last

        fake = _ClientReturningRaisingResponse(bucket_exists=True)
        monkeypatch.setattr(object_store, "get_client", lambda: fake)

        with pytest.raises(RuntimeError):
            object_store.download_bytes("k1")

        assert fake._last.closed is True
        assert fake._last.released is True
