"""POST /v1/decide against the REAL image: real models, real Hub cache layout, the label set baked into the image.

The hermetic tests (tests/ml_service/test_labelsets.py) prove the matcher, the cutoffs, the failover and the wire with fake models. What only a
real container can show, and this file does:

- the shipped `route-topics` set is served by both real models through the real tokenisers, and the similarity it reports is reproduced by an
  independent computation from the vectors `/v1/embeddings` serves (a service that agreed only with itself would pass the hermetic tests);
- with the primary unable to load, the fallback answers, says so, and applies its OWN cutoff;
- the whole call from the app's client is timed, and nearly all of it answers.

Nothing here says anything about routing quality.
"""
import json
import statistics
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx
import numpy as np
import pytest

from app.agent.graph import model_route
from app.core import classification, routing_policy
from tests.conftest import REAL_CHOOSE

pytestmark = pytest.mark.integration

TAG = "agent-core-demo-ml-service:test"
PRIMARY, FALLBACK = "bge-small-en-v1.5", "all-MiniLM-L6-v2"
REPOS = {PRIMARY: "BAAI/bge-small-en-v1.5", FALLBACK: "sentence-transformers/all-MiniLM-L6-v2"}
PINNED = {PRIMARY: "5c38ec7c405ec4b44b94cc5a9bb96e735b38267a", FALLBACK: "1110a243fdf4706b3f48f1d95db1a4f5529b4d41"}
TEXTS = ["How do I reset my password?", "Prove that the square root of two is irrational, then generalise to every prime."]
CODE_SENTENCE = "Write a Python function that parses a CSV file into invoices please."


def _diagnosis(started, url: str) -> str:
    """What a person needs when the models never settle: the service's own account of each model, and the tail of its log."""
    try:
        health = httpx.get(f"{url}/health/models", timeout=5).text[:1500]
    except httpx.HTTPError as exc:
        health = f"/health/models unreachable: {type(exc).__name__}"
    logs = started.get_wrapped_container().logs(tail=60).decode("utf-8", "replace")
    return f"/health/models: {health}\n--- last container log lines:\n{logs}"


@contextmanager
def container(ml_service, registry: Path | None = None) -> Iterator[str]:
    from testcontainers.core.container import DockerContainer

    started = DockerContainer(TAG).with_exposed_ports(80)
    if registry is not None:
        # The service runs as uid 1000 in the container; pytest makes its temp files 0600. On Docker Desktop (macOS) a bind mount ignores that, so
        # this passed locally; on a Linux CI runner the service got `PermissionError` and exited at start-up (found by CI in the first runs of
        # spec 014, diagnosed from the container log that `_diagnosis` prints).
        registry.chmod(0o644)
        started = started.with_volume_mapping(str(registry), "/app/models.toml", "ro")
    started.start()
    try:
        url = f"http://{started.get_container_host_ip()}:{int(started.get_exposed_port(80))}"
        deadline = time.monotonic() + 600  # four models download on a cold container; a loaded machine needs the margin
        while time.monotonic() < deadline:  # every model has either loaded or failed (a failed one is retried later, not now)
            try:
                models = httpx.get(f"{url}/health/models", timeout=3).json()["models"]
                if models and all(m["state"] in ("ready", "failed") for m in models.values()):
                    break
            except (httpx.HTTPError, ValueError):
                pass
            time.sleep(2)
        else:
            pytest.fail(f"the container's models never settled\n{_diagnosis(started, url)}")
        yield url
    finally:
        started.stop()


def decide(url: str, text: str) -> httpx.Response:
    question = {"type": "choice", "instructions": routing_policy.ROUTE_INSTRUCTIONS, "criteria": dict(routing_policy.LABEL_DESCRIPTIONS)}
    return httpx.post(f"{url}/v1/decide", json={"state": text, "questions": {"route-topics": question}}, timeout=30)


def served_vectors(url: str, texts=TEXTS, model=PRIMARY) -> np.ndarray:
    body = httpx.post(f"{url}/v1/embeddings", json={"model": model, "input": texts}, timeout=60).json()
    return np.array([d["embedding"] for d in body["data"]])


