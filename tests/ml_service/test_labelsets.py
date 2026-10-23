"""POST /v1/decide asked a `choice` question of a label set: which predefined choice is this text nearest to, and the rules that keep a label
set from doing harm (specs/015 FR-001..007, specs/016 contracts/decide.md). The first tests pin the wire shape, the table of
rejected requests its errors, and the rest what the matcher does behind it.

The real app, lifespan, registry, supervisor and label-set files are used; only the two text models are fakes, because what is under test is the
set's validation, the per-model index and cutoff, and the failover, not the ONNX pass. The geometry is chosen so every expected number is worked
out by hand: the examples of `alpha`, `beta` and `gamma` sit exactly on the three axes, so each label's centroid is that axis, and a query's
similarity to a label is its unit vector's coordinate on that axis. `[3, 1, 0]` is (0.9487, 0.3162, 0); `[1, 1, 0]` is (0.7071, 0.7071, 0).

The rules pinned here, each with the failure it prevents:
- a set is served only by a model that has a cutoff for it (a fallback on another similarity scale must not be given the primary's number);
- the examples are embedded by the serving model, and re-embedded when that model's revision changes (a stale vector is confident nonsense);
- a malformed set is that set's `refused` line and nothing else (a bad file must not stop embeddings, the reranker or the guard);
- a primary that raises, overruns its deadline or returns NaN hands the request to the fallback, with the fallback's own cutoff;
- the questions of one request are answered by one model, with one embedding pass;
- with no model usable the answer is 503 `model_unavailable`, never a 500 and never a guess;
- no request text reaches a log.
"""
import asyncio
import json
import logging
import math
import time
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

import ml_service.app as app_module
from ml_service import labelsets
from ml_service.app import create_app
from ml_service.embed import Embedded
from ml_service.labelsets import Labeller, Refused, parse_labelset, read_labelsets
from ml_service.registry import Registry, Slot
from tests.ml_service.fakes import (
    FALLBACK,
    PRIMARY,
    FakeText,
    loaders_for,
    make_client,
    settings,
    spec,
)

AXES = {"alpha": [1.0, 0.0, 0.0], "beta": [0.0, 1.0, 0.0], "gamma": [0.0, 0.0, 1.0]}
EXAMPLES = {label: [f"{label} example {n}" for n in (1, 2, 3)] for label in AXES}
NEAR_ALPHA, BETWEEN, EVERYWHERE = "near alpha text", "between alpha and beta text", "a bit of everything text"
QUERIES = {NEAR_ALPHA: [3.0, 1.0, 0.0], BETWEEN: [1.0, 1.0, 0.0], EVERYWHERE: [1.0, 1.0, 1.0]}
PRIMARY_CUTOFF, FALLBACK_CUTOFF = 0.8, 0.5  # different scales on purpose: (0.7071) is "none" for the first and "alpha" for the second


class FakeLabelText(FakeText):
    """A `FakeText` whose vectors are chosen per text: the examples on the axes, the queries as above, anything else off-axis."""

    def __init__(self, name: str, **kw) -> None:
        super().__init__(name, dim=3, **kw)
        self.vectors = {t: np.array(AXES[label], dtype=np.float32) for label, ts in EXAMPLES.items() for t in ts}
        self.vectors.update({t: np.array(v, dtype=np.float32) for t, v in QUERIES.items()})

    async def embed(self, texts: list[str]) -> list[Embedded]:
        self.calls.append(list(texts))
        if self.mode == "raise":
            raise RuntimeError("the session blew up")
        if self.mode == "hang":
            await asyncio.Event().wait()
        if self.mode == "nan":
            return [Embedded(np.array([np.nan] * 3, dtype=np.float32), 3, False) for _ in texts]
        return [Embedded(self.vectors.get(t, np.array([1.0, 1.0, 1.0], dtype=np.float32)), 3, self.truncated) for t in texts]


def set_json(**overrides) -> dict:
    base = {
        "schema_version": 1, "name": "mood", "description": "three moods",
        "labels": {label: list(ts) for label, ts in EXAMPLES.items()},
        "calibration": {PRIMARY: {"min_similarity": PRIMARY_CUTOFF}, FALLBACK: {"min_similarity": FALLBACK_CUTOFF}},
    }
    return {**base, **overrides}


def write_set(directory: Path, name: str = "mood", **overrides) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.json"
    path.write_text(json.dumps(set_json(name=name, **overrides)))
    return path


@pytest.fixture
def sets(tmp_path: Path) -> Path:
    write_set(tmp_path / "labelsets")
    return tmp_path / "labelsets"


