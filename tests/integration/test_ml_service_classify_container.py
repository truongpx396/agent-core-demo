"""POST /classify against the REAL image: real models, real Hub cache layout, head files mounted the way an operator mounts them.

The hermetic tests (tests/ml_service/test_classify_failover.py) prove the routing and the refusal rules with fake models. What only a real
container can show, and this file does:

- the revision a head is bound to is the commit the Hub cache really holds (`snapshots/<sha>/…`), so a head trained on the pinned
  revision loads and one trained on any other is refused (specs/014 research.md C1, the claim that was "to verify");
- `/classify` scores the SAME vectors `/v1/embeddings` serves: an independent computation from the served vectors reproduces the score;
- with no head, and with a head whose JSON says `"accepted": false`, the answer is HTTP 200 `served_by: "none"` (FR-038);
- with the primary unable to load, the fallback answers and says so.

The heads here are synthetic (a fixed random direction) and are written `"accepted": true` BY THE TEST. The trainer never does that
(scripts/model_eval/head.py), so no real head is involved and nothing here says anything about routing quality.
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

from app.core import routing_advice
from tests.conftest import REAL_ADVISE

pytestmark = pytest.mark.integration

TAG = "agent-core-demo-ml-service:test"
PRIMARY, FALLBACK = "bge-small-en-v1.5", "all-MiniLM-L6-v2"
REPOS = {PRIMARY: "BAAI/bge-small-en-v1.5", FALLBACK: "sentence-transformers/all-MiniLM-L6-v2"}
PINNED = {PRIMARY: "5c38ec7c405ec4b44b94cc5a9bb96e735b38267a", FALLBACK: "1110a243fdf4706b3f48f1d95db1a4f5529b4d41"}
DIM = 384
TEXTS = ["How do I reset my password?", "Prove that the square root of two is irrational, then generalise to every prime."]


def sigmoid(x):
    return 1 / (1 + np.exp(-x))


def synthetic_head(directory: Path, model: str, *, accepted: bool, revision: str | None = None, seed: int = 7) -> tuple[np.ndarray, float]:
    rng = np.random.default_rng(seed)
    w = (rng.normal(size=DIM) / np.sqrt(DIM) * 8).astype(np.float32)
    b = np.float32(0.25)
    np.savez(directory / f"head-{model}.npz", w=w, b=b)
    (directory / f"head-{model}.json").write_text(json.dumps({
        "schema_version": 1, "model": model, "model_repo": REPOS[model], "model_revision": revision or PINNED[model],
        "model_file": "onnx/model.onnx", "dim": DIM, "head_version": f"synthetic-{model}", "threshold": {"value": 0.5}, "accepted": accepted,
    }))
    return w, float(b)


@contextmanager
def container(ml_service, heads: Path, registry: Path | None = None) -> Iterator[str]:
    from testcontainers.core.container import DockerContainer

    started = DockerContainer(TAG).with_exposed_ports(80).with_volume_mapping(str(heads), "/app/heads", "ro")
    if registry is not None:
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
            pytest.fail("the container's models never settled")
        yield url
    finally:
        started.stop()


def classify(url: str, texts=TEXTS) -> dict:
    return httpx.post(f"{url}/classify", json={"task": "route", "texts": texts}, timeout=30).json()


def served_vectors(url: str, texts=TEXTS, model=PRIMARY) -> np.ndarray:
    body = httpx.post(f"{url}/v1/embeddings", json={"model": model, "input": texts}, timeout=60).json()
    return np.array([d["embedding"] for d in body["data"]])


def test_with_no_head_mounted_the_default_container_gives_no_advice(ml_service_url):
    body = classify(ml_service_url)

    assert body["served_by"] == "none" and [r["decision"] for r in body["results"]] == ["none", "none"]
    classifier = httpx.get(f"{ml_service_url}/health/models", timeout=10).json()["classifier"]
    assert classifier == {"serving": "none", "heads": {PRIMARY: "absent", FALLBACK: "absent"}}


@pytest.fixture(scope="module")
def accepted_primary(ml_service, tmp_path_factory):
    """One container with an accepted head for the primary and a head for the fallback that was trained for another revision."""
    heads = tmp_path_factory.mktemp("heads")
    w, b = synthetic_head(heads, PRIMARY, accepted=True)
    synthetic_head(heads, FALLBACK, accepted=True, revision="f" * 40, seed=11)  # trained for a revision that is not the pinned one
    with container(ml_service, heads) as url:
        # A request that overruns the service's 250 ms deadline is answered by the next model, and here the next model has no usable head,
        # so the answer is "none". The first request after start-up (or any on a machine under load) can overrun: wait until the primary
        # has answered once, which is the steady state the tests below are about.
        for _ in range(30):
            if classify(url)["served_by"] == "primary":
                break
            time.sleep(1)
        yield url, w, b


def test_an_accepted_head_scores_the_vectors_the_embedding_endpoint_serves_and_binds_to_the_real_revision(accepted_primary):
    url, w, b = accepted_primary

    body = classify(url)
    classifier = httpx.get(f"{url}/health/models", timeout=10).json()["classifier"]
    vectors = served_vectors(url)

    expected = sigmoid(vectors @ w.astype(np.float64) + b)
    assert body["served_by"] == "primary" and body["model"] == PRIMARY and body["head_version"] == f"synthetic-{PRIMARY}"
    # The independent computation: the served unit vectors, the head, a sigmoid. The classifier used the same embedder, not a copy of it.
    np.testing.assert_allclose([r["score"] for r in body["results"]], expected, atol=1e-4)
    assert [r["decision"] for r in body["results"]] == ["needs_stronger" if s >= 0.5 else "local_ok" for s in expected]
    # The primary's head matches the commit the real Hub cache holds; the other was trained for a different one and is refused
    assert classifier == {"serving": "primary", "heads": {PRIMARY: "loaded", FALLBACK: "refused: revision mismatch"}}


async def test_the_whole_routing_call_from_the_app_is_measured_and_answers_nearly_every_call(accepted_primary, monkeypatch, record_property):
    """SC-009's budget is 50 ms at p95 on the reference machine (specs/014 tasks T088); that figure is MEASURED and recorded here and quoted
    in the PR, not asserted, because a CI runner's speed says nothing about the reference machine. What is asserted is the part that must hold
    anywhere: no call is an error or a timeout of the app's own deadline, and nearly all of them are answered with advice."""
    url, _w, _b = accepted_primary
    monkeypatch.setattr(routing_advice, "advise", REAL_ADVISE)  # the suite's autouse guard replaced it with "no advice"
    monkeypatch.setattr(routing_advice, "ML_SERVICE_URL", url)
    monkeypatch.setattr(routing_advice, "ROUTER_TIMEOUT_SECONDS", 0.5)

    await routing_advice.advise(TEXTS[0])  # warm-up: the first request pays for connection set-up and the first model pass
    timings, outcomes = [], []
    for i in range(200):
        started = time.perf_counter()
        advice = await routing_advice.advise(f"{TEXTS[i % 2]} ({i})")
        timings.append((time.perf_counter() - started) * 1000)
        outcomes.append(advice.outcome)

    p50, p95 = statistics.median(timings), sorted(timings)[int(len(timings) * 0.95) - 1]
    record_property("advise_p50_ms", round(p50, 1))
    record_property("advise_p95_ms", round(p95, 1))
    print(f"advise() over {len(timings)} sequential calls: p50={p50:.1f} ms p95={p95:.1f} ms")
    # No call may be an error or a timeout of the app's own deadline. A "no_advice" is the service's own 250 ms deadline being overrun with no
    # fallback head to answer (see the fixture), which a loaded machine can cause now and then; it is allowed for at most one call in ten.
    assert not {"classifier_error", "classifier_timeout"} & set(outcomes) and outcomes.count("advice") >= 180


