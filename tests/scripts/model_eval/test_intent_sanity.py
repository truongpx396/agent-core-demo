"""The intent method-sanity check (specs/014 T072): CLINC-shaped data, a fake embedder, no model and no network.

What is real here: the parquet reader (on a file written with the same schema metadata the Hub's files carry), the multinomial trainer, the
suite's arithmetic, the out-of-scope threshold choice and the full `run` through the CLI and the report schema. What is fake: the embedder.
"""
import dataclasses
import hashlib
import json

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from scripts.model_eval import __main__ as cli
from scripts.model_eval import datasets as ds
from scripts.model_eval import head, metrics, public
from scripts.model_eval.runners import classify
from scripts.model_eval.suites import (
    INTENT_DATASETS,
    IntentSuite,
    ItemData,
    fast_macro_f1,
    suite_for,
)

ID = "clinc-oos-plus"
REPO = "clinc/clinc_oos"
REVISION = "155b9c710419136e17307b80d0a13e68cd46b4ec"
NAMES = ["alpha", "beta", "gamma", "delta", "oos", "epsilon"]  # the OOS class is deliberately NOT last: nothing may assume where it is
OOS = NAMES.index("oos")
INTENTS = [i for i in range(len(NAMES)) if i != OOS]
DIM = 12
MODEL = head.ModelInfo("bge-small-en-v1.5", "BAAI/bge-small-en-v1.5", "onnx/model.onnx", "5c38ec7c405ec4b44b94cc5a9bb96e735b38267a", DIM)


# ---- a CLINC-shaped cache --------------------------------------------------------------------------------------------------------------
def fake_embed(texts):
    """Text `intentK ...` lands near axis K; anything else (out-of-scope) lands in a direction of its own, away from every intent axis."""
    out = []
    for text in texts:
        rng = np.random.default_rng(int(hashlib.sha256(text.encode()).hexdigest()[:8], 16))
        v = rng.normal(size=DIM) * 0.25
        word = text.split()[0]
        if word.startswith("intent"):
            v[int(word[6:])] += 3.0
        else:
            v[DIM - 1] += 3.0
        out.append(v)
    return np.array(out)


def rows(split, per_intent, n_oos):
    r = [{"text": f"intent{k} {split} sample {i}", "intent": k} for k in INTENTS for i in range(per_intent)]
    return r + [{"text": f"unrelated {split} request {i}", "intent": OOS} for i in range(n_oos)]


def write_split(path, items):
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.table({"text": [i["text"] for i in items], "intent": [i["intent"] for i in items]})
    table = table.replace_schema_metadata({b"huggingface": json.dumps({"info": {"features": {"intent": {"names": NAMES}}}}).encode()})
    pq.write_table(table, path)


@pytest.fixture
def cache(tmp_path):
    """A pinned entry whose three parquet files are in a temporary cache, plus the lock file that names them."""
    root = tmp_path / "public"
    base = root / ID / REPO.replace("/", "__") / "plus"
    sizes = {"train": (20, 5), "validation": (6, 20), "test": (10, 30)}
    files = []
    for split, (per_intent, n_oos) in sizes.items():
        relpath = f"plus/{split}-00000-of-00001.parquet"
        write_split(base / f"{split}-00000-of-00001.parquet", rows(split, per_intent, n_oos))
        files.append({"path": relpath, "sha256": ds.sha256_file(base / f"{split}-00000-of-00001.parquet")})
    lock = tmp_path / "public.lock.json"
    lock.write_text(json.dumps([{
        "id": ID, "kind": "classifier", "licence": "CC-BY-3.0", "licence_source": "test", "redistribute": False, "notes": "",
        "sources": [{"hub_repo": REPO, "revision": REVISION, "files": files}],
    }]))
    return root, lock


