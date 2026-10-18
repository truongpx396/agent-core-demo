"""scripts/model_eval/head.py with a fake embedder: no model, no network (specs/014 T070). The head files it writes are also loaded by the
ML service's own reader, because a head that the service would refuse is a head nobody can use."""
import hashlib
import inspect
import json

import numpy as np
import pytest

from ml_service import classify as service
from scripts.model_eval import head
from scripts.model_eval.head import ModelInfo, TrainingRefused, train_head, write_head
from scripts.model_eval.routing import Outcome

DIM = 16
MODEL = ModelInfo("bge-small-en-v1.5", "BAAI/bge-small-en-v1.5", "onnx/model.onnx", "5c38ec7c405ec4b44b94cc5a9bb96e735b38267a", DIM)
WORDS = [f"w{i:03d}" for i in range(400)]
DATASET = {"dataset": "route-domains", "version": "v1"}
WHEN = "2026-10-19T12:00:00Z"


def fake_embed(texts):
    """Vectors with a known separating direction: prompts containing the word `hard` lean +3 on axis 0, the others -3, plus per-text noise."""
    out = []
    for text in texts:
        rng = np.random.default_rng(int(hashlib.sha256(text.encode()).hexdigest()[:8], 16))
        v = rng.normal(size=DIM) * 0.3
        v[0] += 3.0 if "hard" in text.split() else -3.0
        out.append(v)
    return np.array(out)


def items(prefix, n, *, hard, seed):
    """`n` prompts of six distinct words each (never near-duplicates of one another) with the outcomes that make them hard or easy."""
    rng = np.random.default_rng(seed)
    out = []
    for i in range(n):
        words = [WORDS[j] for j in rng.choice(len(WORDS), size=rng.integers(4, 9), replace=False)]
        if hard:
            words.append("hard")
        outcome = Outcome(f"{prefix}{i}", 0.2, 0.9, "judge") if hard else Outcome(f"{prefix}{i}", 0.8, 0.85, "judge")
        out.append((" ".join(words), outcome))
    return out