def client_for(tmp_path: Path, sets_dir: Path, *, services: dict[str, FakeLabelText] | None = None, **kw):
    services = services or {PRIMARY: FakeLabelText(PRIMARY), FALLBACK: FakeLabelText(FALLBACK)}
    client, services = make_client(services=services, labelset_dir=sets_dir, **kw)  # type: ignore[arg-type]  # FakeLabelText is a FakeText
    return client, services


def decide(client: TestClient, text: str = NEAR_ALPHA, *, name: str = "mood", **extra):
    """One `choice` question named after the label set, in the System One request shape (specs/016 contracts/decide.md)."""
    options = {label: f"the {label} topic" for label in AXES}
    question = {"type": "choice", "instructions": "Which topic is this about?", "criteria": options}
    return client.post("/v1/decide", json={"state": text, "questions": {name: question}, **extra})


def answer_of(client: TestClient, text: str = NEAR_ALPHA) -> dict:
    return decide(client, text).json()["answers"]["mood"]


# ---- the primary answers ----------------------------------------------------------------------------------------------------
def test_a_text_near_one_label_gets_it_with_the_similarity_and_the_runner_up(tmp_path, sets):
    client, _ = client_for(tmp_path, sets)
    with client:
        body = decide(client).json()

    assert body["x_served_by"] == "primary" and body["model"] == PRIMARY
    answer = body["answers"]["mood"]
    assert answer["type"] == "choice" and answer["choice"] == "alpha"
    evidence = answer["x_evidence"]
    assert evidence["nearest"] == "alpha" and evidence["similarity"] == pytest.approx(3 / math.sqrt(10))  # 0.9487
    assert evidence["runner_up"] == "beta" and evidence["margin"] == pytest.approx(3 / math.sqrt(10) - 1 / math.sqrt(10))
    assert evidence["truncated"] is False
    assert body["usage"] == {"input_tokens": 3, "output_tokens": 0}, "the embedder's token count, and nothing generated"


def test_the_answer_carries_no_confidence_and_no_probabilities_because_a_cosine_is_neither(tmp_path, sets):
    client, _ = client_for(tmp_path, sets)
    with client:
        answer = decide(client).json()["answers"]["mood"]

    assert "confidence" not in answer and "probabilities" not in answer
    assert set(answer) == {"type", "choice", "x_evidence", "x_labelset_version"}, "everything this service adds is prefixed x_"


def test_the_version_is_the_sets_content_and_is_stable(tmp_path, sets):
    client, _ = client_for(tmp_path, sets)
    with client:
        first = answer_of(client)["x_labelset_version"]
        second = answer_of(client)["x_labelset_version"]

    assert first == second and len(first) == 12 and int(first, 16) >= 0


def test_a_text_below_the_cutoff_has_no_choice_but_still_reports_what_was_nearest(tmp_path, sets):
    client, _ = client_for(tmp_path, sets)
    with client:
        answer = answer_of(client, BETWEEN)

    assert answer["choice"] is None and answer["x_evidence"]["nearest"] == "alpha"
    assert answer["x_evidence"]["similarity"] == pytest.approx(1 / math.sqrt(2)) and answer["x_evidence"]["margin"] == pytest.approx(0.0, abs=1e-6)


def test_the_cutoff_is_inclusive(tmp_path, sets):
    """[4, 3, 0] has unit vector (0.8, 0.6, 0), so its similarity to the alpha axis is exactly the double 0.8, which is the primary's cutoff."""
    services = {PRIMARY: FakeLabelText(PRIMARY), FALLBACK: FakeLabelText(FALLBACK)}
    for service in services.values():
        service.vectors["four three"] = np.array([4.0, 3.0, 0.0], dtype=np.float32)
    client, _ = client_for(tmp_path, sets, services=services)
    with client:
        answer = answer_of(client, "four three")

    assert answer["x_evidence"]["similarity"] == PRIMARY_CUTOFF == 0.8 and answer["choice"] == "alpha"


def test_two_questions_in_one_request_are_answered_by_one_model_with_one_embedding_pass(tmp_path, sets):
    write_set(sets, "mood-too")
    client, services = client_for(tmp_path, sets)
    with client:
        body = client.post("/v1/decide", json={"state": NEAR_ALPHA, "questions": {"mood": GOOD, "mood-too": GOOD}}).json()

    assert body["x_served_by"] == "primary" and set(body["answers"]) == {"mood", "mood-too"}
    assert [a["choice"] for a in body["answers"].values()] == ["alpha", "alpha"]
    assert services[PRIMARY].calls.count([NEAR_ALPHA]) == 1, "the text is embedded once, however many questions ask about it"
    assert body["usage"]["input_tokens"] == 3, "and its tokens are counted once"


