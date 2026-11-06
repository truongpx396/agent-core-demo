"""The Redis connection the Streams queues and the Telegram channel's own state share: one client per process, built on first use.

A platform module like `sql_store.py`: it opens a connection, so it is adapter-side, in `app/platform/` (`tests/core/test_architecture_layers.py`).
"""
import redis.asyncio as redis

from app.core.config import REDIS_MAX_CONNECTIONS, REDIS_URL

# The actual shape of a redis-py Streams read response with
# decode_responses=True — [(stream_name, [(entry_id, {field: value}), ...])].
# redis-py's stubs type xread/xreadgroup far wider (can't key an overload on
# a runtime instance attribute); every client here is always constructed
# with decode_responses=True (see get_client below), so this cast just
# narrows what's unprovable to the type checker. Shared with
# app/adapters/outbound/redis/ingest_queue.py and app/adapters/inbound/workers/ingest_worker.py, which
# read streams the same way.
StreamReadResponse = list[tuple[str, list[tuple[str, dict[str, str]]]]]


_client: redis.Redis | None = None


def get_client() -> redis.Redis:
    """A separate client from `semantic_cache.py`'s (also a module-level
    singleton, but that one deliberately does NOT decode responses since it
    stores raw vector bytes) — this module's payloads are always JSON text,
    so `decode_responses=True` keeps every caller working with plain str.

    `socket_timeout=None` is deliberate: redis-py's 5s default races
    against `XREAD`/`XREADGROUP`'s own server-side `BLOCK` (both
    `read_results` and `agent_worker.py` block for 5000ms), raising
    `TimeoutError` before Redis's own block window elapses. `BLOCK` is what
    actually bounds the wait. `socket_connect_timeout` stays at its normal
    default so an unreachable Redis still fails fast on connect.

    `max_connections=REDIS_MAX_CONNECTIONS`: redis-py's default (100) is
    easy to blow through here — every `POST /chat/stream/queued` SSE
    connection holds a pooled connection for the FULL blocking-read
    duration of its turn, not a quick round trip. Verified: 250 concurrent
    requests against the unset default produced `MaxConnectionsError` past
    the ~100th (an 82% failure rate), regardless of agent_worker.py
    concurrency settings.
    """
    global _client
    if _client is None:
        _client = redis.Redis.from_url(
            REDIS_URL,
            decode_responses=True,
            socket_timeout=None,
            max_connections=REDIS_MAX_CONNECTIONS,
        )
    return _client
