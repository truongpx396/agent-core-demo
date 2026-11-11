"""Every event the real turn loop streams is held to the chat API's committed OpenAPI document.

`_run_graph_stream` is the one generator every turn, resume and continue goes through, and many tests under tests/agent drive it with a fake
model, so the events it yields here are the real ones: a field it adds that the document lacks (or an event type the document does not
list) fails the test that streamed it, and the document cannot describe an event the code does not send. The events built outside it (a
refusal before the graph runs) are the same envelope as its terminal `error`; the worker's own events are checked in
tests/adapters/inbound/workers/conftest.py.
"""
import contextlib

import pytest

from app.agent.runtime import stream
from tests.openapi_conformance import event_schema, load, validator


@pytest.fixture(autouse=True)
def every_streamed_event_conforms_to_the_openapi_document(monkeypatch: pytest.MonkeyPatch) -> None:
    document = load("openapi.json")
    check = validator(document, event_schema(document, "/chat/stream/queued", "post"))
    original = stream._run_graph_stream

    async def checked(*args, **kwargs):
        # aclosing: a caller that leaves early closes THIS generator, and the turn's own cleanup (the abandoned-turn metrics, the budget
        # hold) must run when it does, not whenever the inner one is collected (runtime-reliability.md, "Streaming turns").
        async with contextlib.aclosing(original(*args, **kwargs)) as events:
            async for event in events:
                check.validate(event)
                yield event

    monkeypatch.setattr(stream, "_run_graph_stream", checked)
