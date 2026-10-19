"""The intent / topic classifier suite and the CLINC reader (specs/014 T072): CLINC-shaped data, no model and no network.

What is real here: the parquet reader (on a file written with the same schema metadata the Hub's files carry), the suite's arithmetic with
out-of-scope items scored apart from in-scope ones, and the shuffled-predictions control. Nothing here trains or embeds anything: the runner
that used to (a head trained on CLINC) was removed with the head trainer, and the suite waits for the topic-label classifier that will use it.

`pyarrow` is imported lazily: only the CI `test` job installs it, and a module-level import made every other pytest job fail at collection.
"""
import json

import numpy as np
import pytest

from scripts.model_eval import datasets as ds
from scripts.model_eval import metrics, public
from scripts.model_eval.controls import derived_controls
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


# ---- a CLINC-shaped cache --------------------------------------------------------------------------------------------------------------
def rows(split, per_intent, n_oos):
    r = [{"text": f"intent{k} {split} sample {i}", "intent": k} for k in INTENTS for i in range(per_intent)]
    return r + [{"text": f"unrelated {split} request {i}", "intent": OOS} for i in range(n_oos)]


def arrow():
    return pytest.importorskip("pyarrow"), pytest.importorskip("pyarrow.parquet")


def write_split(path, items):
    pa, pq = arrow()
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
    pa, pq = arrow()
    root, lock = cache
    entry = ds.load_public_lock(lock)[0]
    path = ds.cache_path(entry, entry.sources[0], entry.sources[0].files[0][0], root)
    pq.write_table(pa.table({"text": ["a"], "intent": [0]}), path)  # no schema metadata

    with pytest.raises(ds.DatasetError, match="no intent names"):
        public.load_clinc(entry, root)


def test_a_file_whose_names_have_no_oos_class_is_refused(cache):
    pa, pq = arrow()
    root, lock = cache
    entry = ds.load_public_lock(lock)[0]
    path = ds.cache_path(entry, entry.sources[0], entry.sources[0].files[0][0], root)
    table = pa.table({"text": ["a"], "intent": [0]}).replace_schema_metadata(
        {b"huggingface": json.dumps({"info": {"features": {"intent": {"names": ["a", "b"]}}}}).encode()})
    pq.write_table(table, path)

    with pytest.raises(ds.DatasetError, match="no `oos` class"):
        public.load_clinc(entry, root)


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


# ---- the control ------------------------------------------------------------------------------------------------------------------------
def classifier_data(informative: bool, seed: int = 0):
    """60 items: 48 in-scope over five intents and 12 out-of-scope. An informative classifier is right 90% of the time; an uninformative one guesses."""
    rng = np.random.default_rng(seed)
    label = np.concatenate([rng.integers(0, 5, size=48), np.full(12, -1)])
    right = rng.random(60) < 0.9
    pred = np.where(right if informative else np.zeros(60, dtype=bool), label, rng.integers(0, 5, size=60))
    pred = np.where(label < 0, rng.integers(0, 5, size=60), pred)
    return ItemData([f"c{n}" for n in range(60)], {
        "pred": pred.astype(float), "label": label.astype(float), "confidence": rng.random(60),
        "oos": (label < 0).astype(float), "oos_threshold": np.full(60, 0.5),
    })


def test_a_classifier_that_learned_something_beats_its_own_shuffled_predictions():
    results = derived_controls("classifier", IntentSuite(), classifier_data(informative=True))

    assert [r.name for r in results] == ["identity", "shuffled-predictions"] and all(r.ok for r in results)


def test_the_control_can_fail_a_classifier_whose_predictions_carry_no_information_is_not_better_than_chance():
    """The check must be able to say INVALID: with predictions unrelated to the labels, shuffling them changes nothing."""
    results = derived_controls("classifier", IntentSuite(), classifier_data(informative=False))

    assert not next(r for r in results if r.name == "shuffled-predictions").ok
