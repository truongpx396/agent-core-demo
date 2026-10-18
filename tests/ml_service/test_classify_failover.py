"""POST /classify: who answers, what happens when they cannot, and the rules that keep a head from doing harm (specs/014 FR-027..030).

The real app, lifespan, registry, supervisor and head files are used; only the two text models are fakes (`FakeText`), because what is
under test is the routing between them, not the ONNX pass. A fake returns a chosen vector for a chosen text, so every expected score
below is worked out by hand: with `w = [4, -4]` and `b = 0`, the text mapped to `[1, 0]` scores sigmoid(4) = 0.982 and the one mapped to
`[0, 1]` scores sigmoid(-4) = 0.018.

The rules pinned here, each with the failure it prevents:
- a head is served only if its JSON says `"accepted": true` (a trained but unvalidated head must never escalate a turn);
- a head is served only for the exact model revision it was trained on (the same dimension in another model is a different space);
- a primary that raises or overruns its deadline hands the request to the fallback, and the answer says so;
- with neither model usable the answer is HTTP 200 `served_by: "none"`, never a 5xx the app has to interpret;
- no request text reaches a log.
"""
import asyncio
import json
import logging
import math
import os
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest
from fastapi.testclient import TestClient

import ml_service.app as app_module
from ml_service.app import create_app
from ml_service.embed import Embedded
from ml_service.registry import ModelSpec
from ml_service.settings import Settings
from tests.ml_service.test_rerank_guard_contract import _guard_loader, _rerank_loader

PRIMARY, FALLBACK = "primary-m", "fallback-m"
REVISIONS = {PRIMARY: "a" * 40, FALLBACK: "b" * 40}
HARD, EASY = "a hard prompt", "an easy prompt"
VECTORS = {HARD: np.array([1.0, 0.0], dtype=np.float32), EASY: np.array([0.0, 1.0], dtype=np.float32)}


def sigmoid(x: float) -> float:
    return 1 / (1 + math.exp(-x))


def spec(name: str, *, roles=("embed", "classify")) -> ModelSpec:
    return ModelSpec(name=name, roles=roles, repo=f"org/{name}", file="onnx/model.onnx", revision=REVISIONS.get(name, "c" * 40),
                     pooling="cls", normalize=True, max_length=8, pad_token="[PAD]", licence="MIT")


def settings(head_dir: Path, **overrides) -> Settings:
    base = dict(rerank_model_name="x", guard_model_repo="x", rerank_max_batch_size=8, guard_max_batch_size=8, max_batch_wait_ms=2,
                onnx_intra_op_threads=1, model_retry_base_s=0.02, model_retry_max_s=0.05, models_enabled=(),
                router_head_dir=head_dir, classify_deadline_ms=60.0, classify_max_chars=40)
    return Settings(**{**base, **overrides})


class FakeText:
    """Stands in for an `EmbedService`: the same surface the classifier uses (`model`, `embed`, `info`, `start`, `stop`)."""

    def __init__(self, name: str, *, revision: str | None = None, dim: int = 2, mode: str = "ok", truncated: bool = False) -> None:
        self.model = SimpleNamespace(spec=spec(name), revision=revision or REVISIONS[name], dim=dim)
        self.mode = mode
        self.truncated = truncated
        self.calls: list[list[str]] = []

    def info(self) -> dict:
        return {"revision": self.model.revision, "dim": self.model.dim}

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def embed(self, texts: list[str]) -> list[Embedded]:
        self.calls.append(list(texts))
        if self.mode == "raise":
            raise RuntimeError("the session blew up")
        if self.mode == "hang":
            await asyncio.Event().wait()  # never completes: the caller's deadline is what ends it
        if self.mode == "nan":
            return [Embedded(np.array([np.nan, np.nan], dtype=np.float32), 3, False) for _ in texts]
        return [Embedded(VECTORS.get(t, np.array([3.0, 4.0], dtype=np.float32)), 3, self.truncated) for t in texts]


