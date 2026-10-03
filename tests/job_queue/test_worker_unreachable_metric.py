"""A pool of workers that answers nothing must be visible to an alert.

`read_results` bounds the wait for the FIRST event of a job (`first_event_deadline_
seconds`) and, on expiry, yields an `error` event to whoever is reading. That turns
a hang into an error for ONE caller — but nothing recorded it, so a total outage (no
agent-worker running for a domain, or no ingest-worker at all) produced a stream of
identical per-request errors and no signal an operator could be paged on. Every
request failed and no alert could fire (spec 008, B21: read from the code, no metric
at all on that path).

`agent_worker_unreachable_total{queue}` is the signal, and `WorkerUnreachable` in
observability/prometheus/alerts.yml is the rule on it
(tests/core/test_alert_rules.py checks the rule exists and watches a real metric).
Only the expiry counts: a job that was picked up, however slow, is not "unreachable".
"""
import pytest

from app.core import metrics
from app.ingestion import ingest_queue
from app.job_queue import queue
from tests.conftest import metric_value
from tests.job_queue.test_queue import FakeRedis


def _unreachable(queue_name: str) -> float:
    return metric_value(metrics.agent_worker_unreachable_total, queue=queue_name)


async def _drain(generator):
    return [event async for event in generator]


async def test_an_agent_job_nobody_picks_up_counts_as_an_unreachable_worker():
    before = _unreachable("agent")

    events = await _drain(queue.read_results(FakeRedis(), "r1", first_event_deadline_seconds=0.05))

    assert [e["type"] for e in events] == ["error"]
    assert _unreachable("agent") - before == 1


async def test_an_ingest_job_nobody_picks_up_counts_as_an_unreachable_worker():
    before = _unreachable("ingest")

    events = await _drain(ingest_queue.read_results(FakeRedis(), "j1", first_event_deadline_seconds=0.05))

    assert [e["type"] for e in events] == ["error"]
    assert _unreachable("ingest") - before == 1


async def test_each_queue_counts_under_its_own_label():
    agent_before, ingest_before = _unreachable("agent"), _unreachable("ingest")

    await _drain(queue.read_results(FakeRedis(), "r1", first_event_deadline_seconds=0.05))

    assert _unreachable("ingest") == ingest_before and _unreachable("agent") - agent_before == 1


@pytest.mark.parametrize(
    ("module", "publish", "kind"),
    [(queue, queue.publish_result, "agent"), (ingest_queue, ingest_queue.publish_result, "ingest")],
    ids=["agent", "ingest"],
)
async def test_a_job_that_was_picked_up_is_never_counted_however_the_deadline_compares(module, publish, kind):
    """The deadline guards the wait for the first event only; a worker that
    answered is, by definition, reachable."""
    client = FakeRedis()
    await publish(client, "r1", {"type": "token", "content": "a"})
    await publish(client, "r1", {"type": "done"})
    before = _unreachable(kind)

    await _drain(module.read_results(client, "r1", first_event_deadline_seconds=0.001))

    assert _unreachable(kind) == before


async def test_a_read_with_no_deadline_never_counts_anything():
    client = FakeRedis()
    await queue.publish_result(client, "r1", {"type": "done"})
    before = _unreachable("agent")

    await _drain(queue.read_results(client, "r1"))

    assert _unreachable("agent") == before