# ---- the reader ---------------------------------------------------------------------------------------------------------------------
def test_the_reader_finds_the_out_of_scope_class_by_name_not_by_position(cache):
    root, lock = cache
    entry = ds.load_public_lock(lock)[0]

    data = public.load_clinc(entry, root)

    assert data.names == NAMES and data.oos_index == OOS
    assert len(data.test) == 5 * 10 + 30 and sum(i.oos for i in data.test) == 30  # five in-scope intents of ten, plus thirty out-of-scope
    assert all(i.oos == (i.intent == OOS) for i in data.train + data.validation + data.test)


def test_a_file_without_intent_names_is_refused_so_oos_cannot_be_guessed(cache):
    root, lock = cache
    entry = ds.load_public_lock(lock)[0]
    path = ds.cache_path(entry, entry.sources[0], entry.sources[0].files[0][0], root)
    pq.write_table(pa.table({"text": ["a"], "intent": [0]}), path)  # no schema metadata

    with pytest.raises(ds.DatasetError, match="no intent names"):
        public.load_clinc(entry, root)


def test_a_file_whose_names_have_no_oos_class_is_refused(cache):
    root, lock = cache
    entry = ds.load_public_lock(lock)[0]
    path = ds.cache_path(entry, entry.sources[0], entry.sources[0].files[0][0], root)
    table = pa.table({"text": ["a"], "intent": [0]}).replace_schema_metadata(
        {b"huggingface": json.dumps({"info": {"features": {"intent": {"names": ["a", "b"]}}}}).encode()})
    pq.write_table(table, path)

    with pytest.raises(ds.DatasetError, match="no `oos` class"):
        public.load_clinc(entry, root)


# ---- the trainer --------------------------------------------------------------------------------------------------------------------
def blobs(n_per=40, k=5, d=8, seed=0):
    rng = np.random.default_rng(seed)
    centres = rng.normal(size=(k, d)) * 3.0
    x = np.vstack([centres[c] + rng.normal(size=(n_per, d)) * 0.6 for c in range(k)])
    return x, np.repeat(np.arange(k), n_per)


def test_the_softmax_fit_separates_well_separated_classes():
    x, y = blobs()
    (z,) = head.standardise(x)

    w, b = head.fit_softmax(z, y, 5)

    assert (head.softmax(z @ w + b).argmax(axis=1) == y).mean() > 0.97


def test_the_softmax_fit_is_deterministic_and_more_iterations_lower_the_loss():
    x, y = blobs()
    (z,) = head.standardise(x)

    def loss(w, b):
        return float(-np.log(head.softmax(z @ w + b)[np.arange(len(y)), y]).mean())

    first, second = head.fit_softmax(z, y, 5, iterations=50), head.fit_softmax(z, y, 5, iterations=50)
    longer = head.fit_softmax(z, y, 5, iterations=200)

    np.testing.assert_array_equal(first[0], second[0])
    assert loss(*longer) < loss(*first) < loss(np.zeros((8, 5)), np.zeros(5))


def test_standardising_uses_the_training_rows_statistics_for_every_split():
    train = np.array([[1.0, 10.0], [3.0, 10.0]])
    other = np.array([[2.0, 12.0]])

    z_train, z_other = head.standardise(train, other)

    np.testing.assert_allclose(z_train.mean(axis=0), [0.0, 0.0])
    np.testing.assert_allclose(z_other, [[(2.0 - 2.0) / 1.0, (12.0 - 10.0) / 1.0]])  # std of a constant column is taken as 1, not 0


# ---- the suite ----------------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("seed", range(25))
def test_the_fast_macro_f1_is_the_reference_macro_f1(seed):
    rng = np.random.default_rng(seed)
    k = int(rng.integers(2, 40))
    label = rng.integers(0, k, size=200)
    pred = np.where(rng.random(200) < 0.6, label, rng.integers(0, k + 3, size=200))  # includes classes that are only ever predicted

    assert fast_macro_f1(label, pred) == pytest.approx(metrics.macro_f1(label.tolist(), pred.tolist()).value)