def write_head(directory: Path, model: str, /, *, w=(4.0, -4.0), b: float = 0.0, **overrides: Any) -> None:
    np.savez(directory / f"head-{model}.npz", w=np.array(w, dtype=np.float32), b=np.float32(b))
    meta: dict[str, Any] = {
        "schema_version": 1, "model": model, "model_repo": f"org/{model}", "model_revision": REVISIONS[model],
        "model_file": "onnx/model.onnx", "dim": 2, "head_version": f"v1-{model}", "labels": {"0": "local_ok", "1": "needs_stronger"},
        "threshold": {"value": 0.5}, "accepted": True,
    }
    (directory / f"head-{model}.json").write_text(json.dumps({**meta, **overrides}))


def make_client(head_dir: Path, *, services: dict[str, FakeText] | None = None, loaders: dict[str, Any] | None = None, **settings_kw):
    services = services or {PRIMARY: FakeText(PRIMARY), FALLBACK: FakeText(FALLBACK)}
    all_loaders = {"rerank": _rerank_loader, "prompt-guard": _guard_loader, **{n: (lambda s=s: s) for n, s in services.items()}}
    all_loaders.update(loaders or {})
    app = create_app(settings(head_dir, **settings_kw), specs=[spec(PRIMARY), spec(FALLBACK)], loaders=all_loaders)
    return TestClient(app), services


def classify(client: TestClient, texts=(HARD,), **extra):
    return client.post("/classify", json={"task": "route", "texts": list(texts), **extra})


@pytest.fixture
def heads(tmp_path: Path) -> Path:
    write_head(tmp_path, PRIMARY, w=(4.0, -4.0))
    write_head(tmp_path, FALLBACK, w=(2.0, -2.0))
    return tmp_path


# ---- the primary answers ----------------------------------------------------------------------------------------------------
def test_the_primary_answers_with_its_own_heads_score_and_version(heads):
    client, _ = make_client(heads)
    with client:
        body = classify(client, [HARD, EASY]).json()

    assert (body["served_by"], body["model"], body["head_version"]) == ("primary", PRIMARY, f"v1-{PRIMARY}")
    assert [r["index"] for r in body["results"]] == [0, 1]
    assert body["results"][0]["score"] == pytest.approx(sigmoid(4), abs=1e-6)
    assert body["results"][1]["score"] == pytest.approx(sigmoid(-4), abs=1e-6)
    assert [r["decision"] for r in body["results"]] == ["needs_stronger", "local_ok"]


def test_the_decision_follows_the_heads_recorded_threshold_and_the_score_is_unaffected(heads):
    write_head(heads, PRIMARY, w=(4.0, -4.0), threshold={"value": 0.99})
    client, _ = make_client(heads)
    with client:
        result = classify(client, [HARD]).json()["results"][0]

    assert result["score"] == pytest.approx(sigmoid(4), abs=1e-6)  # the caller decides from the score ...
    assert result["decision"] == "local_ok"  # ... the head's own threshold (0.99) is only a convenience


def test_the_score_is_taken_over_the_l2_normalised_embedding(heads):
    # the fake returns [3, 4] for an unknown text: normalised it is [0.6, 0.8], so w.x = 4*0.6 - 4*0.8 = -0.8 (unnormalised it would be -4)
    client, _ = make_client(heads)
    with client:
        score = classify(client, ["something else"]).json()["results"][0]["score"]

    assert score == pytest.approx(sigmoid(-0.8), abs=1e-6)


def test_the_bias_is_added_before_the_sigmoid(heads):
    write_head(heads, PRIMARY, w=(4.0, -4.0), b=1.5)
    client, _ = make_client(heads)
    with client:
        scores = [r["score"] for r in classify(client, [HARD, EASY]).json()["results"]]

    assert scores == pytest.approx([sigmoid(4 + 1.5), sigmoid(-4 + 1.5)], abs=1e-6)


def test_truncation_is_reported_per_item(heads):
    services = {PRIMARY: FakeText(PRIMARY, truncated=True), FALLBACK: FakeText(FALLBACK)}
    client, _ = make_client(heads, services=services)
    with client:
        assert classify(client).json()["results"][0]["truncated"] is True


