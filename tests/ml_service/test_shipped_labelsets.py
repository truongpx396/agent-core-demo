"""The label sets this repository ships are valid, complete for the models that serve them, and agree with the dataset that judges them
(specs/015 FR-003, FR-004, FR-010).

These are the checks a reviewer would otherwise do by eye each time a set or `models.toml` changes: a set the service refuses starts as a quiet
`refused` line in `/health/models`, a model added to the classify chain without a cutoff silently stops serving the set, and a dataset sentence
that is also an example would be scored against itself.
"""
import json
import tomllib
from pathlib import Path

import pytest

from ml_service import labelsets
from scripts.model_eval import datasets as ds
from scripts.model_eval.build import topics as build_topics

SHIPPED = Path(labelsets.__file__).resolve().parent.parent / "labelsets"
MODELS_TOML = Path(labelsets.__file__).resolve().parent.parent / "models.toml"
SETS = sorted(SHIPPED.glob("*.json"))


@pytest.mark.parametrize("path", SETS, ids=lambda p: p.stem)
def test_every_shipped_set_is_accepted_by_the_services_own_parser(path):
    parsed = labelsets.parse_labelset(path.read_text(), path.stem)

    assert isinstance(parsed, labelsets.LabelSet), getattr(parsed, "reason", parsed)


@pytest.mark.parametrize("path", SETS, ids=lambda p: p.stem)
def test_every_model_in_the_classify_chain_has_a_cutoff_for_every_shipped_set(path):
    """A model with no entry does not serve the set. For the fallback that is a silent loss of redundancy, so adding a model to the chain
    (or a set to the image) without its cutoff fails here, not in production at 3 a.m. when the primary is down."""
    chain = [m["name"] for m in tomllib.loads(MODELS_TOML.read_text())["model"] if "classify" in m["roles"] and m.get("enabled", True)]
    parsed = labelsets.parse_labelset(path.read_text(), path.stem)
    assert isinstance(parsed, labelsets.LabelSet)

    assert chain, "models.toml has no enabled classify-role model"
    assert set(chain) <= set(parsed.calibration), f"{path.name} has no calibration for {sorted(set(chain) - set(parsed.calibration))}"
    assert set(parsed.calibration) <= {m["name"] for m in tomllib.loads(MODELS_TOML.read_text())["model"]}, "a cutoff for a model models.toml does not declare"


@pytest.mark.parametrize("path", SETS, ids=lambda p: p.stem)
def test_a_shipped_set_names_no_tenant_the_way_the_evaluation_data_may_not(path):
    text = path.read_text().lower()

    assert [t for t in ds.TENANT_DENYLIST if t in text] == []


def test_the_routing_topics_and_their_dataset_have_the_same_labels_and_share_no_sentence():
    raw = json.loads((SHIPPED / "route-topics.json").read_text())
    manifest = ds.load_verified(ds.DATASETS_ROOT / "route-topics" / "v1")

    assert sorted(raw["labels"]) == sorted(manifest.raw["labels"])
    splits = {name: manifest.split_items(name) for name in ("dev", "test")}
    assert build_topics.leaks_into_the_label_set(splits, raw["labels"]) == [], "a dataset sentence is also (nearly) a label-set example"
    for split, items in splits.items():
        assert {i["label"] for i in items} == set(raw["labels"]) | {"none"}, f"the {split} split lacks a topic or `none`"


def test_the_routing_label_set_shares_no_sentence_with_the_repositorys_route_prompts():
    """`route-domains` is what the router's behaviour on this repository's real traffic is read from (specs/015 research.md R4). An example that is
    also one of those prompts would make the router look right on the very sentence it was copied from."""
    raw = json.loads((SHIPPED / "route-topics.json").read_text())
    manifest = ds.load_verified(ds.DATASETS_ROOT / "route-domains" / "v1")
    prompts = {name: [i["text"] for i in manifest.split_items(name)] for name in ("dev", "test")}
    examples = [e for ex in raw["labels"].values() for e in ex]

    for name, texts in prompts.items():
        assert ds.near_duplicates(examples, texts) == [], f"a label-set example is (nearly) a route-domains {name} prompt"


def test_the_dataset_source_file_and_the_committed_dataset_agree():
    """The TSV is what a person edits and the JSONL is what is scored: rebuilding from the TSV must give the committed bytes, or someone edited one."""
    raw = json.loads((SHIPPED / "route-topics.json").read_text())
    splits = build_topics.build_splits(build_topics.read_rows(), list(raw["labels"]))
    manifest = ds.load_verified(ds.DATASETS_ROOT / "route-topics" / "v1")

    for name, items in splits.items():
        assert items == manifest.split_items(name)