def test_questions_are_never_split_across_models(tmp_path, sets):
    """`mood-too` has a cutoff for the fallback only, so the primary cannot serve it; the whole request goes to the fallback, because a reply
    that said `model: primary` for one answer and fallback for the other would be a lie about which scale each cutoff was applied on."""
    write_set(sets, "mood-too", calibration={FALLBACK: {"min_similarity": FALLBACK_CUTOFF}})
    client, _ = client_for(tmp_path, sets)
    with client:
        body = client.post("/v1/decide", json={"state": BETWEEN, "questions": {"mood": GOOD, "mood-too": GOOD}}).json()

    assert body["x_served_by"] == "fallback" and body["model"] == FALLBACK
    assert [a["choice"] for a in body["answers"].values()] == ["alpha", "alpha"]  # 0.7071 clears the fallback's 0.5 for both


# ---- failover: each model has its own index and its own cutoff -----------------------------------------------------------------
@pytest.mark.parametrize("mode", ["raise", "hang", "nan"])
def test_a_primary_that_fails_hands_over_and_the_fallback_uses_its_own_cutoff(tmp_path, sets, mode):
    services = {PRIMARY: FakeLabelText(PRIMARY), FALLBACK: FakeLabelText(FALLBACK)}
    client, _ = client_for(tmp_path, sets, services=services)
    with client:
        services[PRIMARY].mode = mode  # after the start-up index build, so the failure is at request time
        body = decide(client, BETWEEN).json()
        state = client.get("/health/models").json()["models"][PRIMARY]["state"]

    assert body["x_served_by"] == "fallback" and body["model"] == FALLBACK
    assert body["answers"]["mood"]["choice"] == "alpha"  # 0.7071 is below the primary's 0.8 and above the fallback's 0.5
    # A model that RAISES is `failed` (and reloaded after a backoff); one that is only slow stays `ready` (the next test).
    assert state == ("ready" if mode == "hang" else "failed")


def test_a_slow_primary_is_skipped_for_the_request_but_stays_ready(tmp_path, sets):
    services = {PRIMARY: FakeLabelText(PRIMARY), FALLBACK: FakeLabelText(FALLBACK)}
    client, _ = client_for(tmp_path, sets, services=services)
    with client:
        services[PRIMARY].mode = "hang"
        assert decide(client).json()["x_served_by"] == "fallback"
        services[PRIMARY].mode = "ok"
        assert decide(client).json()["x_served_by"] == "primary"
        assert client.get("/health/models").json()["models"][PRIMARY]["state"] == "ready"


def test_when_no_model_can_answer_it_is_a_503_model_unavailable_with_retry_after(tmp_path, sets):
    services = {PRIMARY: FakeLabelText(PRIMARY), FALLBACK: FakeLabelText(FALLBACK)}
    client, _ = client_for(tmp_path, sets, services=services)
    with client:
        services[PRIMARY].mode = services[FALLBACK].mode = "raise"
        response = decide(client)

    assert response.status_code == 503 and response.headers["retry-after"] == "1"
    assert response.json() == {"error": {"code": "model_unavailable", "message": "no model can answer these questions right now"}}


def test_a_model_with_no_cutoff_for_the_set_does_not_serve_it(tmp_path):
    sets_dir = tmp_path / "labelsets"
    write_set(sets_dir, calibration={FALLBACK: {"min_similarity": FALLBACK_CUTOFF}})  # nothing for the primary
    client, services = client_for(tmp_path, sets_dir)
    with client:
        body = decide(client).json()
        shown = client.get("/health/models").json()["labelsets"]["mood"]["models"]

    assert body["x_served_by"] == "fallback" and body["model"] == FALLBACK
    assert shown[PRIMARY] == f"refused: no calibration for {PRIMARY}" and shown[FALLBACK] == "loaded"
    assert len(services[PRIMARY].calls) == 0, "a model that cannot serve the set must not even be asked to embed its examples"


def test_with_no_cutoff_for_any_model_the_answer_is_unavailable(tmp_path):
    sets_dir = tmp_path / "labelsets"
    write_set(sets_dir, calibration={"some-other-model": {"min_similarity": 0.5}})
    client, _ = client_for(tmp_path, sets_dir)
    with client:
        assert decide(client).status_code == 503


def test_the_primary_is_used_again_after_it_recovers(tmp_path, sets, monkeypatch):
    monkeypatch.setattr(app_module, "SUPERVISOR_INTERVAL_S", 0.01)
    first, healthy = FakeLabelText(PRIMARY), FakeLabelText(PRIMARY)
    reloads = iter([first, healthy])
    client, _ = client_for(tmp_path, sets, loaders={PRIMARY: lambda: next(reloads)})
    with client:
        first.mode = "raise"  # after the start-up index build, so the break is at request time
        assert decide(client).json()["x_served_by"] == "fallback"  # the first call breaks it ...
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and decide(client).json().get("x_served_by") != "primary":
            time.sleep(0.02)  # ... the supervisor reloads it after the (shortened) backoff, and it is the primary again
        served_by = decide(client).json()["x_served_by"]

    assert served_by == "primary"


