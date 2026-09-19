"""Object storage for uploaded documents (production ingestion pipeline) —
a thin wrapper around the official MinIO SDK, not boto3: smaller,
purpose-built for put/get/bucket-exists against one self-hosted
S3-compatible target, avoiding boto3's much larger AWS SDK tree.

MinIO (docker-compose's `minio` service), not a local-disk volume: real
blob-storage semantics (bucket/key model, multi-instance-ready) —
consistent with this app's self-hosted-backing-store posture elsewhere
(Qdrant, Redis Stack, Postgres).

Lazy client + lazy bucket creation (`ensure_bucket()`, idempotent) — same
shape as `qdrant_store.py`/`semantic_cache.py`'s own backing-store setup,
so importing this module never implies a network dependency.
"""
import io
import logging

from minio import Minio

from app.core.config import (
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
    reaches it first — same shape as `qdrant_store.py::ensure_collection`
    and `semantic_cache.py::_ensure_index`."""
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
    unreachable MinIO — unlike `semantic_cache.py`'s read path, a failure
    here means the caller (`ingest_worker.py`) must report a real job
    failure, not silently skip."""
    client = get_client()
    response = client.get_object(MINIO_BUCKET, key)
    try:
        return response.read()
    finally:
        response.close()
        response.release_conn()