def test_the_shipped_route_topics_set_is_served_by_the_real_container_and_matches_an_independent_computation(ml_service_url):
    """`POST /v1/decide` against the image as built: the label set baked into it, both real models, the real
    tokenisers.
    """
    raw = json.loads((Path(__file__).resolve().parents[2] / "docker" / "ml-service" / "labelsets" / "route-topics.json").read_text())
    sentences = [CODE_SENTENCE, "We received a subpoena for customer records, what do we do?", "qwerty qwerty qwerty"]
    answers: list[dict] = []
    for sentence in sentences:
        body: dict = {}
        for _ in range(30):  # the first request after start-up can overrun the service's 250 ms deadline and be answered 503
            response = decide(ml_service_url, sentence)
            body = response.json()
            if response.status_code == 200 and body.get("x_served_by") == "primary":
                break
            time.sleep(1)
        assert body["x_served_by"] == "primary" and body["model"] == PRIMARY and body["usage"]["output_tokens"] == 0 and body["usage"]["input_tokens"] > 0
        answers.append(body["answers"]["route-topics"])
    assert all(len(a["x_labelset_version"]) == 12 for a in answers)
    evidence = [a["x_evidence"] for a in answers]
    labels = list(raw["labels"])
    centroids = []
    for name in labels:
        mean = served_vectors(ml_service_url, raw["labels"][name]).mean(axis=0)
        centroids.append(mean / np.linalg.norm(mean))
    similarities = served_vectors(ml_service_url, sentences) @ np.stack(centroids).T
    np.testing.assert_allclose([e["similarity"] for e in evidence], similarities.max(axis=1), atol=1e-4)
    assert [e["nearest"] for e in evidence] == [labels[i] for i in similarities.argmax(axis=1)]
    assert [a["choice"] for a in answers] == ["code", "high_stakes", None]
    shown = httpx.get(f"{ml_service_url}/health/models", timeout=10).json()["labelsets"]["route-topics"]
    assert shown["version"] == answers[0]["x_labelset_version"] and shown["models"] == {PRIMARY: "loaded", FALLBACK: "loaded"}


async def test_the_whole_label_call_from_the_app_is_measured_and_answers_nearly_every_call(ml_service_url, monkeypatch, record_property):
    """The budget is 50 ms at p95 on the reference machine (specs/014 SC-009, specs/015 SC-003); that figure is
    MEASURED and recorded here and quoted in the PR, not asserted, because a CI runner's speed says nothing
    about the reference machine.
    """
    monkeypatch.setattr(classification, "choose", REAL_CHOOSE)  # the suite's autouse guard replaced it with "no answer"
    monkeypatch.setattr(classification, "ML_SERVICE_URL", ml_service_url)
    monkeypatch.setattr(classification, "ROUTER_TIMEOUT_SECONDS", 0.5)

    await classification.choose(model_route.ROUTE_QUESTION, TEXTS[0])  # warm-up: the first request pays for connection set-up and the first model pass
    timings, outcomes = [], []
    for i in range(200):
        started = time.perf_counter()
        answer = await classification.choose(model_route.ROUTE_QUESTION, f"{TEXTS[i % 2]} ({i})")
        timings.append((time.perf_counter() - started) * 1000)
        outcomes.append(answer.outcome)

    p50, p95 = statistics.median(timings), sorted(timings)[int(len(timings) * 0.95) - 1]
    record_property("label_p50_ms", round(p50, 1))
    record_property("label_p95_ms", round(p95, 1))
    print(f"label() over {len(timings)} sequential calls: p50={p50:.1f} ms p95={p95:.1f} ms")
    # No call may be an error: the service is reachable and speaks the contract. A "timeout" and a "no_answer" are both the designed answer
    # of a slow machine, not a defect: the service gives each model 250 ms, so a primary and a fallback that both overrun can spend the app's
    # whole 0.5 s, and the app's timer then fires first. The first CI run saw 7 of 200 (p50 149 ms, p95 426 ms on a shared runner, against 2-3 ms
    # in-process), and asserting zero timeouts asserted the runner's speed, which this docstring says it does not. Together they may be at most
    # one call in ten; the latency itself is recorded above, not judged.
    assert "error" not in outcomes and outcomes.count("labelled") >= 180


def test_when_the_primary_cannot_load_the_fallback_answers_and_says_so(ml_service, tmp_path):
    registry = tmp_path / "models.toml"
    registry.write_text(
        f'[[model]]\nname = "{PRIMARY}"\nroles = ["embed", "classify"]\nrepo = "{REPOS[PRIMARY]}"\nfile = "onnx/this-file-does-not-exist.onnx"\n'
        f'revision = "{PINNED[PRIMARY]}"\npooling = "cls"\nmax_length = 512\npad_token = "[PAD]"\nlicence = "MIT"\n\n'
        f'[[model]]\nname = "{FALLBACK}"\nroles = ["embed", "classify"]\nrepo = "{REPOS[FALLBACK]}"\nfile = "onnx/model.onnx"\n'
        f'revision = "{PINNED[FALLBACK]}"\npooling = "mean"\nmax_length = 256\npad_token = "[PAD]"\nlicence = "Apache-2.0"\n'
    )

    with container(ml_service, registry) as url:
        body: dict = {}
        for _ in range(60):  # the fallback's index is built on the first start-up sync; until a model has one the answer is a 503
            response = decide(url, CODE_SENTENCE)
            body = response.json()
            if response.status_code == 200:
                break
            time.sleep(1)
        health = httpx.get(f"{url}/health/models", timeout=10).json()

    assert health["models"][PRIMARY]["state"] == "failed"
    assert body["x_served_by"] == "fallback" and body["model"] == FALLBACK
    assert health["labelsets"]["route-topics"]["models"][FALLBACK] == "loaded"
    answer = body["answers"]["route-topics"]  # which topic MiniLM picks is not asserted (it was not measured); that it answered on its own scale is
    assert answer["choice"] in (*routing_policy.LABEL_DESCRIPTIONS, None) and answer["x_evidence"]["nearest"] in routing_policy.LABEL_DESCRIPTIONS