def test_a_model_without_the_classify_role_is_never_asked(tmp_path, sets):
    services = {PRIMARY: FakeLabelText(PRIMARY), FALLBACK: FakeLabelText(FALLBACK)}
    app = create_app(
        settings(labelset_dir=sets),
        specs=[spec(PRIMARY, roles=("embed",)), spec(FALLBACK)],  # the primary only embeds: the fallback is the only matcher
        loaders=loaders_for(services),
    )
    with TestClient(app) as client:
        body = decide(client).json()

    assert services[PRIMARY].calls == []
    assert body["x_served_by"] == "primary" and body["model"] == FALLBACK  # the first matcher IS the primary, whatever its name


def test_a_model_is_loaded_once_and_serves_every_role_it_declares_but_only_those(tmp_path, sets):
    services = {PRIMARY: FakeLabelText(PRIMARY), FALLBACK: FakeLabelText(FALLBACK)}
    app = create_app(
        settings(labelset_dir=sets),
        specs=[spec(PRIMARY), spec(FALLBACK, roles=("classify",))],  # the fallback decides but does not embed
        loaders=loaders_for(services),
    )
    with TestClient(app) as client:
        listed = {m["id"] for m in client.get("/v1/models").json()["data"]}
        embedded = client.post("/v1/embeddings", json={"model": PRIMARY, "input": NEAR_ALPHA})
        refused = client.post("/v1/embeddings", json={"model": FALLBACK, "input": NEAR_ALPHA})
        decided = decide(client).json()

    assert listed == {PRIMARY}
    assert embedded.status_code == 200  # the same slot answers /v1/embeddings ...
    assert decided["x_served_by"] == "primary"  # ... and /v1/decide
    assert refused.status_code == 404 and refused.json()["error"]["code"] == "model_not_found"


def test_truncation_is_reported_in_the_evidence(tmp_path, sets):
    services = {PRIMARY: FakeLabelText(PRIMARY, truncated=True), FALLBACK: FakeLabelText(FALLBACK)}
    client, _ = client_for(tmp_path, sets, services=services)
    with client:
        evidence = answer_of(client)["x_evidence"]

    assert evidence["truncated"] is True


# ---- the index follows the model --------------------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_examples_are_embedded_again_when_the_models_revision_changes(tmp_path):
    write_set(tmp_path)
    service = FakeLabelText(PRIMARY, revision="a" * 40)
    registry = Registry(base_backoff_s=0.01, max_backoff_s=0.02)
    registry.add(Slot(name=PRIMARY, roles=("embed", "classify"), loader=lambda: service))
    await registry.load_all()
    from ml_service.metrics import Metrics

    labeller = Labeller(registry, [spec(PRIMARY)], read_labelsets(tmp_path), settings(), Metrics())
    await labeller.sync()
    await labeller.sync()  # nothing changed: no second embedding pass
    assert len(service.calls) == 1 and len(service.calls[0]) == 9

    service.model.revision = "b" * 40  # the model was reloaded from another commit
    assert (await labeller.label("mood", [NEAR_ALPHA])).served_by == "none", "a stale index must not answer for a model it was not built from"
    assert len(service.calls) == 1, "and it must not spend a model pass finding that out"
    await labeller.sync()
    assert len(service.calls) == 2 and len(service.calls[1]) == 9  # the examples again, with the model that is loaded now
    assert (await labeller.label("mood", [NEAR_ALPHA])).served_by == "primary"
    assert len(service.calls) == 3


@pytest.mark.asyncio
async def test_asking_about_no_sets_is_no_answer_and_costs_no_model_pass(tmp_path):
    write_set(tmp_path)
    service = FakeLabelText(PRIMARY)
    registry = Registry()
    registry.add(Slot(name=PRIMARY, roles=("embed", "classify"), loader=lambda: service))
    await registry.load_all()
    from ml_service.metrics import Metrics

    labeller = Labeller(registry, [spec(PRIMARY)], read_labelsets(tmp_path), settings(), Metrics())
    await labeller.sync()
    before = len(service.calls)

    assert await labeller.decide([], NEAR_ALPHA) is None and len(service.calls) == before


@pytest.mark.asyncio
async def test_before_the_first_index_a_set_is_pending_not_wrong(tmp_path):
    write_set(tmp_path)
    registry = Registry()
    registry.add(Slot(name=PRIMARY, roles=("embed", "classify"), loader=lambda: FakeLabelText(PRIMARY)))
    await registry.load_all()
    from ml_service.metrics import Metrics

    labeller = Labeller(registry, [spec(PRIMARY)], read_labelsets(tmp_path), settings(), Metrics())

    assert labeller.snapshot()["mood"]["models"][PRIMARY] == "pending: not indexed yet"
    assert (await labeller.label("mood", [NEAR_ALPHA])).served_by == "none"


