"""Gives a test its own request stream and consumer group on a shared Redis.

Each module that reads the stream's name or the group bound it by name (`from app.ports.outbound.jobs import ...`), so patching the module that defines them reaches
none of them. This patches every place that looks one up, and the client singleton, so a test started against a container does not read the production names.
"""
import uuid

from app.adapters.inbound.workers import agent_worker, streams
from app.adapters.outbound.redis import queue
from app.platform import redis_client
from app.ports.outbound import jobs


def use_private_requests_stream(monkeypatch, redis_url: str) -> tuple[str, str]:
    """Points the queue at `redis_url` with a fresh client, and every lookup of the requests stream and consumer group at unique names.
    Every domain collapses onto the one stream (the tests using this only run the default domain). Returns `(stream, group)`."""
    stream = f"agent:requests:test:{uuid.uuid4()}"
    group = f"agent-workers:test:{uuid.uuid4()}"
    monkeypatch.setattr(redis_client, "REDIS_URL", redis_url)
    monkeypatch.setattr(redis_client, "_client", None)  # get_client() is a lazy singleton
    for module in (jobs, queue, streams, agent_worker):
        monkeypatch.setattr(module, "requests_stream_key", lambda domain="ecorp": stream)
    for module in (jobs, streams, agent_worker):
        monkeypatch.setattr(module, "CONSUMER_GROUP", group)
    return stream, group