def intent_data(**override):
    arrays = {
        "pred": np.array([0, 1, 2, 0, 3, 3, 3, 3], dtype=float),
        "label": np.array([0, 1, 1, 0, -1, -1, -1, -1], dtype=float),
        "confidence": np.array([0.9, 0.8, 0.6, 0.95, 0.2, 0.4, 0.7, 0.3]),
        "oos": np.array([0, 0, 0, 0, 1, 1, 1, 1], dtype=float),
        "oos_threshold": np.full(8, 0.5),
    }
    arrays.update(override)
    return ItemData([f"i{n}" for n in range(8)], arrays)


def test_the_intent_metrics_are_hand_computed_and_out_of_scope_is_scored_apart_from_in_scope():
    values = IntentSuite().compute(intent_data(), np.arange(8))

    assert values["accuracy_in_scope"] == 0.75  # 3 of the 4 in-scope items are right
    assert values["oos_recall@thr"] == 0.75  # confidences 0.2, 0.4, 0.3 fall under 0.5 and 0.7 does not
    assert values["in_scope_reject@thr"] == 0.0  # every in-scope confidence is at least 0.6
    assert values["macro_f1_in_scope"] == pytest.approx(metrics.macro_f1([0, 1, 1, 0], [0, 1, 2, 0]).value)
    expected_ece, _ = metrics.expected_calibration_error(np.array([0.9, 0.8, 0.6, 0.95]), np.array([True, True, False, True]))
    assert values["ece_in_scope"] == pytest.approx(expected_ece)


def test_out_of_scope_items_never_touch_the_in_scope_figures():
    base = IntentSuite().compute(intent_data(), np.arange(8))
    changed = IntentSuite().compute(intent_data(pred=np.array([0, 1, 2, 0, 0, 1, 2, 3], dtype=float)), np.arange(8))

    for name in ("accuracy_in_scope", "macro_f1_in_scope", "ece_in_scope", "in_scope_reject@thr"):
        assert changed[name] == base[name]


def test_a_resample_with_no_in_scope_item_has_no_value_rather_than_a_wrong_one():
    values = IntentSuite().compute(intent_data(), np.array([4, 5, 6]))

    assert all(np.isnan(v) for v in values.values())


def test_the_suite_follows_the_dataset():
    assert isinstance(suite_for("classifier", dataset=INTENT_DATASETS[0]), IntentSuite)
    assert not isinstance(suite_for("classifier", dataset="route-domains"), IntentSuite)


def test_the_threshold_is_the_value_that_separates_the_two_kinds_of_item_best_and_the_lowest_wins_a_tie():
    confidence = np.array([0.9, 0.85, 0.8, 0.3, 0.25, 0.2])
    oos = np.array([False, False, False, True, True, True])

    # Caught = out-of-scope items strictly under the threshold; kept = in-scope items at or over it. At 0.31 the three out-of-scope items (0.3, 0.25,
    # 0.2) are all caught and the three in-scope ones (0.9, 0.85, 0.8) all kept, and that holds up to 0.8; at 0.30 the item at exactly 0.3 is not
    # caught. So the lowest best value is 0.31.
    assert classify._pick_oos_threshold(confidence, oos) == 0.31


# ---- the whole run ------------------------------------------------------------------------------------------------------------------
@pytest.fixture
def run_env(cache, monkeypatch, tmp_path):
    root, lock = cache
    monkeypatch.setattr(classify, "service_embedder", lambda name: (fake_embed, MODEL))
    original = cli._settings
    monkeypatch.setattr(cli, "_settings", lambda args: dataclasses.replace(original(args), public_root=root, public_lock=lock))
    return tmp_path / "reports"


def run_cli(out, candidate="bge-small-en-v1.5"):
    return cli.main(["run", "--kind", "classifier", "--candidate", candidate, "--dataset", ID, "--runs", "1", "--no-compare", "--out", str(out)])