@pytest.mark.asyncio
async def test_an_indexing_failure_is_the_pairs_status_and_the_models_failed_state_not_a_crash(tmp_path):
    write_set(tmp_path)
    service = FakeLabelText(PRIMARY, mode="raise")
    registry = Registry(base_backoff_s=60, max_backoff_s=60)
    registry.add(Slot(name=PRIMARY, roles=("embed", "classify"), loader=lambda: service))
    await registry.load_all()
    from ml_service.metrics import Metrics

    labeller = Labeller(registry, [spec(PRIMARY)], read_labelsets(tmp_path), settings(), Metrics())
    await labeller.sync()

    assert labeller.snapshot()["mood"]["models"][PRIMARY] == "pending: indexing failed (RuntimeError)"
    assert registry.snapshot()[PRIMARY]["state"] == "failed"


# ---- the request ------------------------------------------------------------------------------------------------------------
GOOD = {"type": "choice", "instructions": "Which topic?", "criteria": {label: f"the {label} topic" for label in AXES}}


@pytest.mark.parametrize(
    ("body", "status", "code"),
    [
        ("not an object", 400, "invalid_input"),
        ({"questions": {"mood": GOOD}}, 400, "invalid_input"),  # no state
        ({"state": "", "questions": {"mood": GOOD}}, 400, "invalid_input"),
        ({"state": "   ", "questions": {"mood": GOOD}}, 400, "invalid_input"),
        ({"state": 3, "questions": {"mood": GOOD}}, 400, "invalid_input"),
        ({"state": {"a": 1}, "questions": {"mood": GOOD}}, 422, "unsupported_state"),
        ({"state": ["a"], "questions": {"mood": GOOD}}, 422, "unsupported_state"),
        ({"state": "x" * 41, "questions": {"mood": GOOD}}, 413, "input_too_long"),
        ({"state": "x"}, 400, "invalid_input"),  # no questions
        ({"state": "x", "questions": {}}, 400, "invalid_input"),
        ({"state": "x", "questions": [GOOD]}, 400, "invalid_input"),
        ({"state": "x", "questions": {f"q{i}": GOOD for i in range(9)}}, 400, "too_many_questions"),
        ({"state": "x", "questions": {"mood": {**GOOD, "criteria": {f"o{i}": "d" for i in range(256)}}}}, 400, "invalid_input"),  # the interface's 255 options
        ({"state": "x", "questions": {"mood": {**GOOD, "criteria": {f"o{i}": "d" for i in range(255)}}}}, 400, "criteria_mismatch"),  # 255 is allowed, and then is not the set
        ({"state": "x", "questions": {"mood": GOOD}, "temperature": 0}, 400, "invalid_input"),
        ({"state": "x", "questions": {"mood": GOOD}, "assets": {}}, 422, "unsupported_field"),
        ({"state": "x", "questions": {"mood": GOOD}, "model": "typesafe/jev-1.13.0"}, 400, "unknown_model"),
        ({"state": "x", "questions": {"bad name!": GOOD}}, 400, "invalid_input"),
        ({"state": "x", "questions": {"mood": "choice"}}, 400, "invalid_input"),
        ({"state": "x", "questions": {"mood": {**GOOD, "extra": 1}}}, 400, "invalid_input"),
        ({"state": "x", "questions": {"mood": {**GOOD, "type": "banana"}}}, 400, "invalid_input"),
        ({"state": "x", "questions": {"mood": {**GOOD, "type": "noul"}}}, 422, "unsupported_question_type"),
        ({"state": "x", "questions": {"mood": {**GOOD, "type": "score", "criteria": ["a", "b"]}}}, 422, "unsupported_question_type"),
        ({"state": "x", "questions": {"mood": {**GOOD, "instructions": ""}}}, 400, "invalid_input"),
        ({"state": "x", "questions": {"mood": {"type": "choice", "instructions": "i"}}}, 400, "invalid_input"),  # no criteria
        ({"state": "x", "questions": {"mood": {**GOOD, "criteria": {}}}}, 400, "invalid_input"),
        ({"state": "x", "questions": {"mood": {**GOOD, "criteria": ["alpha", "beta", "gamma"]}}}, 400, "invalid_input"),
        ({"state": "x", "questions": {"mood": {**GOOD, "criteria": {"alpha": 1, "beta": "b", "gamma": "g"}}}}, 400, "invalid_input"),
        ({"state": "x", "questions": {"nope": GOOD}}, 400, "unknown_decision"),
        ({"state": "x", "questions": {"mood": {**GOOD, "criteria": {"alpha": "a", "beta": "b"}}}}, 400, "criteria_mismatch"),
        ({"state": "x", "questions": {"mood": {**GOOD, "criteria": {**GOOD["criteria"], "delta": "d"}}}}, 400, "criteria_mismatch"),
    ],
)
def test_a_malformed_request_is_rejected_with_a_stable_code_before_it_reaches_a_model(tmp_path, sets, body, status, code):
    client, services = client_for(tmp_path, sets)
    with client:
        before = len(services[PRIMARY].calls)
        response = client.post("/v1/decide", json=body)

    assert response.status_code == status and response.json()["error"]["code"] == code
    assert len(services[PRIMARY].calls) == before, "a rejected request must not reach a model"