# ---- failover ---------------------------------------------------------------------------------------------------------------
def test_a_primary_that_raises_hands_the_request_to_the_fallback_and_says_so(heads):
    services = {PRIMARY: FakeText(PRIMARY, mode="raise"), FALLBACK: FakeText(FALLBACK)}
    client, _ = make_client(heads, services=services)
    with client:
        response = classify(client, [HARD])
        health = client.get("/health/models").json()

    body = response.json()
    assert response.status_code == 200
    assert (body["served_by"], body["model"], body["head_version"]) == ("fallback", FALLBACK, f"v1-{FALLBACK}")
    assert body["results"][0]["score"] == pytest.approx(sigmoid(2), abs=1e-6)  # the FALLBACK's head (w = [2, -2]), not the primary's
    assert health["models"][PRIMARY]["state"] == "failed" and "RuntimeError" in health["models"][PRIMARY]["reason"]
    assert health["classifier"]["serving"] == "fallback"


def test_a_primary_that_overruns_the_deadline_hands_over_but_stays_ready(heads):
    services = {PRIMARY: FakeText(PRIMARY, mode="hang"), FALLBACK: FakeText(FALLBACK)}
    client, _ = make_client(heads, services=services)
    with client:
        started = time.monotonic()
        body = classify(client, [HARD]).json()
        waited = time.monotonic() - started
        health = client.get("/health/models").json()

    assert body["served_by"] == "fallback"
    assert waited < 1.0  # bounded by the deadline (60 ms here), not by the hung primary
    assert health["models"][PRIMARY]["state"] == "ready"  # slow once is not broken: it is not sent to the backoff
    assert health["classifier"]["serving"] == "primary"


def test_the_primary_is_used_again_after_it_recovers(heads, monkeypatch):
    monkeypatch.setattr(app_module, "SUPERVISOR_INTERVAL_S", 0.01)
    broken, healthy = FakeText(PRIMARY, mode="raise"), FakeText(PRIMARY)
    reloads = iter([broken, healthy])
    client, _ = make_client(heads, loaders={PRIMARY: lambda: next(reloads)})
    with client:
        assert classify(client).json()["served_by"] == "fallback"  # the first call breaks it ...
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and classify(client).json()["served_by"] != "primary":
            time.sleep(0.02)  # ... the supervisor reloads it after the (shortened) backoff
        served_by = classify(client).json()["served_by"]

    assert served_by == "primary"


def test_with_neither_model_usable_the_answer_is_200_and_says_none(heads):
    def broken():
        raise OSError("model file missing")

    client, _ = make_client(heads, loaders={PRIMARY: broken, FALLBACK: broken})
    with client:
        response = classify(client, [HARD, EASY])
        health = client.get("/health/models").json()

    assert response.status_code == 200
    assert response.json() == {
        "served_by": "none", "model": None, "head_version": None,
        "results": [{"index": i, "decision": "none", "score": None, "truncated": False} for i in range(2)],
    }
    assert health["classifier"]["serving"] == "none"


def test_with_both_models_raising_at_call_time_the_answer_is_still_200_none(heads):
    services = {PRIMARY: FakeText(PRIMARY, mode="raise"), FALLBACK: FakeText(FALLBACK, mode="raise")}
    client, _ = make_client(heads, services=services)
    with client:
        response = classify(client)

    assert response.status_code == 200 and response.json()["served_by"] == "none"


def test_a_model_that_returns_garbage_is_treated_as_failed_not_served(heads):
    # NaN scores cannot be put in JSON (the response would be a 500) and are not advice; the fallback must answer instead
    services = {PRIMARY: FakeText(PRIMARY, mode="nan"), FALLBACK: FakeText(FALLBACK)}
    client, _ = make_client(heads, services=services)
    with client:
        response = classify(client)
        primary_state = client.get("/health/models").json()["models"][PRIMARY]["state"]

    assert response.status_code == 200 and response.json()["served_by"] == "fallback"
    assert primary_state == "failed"


def test_a_model_without_the_classify_role_is_never_asked(heads):
    services = {PRIMARY: FakeText(PRIMARY), FALLBACK: FakeText(FALLBACK)}
    app = create_app(
        settings(heads),
        specs=[spec(PRIMARY, roles=("embed",)), spec(FALLBACK)],  # the primary only embeds: the fallback is the only classifier
        loaders={"rerank": _rerank_loader, "prompt-guard": _guard_loader, **{n: (lambda s=s: s) for n, s in services.items()}},
    )
    with TestClient(app) as client:
        body = classify(client).json()

    assert services[PRIMARY].calls == []
    assert body["served_by"] == "primary" and body["model"] == FALLBACK  # the first classifier IS the primary, whatever its name