def test_the_whole_check_runs_and_writes_a_valid_report(run_env, capsys):
    assert run_cli(run_env) == cli.OK, capsys.readouterr()

    report = json.loads(next(run_env.glob("*.json")).read_text())
    q = {k: v["value"] for k, v in report["quality"].items()}
    assert report["kind"] == "classifier" and report["dataset"]["name"] == ID and report["dataset"]["version"] == REVISION[:12]
    assert report["dataset"]["public"] == [{"id": ID, "revision": REVISION, "licence": "CC-BY-3.0"}]
    assert q["accuracy_in_scope"] > 0.95 and q["macro_f1_in_scope"] > 0.95
    assert set(q) == {"accuracy_in_scope", "macro_f1_in_scope", "ece_in_scope", "oos_recall@thr", "in_scope_reject@thr"}
    assert [c["name"] for c in report["controls"]] == ["identity", "shuffled-predictions"] and all(c["ok"] for c in report["controls"])
    assert any("method sanity only" in t for t in report["tradeoffs"]) and any("scored separately" in t for t in report["tradeoffs"])


def test_the_out_of_scope_class_is_never_predicted_it_is_what_a_low_confidence_means(run_env, monkeypatch):
    seen = {}
    real = ItemData

    def spy(ids, arrays):
        seen.update(arrays)
        return real(ids, arrays)

    monkeypatch.setattr(classify, "ItemData", spy)

    run_cli(run_env)

    assert OOS not in set(seen["pred"].astype(int))
    assert (seen["label"][seen["oos"] == 1] == -1).all()


def test_the_check_is_for_in_process_embedding_models_only(run_env, capsys):
    assert run_cli(run_env, "ml-service-classify") == cli.MISSING
    assert "inprocess" in capsys.readouterr().err
    assert run_cli(run_env, "constant") == cli.MISSING
    assert "control for the routing evaluation" in capsys.readouterr().err


def test_an_uncached_dataset_says_what_to_fetch_and_writes_no_report(run_env, monkeypatch, capsys, cache):
    root, _ = cache
    next(root.rglob("*.parquet")).unlink()

    assert run_cli(run_env) == cli.MISSING

    assert "make model-eval-data" in capsys.readouterr().err and not run_env.exists()


def test_a_pipeline_no_better_than_shuffled_predictions_makes_the_report_invalid(run_env, monkeypatch):
    """The sanity check must be able to FAIL: an embedder that carries no information cannot pass as a sensible pipeline."""
    monkeypatch.setattr(classify, "service_embedder", lambda name: (lambda texts: np.random.default_rng(0).normal(size=(len(texts), DIM)), MODEL))

    assert run_cli(run_env) == cli.CONTROL_FAILED

    report = json.loads(next(run_env.glob("*.json")).read_text())
    assert report["status"] == "invalid" and any(not c["ok"] for c in report["controls"])


def test_the_intent_head_is_trained_on_in_scope_examples_only(run_env, monkeypatch):
    """CLINC's out-of-scope examples are what the head must NOT recognise; training on them as a class would teach it that "none of these" is
    an intent and make the out-of-scope figures meaningless."""
    seen = {}
    real = classify.fit_softmax

    def spy(x, y, n_classes, **kwargs):
        seen["labels"], seen["rows"] = set(np.asarray(y).tolist()), len(y)
        return real(x, y, n_classes, **kwargs)

    monkeypatch.setattr(classify, "fit_softmax", spy)

    run_cli(run_env)

    assert OOS not in seen["labels"] and seen["rows"] == 5 * 20  # five in-scope intents of twenty training examples; the five OOS ones are left out


def test_the_out_of_scope_threshold_is_chosen_on_validation_never_on_the_test_split(run_env, monkeypatch):
    calls = []
    real = classify._pick_oos_threshold

    def spy(confidence, oos):
        calls.append((len(confidence), int(np.asarray(oos).sum())))
        return real(confidence, oos)

    monkeypatch.setattr(classify, "_pick_oos_threshold", spy)

    run_cli(run_env)

    assert calls == [(5 * 6 + 20, 20)]  # the validation split: thirty in-scope items and twenty out-of-scope; the test split has eighty items