def test_the_limits_are_inclusive(tmp_path, sets):
    """A state of exactly the limit (40 here) and exactly eight questions are accepted; one more of either is the rejection above."""
    for i in range(7):
        write_set(sets, f"mood-{i}")
    client, _ = client_for(tmp_path, sets)
    with client:
        at_the_limit = decide(client, "x" * 40)
        eight = client.post("/v1/decide", json={"state": NEAR_ALPHA, "questions": {"mood": GOOD, **{f"mood-{i}": GOOD for i in range(7)}}})

    assert at_the_limit.status_code == 200 and eight.status_code == 200 and len(eight.json()["answers"]) == 8


def test_a_body_that_is_not_json_is_a_400(tmp_path, sets):
    client, _ = client_for(tmp_path, sets)
    with client:
        response = client.post("/v1/decide", content=b"{nope", headers={"content-type": "application/json"})

    assert response.status_code == 400 and response.json()["error"]["code"] == "invalid_input"


def test_a_mismatched_criteria_says_what_differs(tmp_path, sets):
    client, _ = client_for(tmp_path, sets)
    with client:
        message = client.post("/v1/decide", json={"state": "x", "questions": {"mood": {**GOOD, "criteria": {"alpha": "a", "delta": "d"}}}}).json()["error"]["message"]

    assert "missing ['beta', 'gamma']" in message and "not in the set ['delta']" in message


def test_local_is_an_accepted_model_name_and_leaving_it_out_is_the_same(tmp_path, sets):
    client, _ = client_for(tmp_path, sets)
    with client:
        named = client.post("/v1/decide", json={"model": "local", "state": NEAR_ALPHA, "questions": {"mood": GOOD}})
        unnamed = client.post("/v1/decide", json={"state": NEAR_ALPHA, "questions": {"mood": GOOD}})

    assert named.status_code == unnamed.status_code == 200 and named.json()["answers"] == unnamed.json()["answers"]


@pytest.mark.parametrize("task", ["label", "route"])
def test_classify_is_gone_whatever_task_it_names(tmp_path, sets, task):
    """`/classify` served a trained routing head (spec 014, never accepted) and then the first draft of the topic matcher (spec 015); both are
    `/v1/decide` or nothing now. A caller still posting to it must be told by a 404, not answered by something that happens to look similar."""
    client, _ = client_for(tmp_path, sets)
    with client:
        response = client.post("/classify", json={"task": task, "labelset": "mood", "texts": ["x"]})
        paths = set(client.get("/openapi.json").json()["paths"])

    assert response.status_code == 404 and "/classify" not in paths and "/v1/decide" in paths


# ---- a bad file is that set's problem, and only that set's -----------------------------------------------------------------------
def test_a_malformed_set_is_refused_in_health_and_the_good_one_still_serves(tmp_path, sets):
    (sets / "broken.json").write_text("{nope")
    client, _ = client_for(tmp_path, sets)
    with client:
        health = client.get("/health/models").json()
        good = decide(client).json()
        broken = decide(client, name="broken")

    assert health["labelsets"]["broken"]["state"].startswith("refused: malformed")
    assert health["labelsets"]["mood"]["models"][PRIMARY] == "loaded"
    assert good["x_served_by"] == "primary"
    assert broken.status_code == 503, "a known but refused set is 'cannot answer', not a 400 (its labels are unknown) or a 500"


def test_a_missing_label_set_directory_is_an_empty_list_not_a_failed_start(tmp_path):
    client, _ = client_for(tmp_path, tmp_path / "does-not-exist")
    with client:
        assert client.get("/health").status_code == 200
        assert client.get("/health/models").json()["labelsets"] == {}
        assert decide(client).json()["error"]["code"] == "unknown_decision"


def test_an_unreadable_label_set_directory_is_a_refusal_not_a_failed_start(tmp_path, monkeypatch):
    sets_dir = tmp_path / "labelsets"
    sets_dir.mkdir()
    original = Path.glob

    def deny(self, pattern):
        if self == sets_dir:
            raise PermissionError("denied")
        return original(self, pattern)

    monkeypatch.setattr(Path, "glob", deny)
    client, _ = client_for(tmp_path, sets_dir)
    with client:
        assert client.get("/health").status_code == 200
        assert client.get("/health/models").json()["labelsets"]["(directory)"]["state"].startswith("refused: unreadable (PermissionError")


