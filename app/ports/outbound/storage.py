"""Object storage for uploaded documents: where the API puts a file and where the ingest worker reads it back.

The calls block (a network round trip to the store), so a caller on the event loop runs them with `asyncio.to_thread`. `delete_object` is
best effort and never raises: it compensates for an upload whose job then failed to queue, and a failed cleanup must not replace the failure
that caused it. The other two raise, since a missing blob or an unreachable store is a real job failure
(`app/adapters/outbound/minio/object_store.py`).
"""
from __future__ import annotations

from typing import Protocol

from app.ports.slot import Slot


class ObjectStore(Protocol):
    def upload_bytes(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        """Writes `data` under `key`, creating the bucket on first use."""
        ...

    def delete_object(self, key: str) -> None:
        """Removes `key`. Idempotent, and never raises."""
        ...

    def download_bytes(self, key: str) -> bytes:
        """The bytes under `key`. Raises for a missing key or an unreachable store."""
        ...


object_store: Slot[ObjectStore] = Slot("object_store", ObjectStore)