# ---- heads: the rules that keep one from doing harm ---------------------------------------------------------------------------
def test_with_no_head_files_the_service_gives_no_advice(tmp_path):
    client, services = make_client(tmp_path)
    with client:
        body = classify(client).json()
        heads = client.get("/health/models").json()["classifier"]["heads"]

    assert body["served_by"] == "none" and body["results"][0]["decision"] == "none"
    assert heads == {PRIMARY: "absent", FALLBACK: "absent"}
    assert services[PRIMARY].calls == []  # nothing to score with, so no model pass is wasted


def test_a_head_that_is_not_accepted_gives_no_advice(heads):
    write_head(heads, PRIMARY, accepted=False)
    write_head(heads, FALLBACK, accepted=False)
    client, services = make_client(heads)
    with client:
        body = classify(client).json()
        health = client.get("/health/models").json()["classifier"]

    assert body["served_by"] == "none" and body["results"][0]["score"] is None
    assert health["heads"] == {PRIMARY: "refused: not accepted", FALLBACK: "refused: not accepted"}
    assert services[PRIMARY].calls == [] and services[FALLBACK].calls == []


@pytest.mark.parametrize("not_true", ["true", 1, "yes", None])
def test_accepted_must_be_the_json_value_true(heads, not_true):
    write_head(heads, PRIMARY, accepted=not_true)
    client, _ = make_client(heads)
    with client:
        assert client.get("/health/models").json()["classifier"]["heads"][PRIMARY] == "refused: not accepted"


def test_an_unaccepted_primary_head_lets_the_fallback_answer(heads):
    write_head(heads, PRIMARY, accepted=False)
    client, _ = make_client(heads)
    with client:
        body = classify(client).json()

    assert body["served_by"] == "fallback" and body["model"] == FALLBACK


def test_a_head_built_for_another_revision_is_refused_and_that_model_gives_no_advice(heads):
    write_head(heads, PRIMARY, model_revision="d" * 40)
    client, services = make_client(heads)
    with client:
        body = classify(client).json()
        health = client.get("/health/models").json()["classifier"]

    assert health["heads"][PRIMARY] == "refused: revision mismatch" and health["heads"][FALLBACK] == "loaded"
    assert body["served_by"] == "fallback"
    assert services[PRIMARY].calls == []


def test_the_revision_compared_is_the_one_the_model_really_loaded_not_the_one_declared(heads):
    # the registry pins revision `a…`; suppose the cache resolved something else. The head matches the DECLARED revision, so a check
    # against the declaration would pass and serve a head over vectors from different weights.
    services = {PRIMARY: FakeText(PRIMARY, revision="e" * 40), FALLBACK: FakeText(FALLBACK)}
    client, _ = make_client(heads, services=services)
    with client:
        assert client.get("/health/models").json()["classifier"]["heads"][PRIMARY] == "refused: revision mismatch"


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [("model_repo", "someone/else", "repo mismatch"), ("model_file", "onnx/model_int8.onnx", "file mismatch"),
     ("model", "another-model", "model mismatch")],
)
def test_a_head_bound_to_another_repo_file_or_model_name_is_refused(heads, field, value, reason):
    write_head(heads, PRIMARY, **{field: value})
    client, _ = make_client(heads)
    with client:
        assert client.get("/health/models").json()["classifier"]["heads"][PRIMARY] == f"refused: {reason}"


def test_a_head_for_a_different_dimension_is_refused(heads):
    write_head(heads, PRIMARY, w=(1.0, 2.0, 3.0), dim=3)
    client, _ = make_client(heads)
    with client:
        assert client.get("/health/models").json()["classifier"]["heads"][PRIMARY] == "refused: dimension mismatch"