# ---- the file format ------------------------------------------------------------------------------------------------------------
def parse(**overrides):
    return parse_labelset(json.dumps(set_json(**overrides)), "mood")


def test_a_valid_set_parses_with_its_calibration():
    parsed = parse()
    assert isinstance(parsed, labelsets.LabelSet)
    assert parsed.labels["alpha"] == tuple(EXAMPLES["alpha"]) and parsed.calibration == {PRIMARY: PRIMARY_CUTOFF, FALLBACK: FALLBACK_CUTOFF}


def test_the_version_changes_with_the_content_but_not_with_the_description():
    base = parse()
    reworded = parse(description="a different sentence")
    edited = parse(labels={**{k: list(v) for k, v in EXAMPLES.items()}, "alpha": ["alpha example 1", "alpha example 2", "alpha example 9"]})
    recalibrated = parse(calibration={PRIMARY: {"min_similarity": 0.81}, FALLBACK: {"min_similarity": 0.5}})

    assert base.version == reworded.version
    assert len({base.version, edited.version, recalibrated.version}) == 3


def many(label: str, n: int) -> list[str]:
    return [f"{label} {i}" for i in range(n)]


@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        ({"schema_version": 2}, "schema_version"),
        ({"name": "other"}, "name must equal the file name"),
        ({"labels": {"only": many("only", 5)}}, "2..64 labels"),
        ({"labels": {"a": many("a", 2), "b": many("b", 5)}}, "needs 3..64 examples"),
        ({"labels": {"a": many("a", 65), "b": many("b", 5)}}, "needs 3..64 examples"),
        ({"labels": {"a": ["x", "", "y"], "b": many("b", 5)}}, "1..300 characters"),
        ({"labels": {"a": ["x", 3, "y"], "b": many("b", 5)}}, "1..300 characters"),
        ({"labels": {"a": ["x" * 301, "y", "z"], "b": many("b", 5)}}, "1..300 characters"),
        ({"labels": {"bad name!": many("a", 3), "b": many("b", 5)}}, "label name must match"),
        ({"labels": {"a": ["same", "x", "y"], "b": ["Same ", "p", "q"]}}, "under both 'a' and 'b'"),
        ({"labels": {"a": ["dup", "dup", "y"], "b": many("b", 3)}}, "twice under 'a'"),
        ({"calibration": {}}, "calibration must be an object"),
        ({"calibration": {PRIMARY: {"min_similarity": 0}}}, "strictly between 0 and 1"),
        ({"calibration": {PRIMARY: {"min_similarity": 1}}}, "strictly between 0 and 1"),
        ({"calibration": {PRIMARY: {"min_similarity": True}}}, "strictly between 0 and 1"),
        ({"calibration": {PRIMARY: {"min_similarity": "0.5"}}}, "strictly between 0 and 1"),
        ({"calibration": {PRIMARY: 0.5}}, "strictly between 0 and 1"),
        ({"description": 3}, "description must be a string"),
    ],
)
def test_every_rule_of_the_format_is_a_refusal(overrides, fragment):
    refused = parse(**overrides)
    assert isinstance(refused, Refused) and fragment in refused.reason


def test_more_examples_than_the_total_limit_is_refused():
    labels = {f"l{i}": many(f"l{i}", 64) for i in range(9)}  # 576 > 512
    refused = parse(labels=labels)
    assert isinstance(refused, Refused) and "576 examples" in refused.reason


@pytest.mark.parametrize("text", ["[]", "3", '"x"', "null"])
def test_a_set_that_is_not_an_object_is_refused(text):
    assert isinstance(parse_labelset(text, "mood"), Refused)


def test_the_centroid_of_a_label_whose_examples_cancel_out_is_refused_at_build_time():
    labelset = parse(labels={"a": ["a1", "a2", "a3"], "b": ["b1", "b2", "b3"]})
    assert isinstance(labelset, labelsets.LabelSet)
    vectors = np.array([[1, 0], [-1, 0], [0, 0.0]] + [[0, 1]] * 3)  # a's three examples sum to zero
    with pytest.raises(ValueError, match="cancel out"):
        labelsets.build_index(labelset, vectors, "r" * 40, 0.5)


def test_a_non_finite_example_vector_is_refused_at_build_time():
    labelset = parse(labels={"a": ["a1", "a2", "a3"], "b": ["b1", "b2", "b3"]})
    assert isinstance(labelset, labelsets.LabelSet)
    with pytest.raises(ValueError, match="non-finite"):
        labelsets.build_index(labelset, np.array([[1.0, 0]] * 5 + [[np.nan, 0]]), "r" * 40, 0.5)


