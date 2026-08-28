"""Object storage for uploaded documents (production ingestion pipeline)
— a thin wrapper around the official
MinIO Python SDK, not boto3: smaller, purpose-built for exactly
put/get/bucket-exists against one self-hosted S3-compatible target,
avoiding boto3's much larger multi-service AWS SDK dependency tree for a
demo that only ever talks to one bucket.

MinIO (docker-compose's `minio` service) rather than a plain local-disk
volume: real blob-storage semantics (a bucket/key model or, one day, a
multi-instance deployment where "the API process's local disk" wouldn't
even be a coherent place to write to) — consistent with this app's
"self-hosted, not a hollow stand-in" posture everywhere else (Qdrant for
vectors, Redis Stack for the semantic cache, Postgres for structured data).

Lazy client (opened on first `get_client()` call, not at import time) and
lazy bucket creation (`ensure_bucket()`, idempotent) — same shape
app/qdrant_store.py's `get_client()`/`ensure_collection()` and
app/semantic_cache.py's `_get_client()`/`_ensure_index()` already use for
their own backing stores, so importing this module never implies a
network dependency.
"""
import io
import logging

from minio import Minio

from app.config import (
    MINIO_ACCESS_KEY,
    MINIO_BUCKET,
    MINIO_ENDPOINT,
    MINIO_SECRET_KEY,
    MINIO_SECURE,
)

logger = logging.getLogger(__name__)

_client: Minio | None = None
_bucket_ready = False


def get_client() -> Minio:
    global _client
    if _client is None:
        _client = Minio(
            MINIO_ENDPOINT,
            access_key=MINIO_ACCESS_KEY,
            secret_key=MINIO_SECRET_KEY,
            secure=MINIO_SECURE,
        )
    return _client


def ensure_bucket(client: Minio | None = None) -> None:
    """Idempotent: creates the bucket on first use, in whichever process
    reaches it first — same idempotent-setup shape
    app/qdrant_store.py::ensure_collection and
    app/semantic_cache.py::_ensure_index already use."""
    global _bucket_ready
    if _bucket_ready:
        return
    client = client or get_client()
    if not client.bucket_exists(MINIO_BUCKET):
        client.make_bucket(MINIO_BUCKET)
    _bucket_ready = True


def upload_bytes(key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
    """Write `data` to `key` in the shared bucket, creating the bucket
    first if this is the first write this process has made."""
    client = get_client()
    ensure_bucket(client)
    client.put_object(MINIO_BUCKET, key, io.BytesIO(data), length=len(data), content_type=content_type)


def download_bytes(key: str) -> bytes:
    """Read `key` back out. Raises (does not degrade) on a missing key or
    an unreachable MinIO — unlike app/semantic_cache.py's read path, a
    failure here means the actual uploaded document is unavailable, which
    the caller (app/ingest_worker.py) needs to know about and report as a
    real job failure, not silently skip."""
    client = get_client()
    response = client.get_object(MINIO_BUCKET, key)
    try:
        return response.read()
    finally:
        response.close()
        response.release_conn()