def test_a_head_that_is_not_accepted_gives_no_advice_in_the_real_container(ml_service, tmp_path):
    synthetic_head(tmp_path, PRIMARY, accepted=False)
    synthetic_head(tmp_path, FALLBACK, accepted=False, seed=11)

    with container(ml_service, tmp_path) as url:
        body = classify(url)
        classifier = httpx.get(f"{url}/health/models", timeout=10).json()["classifier"]

    assert body["served_by"] == "none" and all(r["score"] is None for r in body["results"])
    assert classifier["heads"] == {PRIMARY: "refused: not accepted", FALLBACK: "refused: not accepted"}


def test_when_the_primary_cannot_load_the_fallback_answers_and_says_so(ml_service, tmp_path):
    synthetic_head(tmp_path, PRIMARY, accepted=True)
    w, b = synthetic_head(tmp_path, FALLBACK, accepted=True, seed=11)
    registry = tmp_path / "models.toml"
    registry.write_text(
        f'[[model]]\nname = "{PRIMARY}"\nroles = ["embed", "classify"]\nrepo = "{REPOS[PRIMARY]}"\nfile = "onnx/this-file-does-not-exist.onnx"\n'
        f'revision = "{PINNED[PRIMARY]}"\npooling = "cls"\nmax_length = 512\npad_token = "[PAD]"\nlicence = "MIT"\n\n'
        f'[[model]]\nname = "{FALLBACK}"\nroles = ["embed", "classify"]\nrepo = "{REPOS[FALLBACK]}"\nfile = "onnx/model.onnx"\n'
        f'revision = "{PINNED[FALLBACK]}"\npooling = "mean"\nmax_length = 256\npad_token = "[PAD]"\nlicence = "Apache-2.0"\n'
    )

    with container(ml_service, tmp_path, registry) as url:
        body = classify(url)
        for _ in range(60):  # a failed model is retried after a backoff, and while that retry is in flight its state is honestly `loading`
            health = httpx.get(f"{url}/health/models", timeout=10).json()
            if health["models"][PRIMARY]["state"] == "failed":
                break
            time.sleep(1)
        vectors = served_vectors(url, model=FALLBACK)

    assert health["models"][PRIMARY]["state"] == "failed" and health["classifier"]["serving"] == "fallback"
    assert body["served_by"] == "fallback" and body["model"] == FALLBACK
    np.testing.assert_allclose([r["score"] for r in body["results"]], sigmoid(vectors @ w.astype(np.float64) + b), atol=1e-4)