# ---- no text in a log; the metric names a set it knows ----------------------------------------------------------------------------
def test_no_request_text_reaches_a_log_even_when_a_model_fails(tmp_path, sets, caplog):
    secret = "the customer's secret sentence 4711"
    services = {PRIMARY: FakeLabelText(PRIMARY), FALLBACK: FakeLabelText(FALLBACK)}
    client, _ = client_for(tmp_path, sets, services=services)
    caplog.set_level(logging.DEBUG)
    with client:
        decide(client, secret)  # 35 characters, under the test limit of 40, so it reaches the model
        services[PRIMARY].mode = "raise"
        decide(client, secret)
        services[PRIMARY].mode = "hang"
        decide(client, secret)

    rendered = " ".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
    assert "secret sentence" not in rendered and "4711" not in rendered


def test_the_request_counter_is_labelled_by_set_and_answerer_and_an_unknown_name_adds_no_series(tmp_path, sets):
    client, _ = client_for(tmp_path, sets)
    with client:
        decide(client)
        decide(client, name="not-a-set-4711")
        scraped = client.get("/metrics").text

    assert 'ml_label_requests_total{labelset="mood",served_by="primary"} 1.0' in scraped
    assert "not-a-set-4711" not in scraped


def test_an_unavailable_answer_is_counted_as_none(tmp_path, sets):
    services = {PRIMARY: FakeLabelText(PRIMARY), FALLBACK: FakeLabelText(FALLBACK)}
    client, _ = client_for(tmp_path, sets, services=services)
    with client:
        services[PRIMARY].mode = services[FALLBACK].mode = "raise"
        decide(client)
        scraped = client.get("/metrics").text

    assert 'ml_label_requests_total{labelset="mood",served_by="none"} 1.0' in scraped


# ---- the centroid ------------------------------------------------------------------------------------------------------------------
def test_a_centroid_is_the_renormalised_mean_of_unit_vectors_whatever_the_examples_lengths():
    """Label `a` has examples at +45 degrees, -45 degrees and 0 with lengths 2.83, 1.41 and 5; label `b` is three vectors on the y axis of lengths
    1, 2 and 3. Normalising each example first makes the mean direction the x axis exactly; without it the long vector drags the direction off
    it, and without renormalising the mean the centroid would be shorter than a unit vector and every similarity to it too small."""
    labelset = parse(labels={"a": ["a1", "a2", "a3"], "b": ["b1", "b2", "b3"]})
    assert isinstance(labelset, labelsets.LabelSet)
    vectors = np.array([[2, 2], [1, -1], [5, 0], [0, 1], [0, 2], [0, 3]], dtype=np.float64)

    index = labelsets.build_index(labelset, vectors, "r" * 40, 0.5)

    assert index.labels == ("a", "b") and index.min_similarity == 0.5 and index.revision == "r" * 40
    assert np.linalg.norm(index.centroids, axis=1) == pytest.approx([1.0, 1.0])
    assert index.centroids == pytest.approx(np.array([[1.0, 0.0], [0.0, 1.0]]), abs=1e-12)


def test_the_similarity_is_the_cosine_so_a_longer_query_is_not_nearer():
    labelset = parse(labels={"a": ["a1", "a2", "a3"], "b": ["b1", "b2", "b3"]})
    assert isinstance(labelset, labelsets.LabelSet)
    index = labelsets.build_index(labelset, np.array([[1, 0]] * 3 + [[0, 1]] * 3, dtype=np.float64), "r" * 40, 0.5)
    short = labelsets.label_results([Embedded(np.array([1.0, 1.0]), 1, False)], index)[0]
    long = labelsets.label_results([Embedded(np.array([100.0, 100.0]), 1, False)], index)[0]

    assert short["similarity"] == pytest.approx(long["similarity"]) == pytest.approx(1 / math.sqrt(2))


# ---- a file the service cannot read -------------------------------------------------------------------------------------------------
def test_a_set_that_is_not_text_or_cannot_be_read_is_that_sets_refusal_and_nothing_else(tmp_path):
    sets_dir = tmp_path / "labelsets"
    write_set(sets_dir)
    (sets_dir / "binary.json").write_bytes(b"\xff\xfe\x00\x01")
    (sets_dir / "folder.json").mkdir()  # reading a directory as a file is an OSError

    found = read_labelsets(sets_dir)

    assert isinstance(found["mood"], labelsets.LabelSet)
    assert isinstance(found["binary"], Refused) and found["binary"].reason.startswith("unreadable (UnicodeDecodeError")
    assert isinstance(found["folder"], Refused) and found["folder"].reason.startswith("unreadable (IsADirectoryError")