@pytest.mark.skipif(os.geteuid() == 0, reason="root can search any directory whatever its mode, so there is no unreadable directory to test with")
def test_a_heads_directory_the_service_cannot_read_is_a_refusal_not_a_crash(heads):
    """Regression, found by CI and not by a test. pytest makes its temp directories 0700, and a container running as uid 1000 that mounted one
    got `PermissionError` out of `Path.exists()`: in Python 3.13 it ignores only "not found"-class errors. `read_head` is documented as never
    raising, but this escaped the lifespan and the WHOLE service failed to start (embeddings, rerank and prompt guard too, not only routing).
    An operator who mounts the heads with the wrong owner would have taken retrieval down the same way. It is a refusal, visible in
    /health/models, and every other endpoint keeps working."""
    heads.chmod(0o000)  # the directory itself: its files can no longer be reached, which is what a directory owned by another uid does
    try:
        client, _ = make_client(heads)
        with client:
            classifier = client.get("/health/models").json()["classifier"]
            body = classify(client).json()
            embeddings = client.post("/v1/embeddings", json={"model": PRIMARY, "input": ["still serving"]})
    finally:
        heads.chmod(0o700)  # so pytest can remove it

    assert all(status.startswith("refused: unreadable") for status in classifier["heads"].values()), classifier
    assert classifier["serving"] == "none" and body["served_by"] == "none"
    assert embeddings.status_code == 200  # the failure is that one thing's, not the service's


@pytest.mark.parametrize("damage", ["json", "npz", "shape", "nan", "no_threshold", "no_version", "schema"])
def test_a_damaged_head_is_refused_without_taking_the_service_down(heads, damage):
    if damage == "json":
        (heads / f"head-{PRIMARY}.json").write_text("{not json")
    elif damage == "npz":
        (heads / f"head-{PRIMARY}.npz").write_bytes(b"not an npz")
    elif damage == "shape":
        write_head(heads, PRIMARY, w=(1.0, 2.0, 3.0))  # json says dim 2, the weights have 3
    elif damage == "nan":
        write_head(heads, PRIMARY, w=(float("nan"), 1.0))
    elif damage == "no_threshold":
        write_head(heads, PRIMARY, threshold=None)
    elif damage == "no_version":
        write_head(heads, PRIMARY, head_version="")
    else:
        write_head(heads, PRIMARY, schema_version=2)
    client, _ = make_client(heads)
    with client:
        status = client.get("/health/models").json()["classifier"]["heads"][PRIMARY]
        body = classify(client).json()

    assert status.startswith("refused: malformed")
    assert body["served_by"] == "fallback"  # the other model is unaffected


class _RunsOnUnpickle:
    """Unpickling this calls `marker.write_text("ran")`: the smallest honest stand-in for a hostile head file's payload."""

    def __init__(self, marker: Path) -> None:
        self.marker = marker

    def __reduce__(self):
        return (self.marker.write_text, ("ran",))


def test_an_npz_is_never_unpickled(heads, tmp_path_factory):
    # `np.load(allow_pickle=True)` would run code from a file an operator mounted. Refusing the array afterwards is not enough: the
    # side effect has already happened by then, so the test looks for the side effect, not for the refusal.
    marker = tmp_path_factory.mktemp("marker") / "unpickled"
    np.savez(heads / f"head-{PRIMARY}.npz", w=np.array([_RunsOnUnpickle(marker), 0], dtype=object), b=np.float32(0))
    client, _ = make_client(heads)
    with client:
        status = client.get("/health/models").json()["classifier"]["heads"][PRIMARY]

    assert not marker.exists(), "the head file was unpickled"
    assert status.startswith("refused: malformed")


# ---- the request ------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({"task": "route", "texts": []}, "invalid_input"),
        ({"task": "route", "texts": ["x"] * 17}, "batch_too_large"),
        ({"task": "route", "texts": [""]}, "invalid_input"),
        ({"task": "route", "texts": [3]}, "invalid_input"),
        ({"task": "route", "texts": "not a list"}, "invalid_input"),
        ({"task": "route"}, "invalid_input"),
        ({"task": "route", "texts": ["x" * 41]}, "input_too_long"),
        ({"texts": ["x"]}, "invalid_task"),
        ({"task": "sentiment", "texts": ["x"]}, "invalid_task"),
        (["not", "an", "object"], "invalid_input"),
    ],
)
def test_a_malformed_request_is_a_400_with_a_stable_code(heads, body, code):
    client, services = make_client(heads)
    with client:
        response = client.post("/classify", json=body)

    assert response.status_code == 400 and response.json()["error"]["code"] == code
    assert services[PRIMARY].calls == []


