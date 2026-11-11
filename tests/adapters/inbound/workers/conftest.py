"""Every event a worker publishes is held to the chat API's committed OpenAPI document.

`publish_result` is the one function through which the agent worker puts an event on a turn's results stream (the stream's own events, and the
worker's `done`, cancelled and failed-job `error`) and the ingest worker one on an upload's. What the API relays to a client is what these
publish, so an event whose shape the document does not describe fails the test that published it.
"""
import pytest

from app.adapters.inbound.workers import agent_worker, ingest_worker
from tests.openapi_conformance import event_schema, load, validator


@pytest.fixture(autouse=True)
def every_published_event_conforms_to_the_openapi_document(monkeypatch: pytest.MonkeyPatch) -> None:
    document = load("openapi.json")
    for module, path, method in ((agent_worker, "/chat/stream/queued", "post"), (ingest_worker, "/ingest/stream/{job_id}", "get")):
        check = validator(document, event_schema(document, path, method))
        original = module.publish_result

        async def publish_result(client, stream_id, event, *, _check=check, _original=original):
            _check.validate(event)
            await _original(client, stream_id, event)

        monkeypatch.setattr(module, "publish_result", publish_result)