def sets(train_n=20, held_n=12):
    train = items("tr-h", train_n // 2, hard=True, seed=1) + items("tr-e", train_n // 2, hard=False, seed=2)
    held = items("te-h", held_n // 2, hard=True, seed=3) + items("te-e", held_n // 2, hard=False, seed=4)
    return train, held


def train(**kw):
    train_items, held = sets()
    args = dict(model=MODEL, embed=fake_embed, train=train_items, heldout=held, dataset=DATASET, outcomes_sha256="f" * 64)
    return train_head(**{**args, **kw})


# ---- the fit -----------------------------------------------------------------------------------------------------------------------
def test_the_fit_reaches_the_optimum_of_its_own_loss():
    rng = np.random.default_rng(0)
    x = head.l2_normalise(rng.normal(size=(40, 6)))
    y = (x[:, 0] + 0.3 * rng.normal(size=40) > 0).astype(float)
    w, b = head.fit_logistic(x, y, l2=0.5)

    p = head.sigmoid(x @ w + b)
    gradient = np.concatenate([x.T @ (p - y) + 0.5 * w, [np.sum(p - y)]])
    assert np.abs(gradient).max() < 1e-6  # a stationary point of a convex loss: the optimum, not merely "some" solution


def test_the_loss_never_goes_up_and_perfectly_separable_data_still_gives_finite_weights():
    x = head.l2_normalise(np.array([[1.0, 0.1], [0.9, -0.1], [-1.0, 0.1], [-0.9, -0.1]]))
    y = np.array([1.0, 1.0, 0.0, 0.0])

    w, b = head.fit_logistic(x, y, l2=0.01)

    assert np.isfinite(w).all() and np.isfinite(b)  # the penalty keeps the weights bounded where plain logistic regression diverges
    assert head._loss(x, y, w, b, 0.01) < head._loss(x, y, np.zeros(2), 0.0, 0.01)


def test_the_chosen_penalty_is_deterministic_and_comes_from_the_grid():
    train_items, _ = sets()
    x = head.l2_normalise(fake_embed([t for t, _ in train_items]))
    y = np.array([1.0 if "hard" in t.split() else 0.0 for t, _ in train_items])

    assert head.choose_l2(x, y, seed=7) == head.choose_l2(x, y, seed=7) in head.L2_GRID


def test_with_too_few_of_a_class_to_cross_validate_the_default_penalty_is_used():
    x = head.l2_normalise(np.random.default_rng(0).normal(size=(10, 4)))
    y = np.array([1.0] + [0.0] * 9)

    assert head.choose_l2(x, y) == head.DEFAULT_L2


def test_it_recovers_a_known_separating_direction_and_scores_the_held_out_prompts_right():
    trained = train()

    direction = np.zeros(DIM)
    direction[0] = 1.0
    cosine = float(trained.w @ direction / np.linalg.norm(trained.w))
    held = trained.provenance["heldout"]
    assert cosine > 0.9  # the weight vector points along the axis that really separates the classes
    assert held["accuracy"] == 1.0 and held["macro_f1"] == 1.0 and held["items"] == 12


def test_the_head_beats_a_random_router_and_one_that_escalates_the_longest_prompts_first():
    provenance = train().provenance

    beaten = provenance["controls_beaten"]
    held = provenance["heldout"]
    assert beaten == {"random": True, "length": True}
    assert held["apgr"] > held["controls"]["random"]["apgr"] == pytest.approx(0.5)
    assert held["cpt"]["50"] is not None and held["cpt"]["50"] < held["controls"]["random"]["cpt"]["50"]


def test_when_the_strong_model_is_no_better_on_the_held_out_prompts_the_gap_is_undefined_not_a_number():
    train_items, held = sets()
    held = [(text, Outcome(o.id, 0.7, 0.7, "judge")) for text, o in held]  # no gap to recover

    trained = train_head(model=MODEL, embed=fake_embed, train=train_items, heldout=held, dataset=DATASET, outcomes_sha256="0" * 64)

    report = trained.provenance["heldout"]
    assert report["pgr_curve"] is None and report["apgr"] is None and "undefined" in report["performance_gap"]
    assert trained.provenance["controls_beaten"] == {"random": None, "length": None}  # unknown, not "false" and not "true"


# ---- what it refuses ---------------------------------------------------------------------------------------------------------------
def test_it_refuses_to_train_on_a_prompt_it_is_reported_on(tmp_path):
    train_items, held = sets()
    leaked = train_items + [held[0]]  # one held-out prompt slipped into the training set

    with pytest.raises(TrainingRefused, match="overlap the held-out split"):
        train(train=leaked)


def test_a_near_duplicate_of_a_held_out_prompt_counts_as_overlap():
    train_items, held = sets()
    text, outcome = held[0]
    near = (text + " please", Outcome("near", 0.2, 0.9, "judge"))  # one extra word: Jaccard stays above the near-duplicate threshold

    with pytest.raises(TrainingRefused, match="near-duplicate"):
        train(train=train_items + [near])


def test_prompts_with_a_missing_score_are_dropped_and_counted_never_treated_as_zero():
    train_items, held = sets()
    train_items[0] = (train_items[0][0], Outcome("x", None, 0.9, "judge"))

    trained = train(train=train_items)

    assert trained.provenance["trained_on"]["items"] == len(train_items) - 1
    assert trained.provenance["trained_on"]["dropped_missing_scores"] == 1


def test_too_few_labelled_prompts_is_not_a_training_set():
    train_items, _ = sets()

    with pytest.raises(TrainingRefused, match="at least 8"):
        train(train=train_items[:5])


def test_a_training_set_with_one_label_is_refused():
    only_hard = items("h", 12, hard=True, seed=9)

    with pytest.raises(TrainingRefused, match="same label"):
        train(train=only_hard)


def test_a_heldout_split_with_no_scored_prompt_is_refused_because_there_is_nothing_to_report_on():
    held = [(t, Outcome(o.id, None, None, "judge")) for t, o in sets()[1]]

    with pytest.raises(TrainingRefused, match="nothing to report the head on"):
        train(heldout=held)


def test_a_wrong_dimension_from_the_embedder_is_refused():
    with pytest.raises(TrainingRefused, match="declared 384-d"):
        train(model=ModelInfo(MODEL.name, MODEL.repo, MODEL.file, MODEL.revision, 384))


# ---- the files ---------------------------------------------------------------------------------------------------------------------
def test_equal_inputs_give_equal_bytes_in_both_files(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"

    write_head(a, train(), trained_at=WHEN)
    write_head(b, train(), trained_at=WHEN)

    for name in ("head-bge-small-en-v1.5.npz", "head-bge-small-en-v1.5.json"):
        assert (a / name).read_bytes() == (b / name).read_bytes()


def test_the_weights_file_is_a_plain_float32_npz_that_loads_without_pickle(tmp_path):
    trained = train()
    _json, npz = write_head(tmp_path, trained, trained_at=WHEN)

    with np.load(npz, allow_pickle=False) as arrays:
        assert arrays["w"].dtype == np.float32 and arrays["w"].shape == (DIM,)
        assert arrays["b"].dtype == np.float32 and arrays["b"].shape == ()
        np.testing.assert_array_equal(arrays["w"], trained.w)


def test_the_writer_cannot_emit_an_accepted_head_and_the_file_says_false(tmp_path):
    json_path, _ = write_head(tmp_path, train(), trained_at=WHEN)

    assert "accepted" not in inspect.signature(write_head).parameters  # there is no argument that could set it
    assert json.loads(json_path.read_text())["accepted"] is False
    assert not hasattr(head, "accept")


def test_the_json_records_where_the_head_came_from(tmp_path):
    json_path, _ = write_head(tmp_path, train(), trained_at=WHEN)

    doc = json.loads(json_path.read_text())
    assert (doc["model"], doc["model_repo"], doc["model_file"], doc["dim"]) == (MODEL.name, MODEL.repo, MODEL.file, DIM)
    assert doc["model_revision"] == MODEL.revision and len(doc["model_revision"]) == 40
    assert doc["trained_on"]["outcomes_sha256"] == "f" * 64 and doc["trained_on"]["margin"] == 0.2 and doc["seed"] == head.SEED
    assert doc["head_version"].startswith("2026-10-19.") and doc["labels"] == {"0": "local_ok", "1": "needs_stronger"}
    assert set(doc["controls_beaten"]) == {"random", "length"} and doc["method"]["iterations"] == head.ITERATIONS


def test_a_person_accepted_head_is_not_silently_overwritten(tmp_path):
    json_path, _ = write_head(tmp_path, train(), trained_at=WHEN)
    doc = json.loads(json_path.read_text())
    doc["accepted"] = True  # what a person does
    json_path.write_text(json.dumps(doc))
    before = json_path.read_bytes()

    with pytest.raises(TrainingRefused, match="already ACCEPTED"):
        write_head(tmp_path, train(), trained_at=WHEN)

    assert json_path.read_bytes() == before
    write_head(tmp_path, train(), trained_at=WHEN, force=True)  # an explicit choice replaces it, and the new file is unaccepted
    assert json.loads(json_path.read_text())["accepted"] is False


def test_an_unaccepted_head_is_simply_replaced(tmp_path):
    write_head(tmp_path, train(), trained_at=WHEN)

    write_head(tmp_path, train(), trained_at="2026-10-20T00:00:00Z")

    assert json.loads((tmp_path / "head-bge-small-en-v1.5.json").read_text())["head_version"].startswith("2026-10-20.")


# ---- the service accepts what this writes ------------------------------------------------------------------------------------------
def test_the_schema_version_is_the_one_the_service_reads():
    assert head.HEAD_SCHEMA_VERSION == service.HEAD_SCHEMA_VERSION


def test_the_service_refuses_the_written_head_until_a_person_accepts_it(tmp_path):
    write_head(tmp_path, train(), trained_at=WHEN)

    refused = service.read_head(tmp_path, MODEL.name)

    assert isinstance(refused, service.Refused) and refused.reason == "not accepted"


def test_the_service_loads_the_written_head_once_accepted_and_it_binds_to_the_model_it_was_trained_on(tmp_path):
    trained = train()
    json_path, _ = write_head(tmp_path, trained, trained_at=WHEN)
    doc = json.loads(json_path.read_text())
    doc["accepted"] = True  # the one thing only a person does
    json_path.write_text(json.dumps(doc))

    loaded = service.read_head(tmp_path, MODEL.name)

    assert isinstance(loaded, service.Head)
    assert (loaded.model, loaded.repo, loaded.file, loaded.revision, loaded.dim) == (MODEL.name, MODEL.repo, MODEL.file, MODEL.revision, DIM)
    np.testing.assert_allclose(loaded.w, trained.w.astype(np.float64))
    assert loaded.b == pytest.approx(trained.b) and loaded.threshold == 0.5 and loaded.version == doc["head_version"]


def test_the_service_scores_a_prompt_the_way_the_trainer_scored_the_held_out_set(tmp_path):
    """The trainer's held-out score and the service's `sigmoid(w.x + b)` over the L2-normalised vector must be the same number."""
    trained = train()
    json_path, _ = write_head(tmp_path, trained, trained_at=WHEN)
    doc = json.loads(json_path.read_text())
    doc["accepted"] = True
    json_path.write_text(json.dumps(doc))
    loaded = service.read_head(tmp_path, MODEL.name)
    text = sets()[1][0][0]

    vector = head.l2_normalise(fake_embed([text]))[0]
    by_trainer = float(head.sigmoid(vector @ trained.w.astype(np.float64) + trained.b))
    by_service = 1.0 / (1.0 + np.exp(-(float(vector @ loaded.w) + loaded.b)))

    assert by_service == pytest.approx(by_trainer, abs=1e-9)


def test_the_weights_file_carries_a_fixed_timestamp_so_equal_inputs_can_never_differ_by_the_clock():
    import io
    import zipfile

    with zipfile.ZipFile(io.BytesIO(head.npz_bytes(np.ones(4), 0.5))) as archive:
        assert {info.date_time for info in archive.infolist()} == {(1980, 1, 1, 0, 0, 0)}  # not "now": np.savez would stamp the current time


def test_the_stored_weights_are_the_optimum_of_the_loss_on_the_NORMALISED_training_vectors():
    """A trainer that fitted raw (unnormalised) vectors would still score its own held-out set well, and would serve different numbers than it
    reported, because the service normalises before it scores. The stored weights must be a stationary point on the vectors the service uses."""
    train_items, _ = sets()
    trained = train()
    x = head.l2_normalise(fake_embed([t for t, _ in train_items]))
    y = np.array([1.0 if "hard" in t.split() else 0.0 for t, _ in train_items])
    l2 = trained.provenance["method"]["l2"]
    w, b = trained.w.astype(np.float64), trained.b

    p = head.sigmoid(x @ w + b)
    gradient = np.concatenate([x.T @ (p - y) + l2 * w, [np.sum(p - y)]])

    assert np.abs(gradient).max() < 1e-3  # float32 storage is the only slack


def test_the_margin_the_operator_chose_is_the_margin_the_labels_use():
    """Items here have a strong-minus-local gap of 0.7 (hard) or 0.05 (easy). At the default margin 0.2 half need the stronger model; at 0.8
    none do, so there is nothing to learn. A trainer that recorded the margin but labelled with the default would train happily at 0.8."""
    assert train(margin=0.2).provenance["trained_on"]["needs_stronger_share"] == 0.5

    with pytest.raises(TrainingRefused, match="same label.*margin 0.8"):
        train(margin=0.8)


def test_each_newton_step_lowers_the_loss_even_on_a_problem_where_a_full_step_would_overshoot():
    """Found by search (seed 257): from zero, an undamped Newton step RAISES the loss on this nearly unregularised problem. The halving line
    search is what keeps the fit monotone, so the loss after k steps must never exceed the loss after k-1."""
    rng = np.random.default_rng(257)
    n, d = int(rng.integers(6, 14)), int(rng.integers(2, 5))
    x = rng.normal(size=(n, d)) * rng.choice([0.3, 1, 4])
    y = (rng.random(n) < 0.5).astype(float)
    l2 = 1e-4

    losses = [head._loss(x, y, *head.fit_logistic(x, y, l2, k), l2) for k in range(8)]

    assert all(later <= earlier + 1e-9 for earlier, later in zip(losses, losses[1:], strict=False)), losses
    # Not merely "never up": a fit that gave up at zero would also be flat. The damped fit takes the loss from 8.3 to 0.28 and reaches the optimum.
    assert losses[-1] < 0.5 * losses[0]
    w, b = head.fit_logistic(x, y, l2)
    p = head.sigmoid(x @ w + b)
    assert np.abs(np.concatenate([x.T @ (p - y) + l2 * w, [np.sum(p - y)]])).max() < 1e-6