def test_a_body_that_is_not_json_is_a_400(heads):
    client, _ = make_client(heads)
    with client:
        response = client.post("/classify", content=b"{nope", headers={"content-type": "application/json"})

    assert response.status_code == 400 and response.json()["error"]["code"] == "invalid_input"


def test_sixteen_texts_are_accepted(heads):
    client, _ = make_client(heads)
    with client:
        assert len(classify(client, [HARD] * 16).json()["results"]) == 16


# ---- observability ----------------------------------------------------------------------------------------------------------
def test_the_request_counter_is_labelled_by_who_answered(heads):
    services = {PRIMARY: FakeText(PRIMARY), FALLBACK: FakeText(FALLBACK)}
    client, _ = make_client(heads, services=services)
    with client:
        classify(client)
        services[PRIMARY].mode = "raise"
        classify(client)
        classify(client)  # the primary is now failed: skipped
        text = client.get("/metrics").text

    assert 'ml_classify_requests_total{served_by="primary"} 1.0' in text
    assert 'ml_classify_requests_total{served_by="fallback"} 2.0' in text


def test_the_serving_gauge_is_computed_when_scraped_so_it_cannot_go_stale(heads):
    services = {PRIMARY: FakeText(PRIMARY, mode="raise"), FALLBACK: FakeText(FALLBACK)}
    client, _ = make_client(heads, services=services)
    with client:
        before = client.get("/metrics").text
        classify(client)  # breaks the primary
        after = client.get("/metrics").text  # no request since: the alert on this gauge needs it to be right with no traffic

    assert 'ml_classifier_serving{served_by="primary"} 1.0' in before and 'ml_classifier_serving{served_by="fallback"} 0.0' in before
    assert 'ml_classifier_serving{served_by="primary"} 0.0' in after and 'ml_classifier_serving{served_by="fallback"} 1.0' in after


def test_health_models_reports_who_is_serving_and_every_heads_status(heads):
    client, _ = make_client(heads)
    with client:
        classifier = client.get("/health/models").json()["classifier"]

    assert classifier == {"serving": "primary", "heads": {PRIMARY: "loaded", FALLBACK: "loaded"}}


def test_no_request_text_reaches_a_log_or_a_metric(heads, caplog):
    secret = "my-password-is-hunter2-and-the-card-is-4111"
    caplog.set_level(logging.DEBUG)
    services = {PRIMARY: FakeText(PRIMARY, mode="raise"), FALLBACK: FakeText(FALLBACK)}
    client, _ = make_client(heads, services=services, classify_max_chars=200)
    with client:
        classify(client, [secret])  # primary raises -> fallback; the failure path logs too
        classify(client, [secret, ""])  # a rejected request
        classify(client, [secret * 10])  # an over-long one
        everything = caplog.text + repr([r.__dict__ for r in caplog.records]) + client.get("/metrics").text
        everything += json.dumps(client.get("/health/models").json())

    assert "hunter2" not in everything and "4111" not in everything


def test_a_model_is_loaded_once_and_serves_every_role_it_declares_but_only_those(heads):
    services = {PRIMARY: FakeText(PRIMARY), FALLBACK: FakeText(FALLBACK)}
    app = create_app(
        settings(heads),
        specs=[spec(PRIMARY), spec(FALLBACK, roles=("classify",))],  # the fallback classifies but does not embed
        loaders={"rerank": _rerank_loader, "prompt-guard": _guard_loader, **{n: (lambda s=s: s) for n, s in services.items()}},
    )
    with TestClient(app) as client:
        listed = {m["id"] for m in client.get("/v1/models").json()["data"]}
        embedded = client.post("/v1/embeddings", json={"model": PRIMARY, "input": HARD})
        refused = client.post("/v1/embeddings", json={"model": FALLBACK, "input": HARD})
        classified = classify(client).json()

    assert listed == {PRIMARY}
    assert embedded.status_code == 200  # the same slot answers /v1/embeddings ...
    assert classified["served_by"] == "primary"  # ... and /classify
    assert refused.status_code == 404 and refused.json()["error"]["code"] == "model_not_found"
