"""The shipped models.toml: what the service declares it can serve (specs/014 FR-022). Parsed with the same validation as at startup."""
from ml_service.app import MODELS_TOML
from ml_service.registry import enabled_specs, load_model_specs

SPECS = {s.name: s for s in load_model_specs(MODELS_TOML)}


def test_the_three_declared_models_are_present_with_their_licences():
    assert set(SPECS) == {"bge-small-en-v1.5", "all-MiniLM-L6-v2", "gte-modernbert-base-int8"}
    assert {n: s.licence for n, s in SPECS.items()} == {"bge-small-en-v1.5": "MIT", "all-MiniLM-L6-v2": "Apache-2.0", "gte-modernbert-base-int8": "Apache-2.0"}


def test_the_two_classifier_models_load_by_default_and_the_big_embedder_does_not():
    assert [s.name for s in enabled_specs(list(SPECS.values()), ())] == ["bge-small-en-v1.5", "all-MiniLM-L6-v2"]


def test_the_classify_chain_is_bge_small_then_minilm_in_that_order_and_the_big_embedder_does_not_classify():
    # the order IS the configuration: the first classify-role model is the primary (contracts/settings.md "Not settings")
    assert [n for n, s in SPECS.items() if "classify" in s.roles] == ["bge-small-en-v1.5", "all-MiniLM-L6-v2"]
    assert "embed" in SPECS["bge-small-en-v1.5"].roles and "embed" in SPECS["all-MiniLM-L6-v2"].roles
    assert SPECS["gte-modernbert-base-int8"].roles == ("embed",)


def test_the_environment_override_can_turn_the_others_on():
    assert [s.name for s in enabled_specs(list(SPECS.values()), ("gte-modernbert-base-int8",))] == ["gte-modernbert-base-int8"]


def test_each_pins_a_full_commit_and_names_its_exact_file_pooling_and_pad_token():
    for spec in SPECS.values():
        assert len(spec.revision) == 40 and spec.file.endswith(".onnx") and spec.pad_token and spec.max_length > 0
    assert SPECS["bge-small-en-v1.5"].pooling == "cls" and SPECS["all-MiniLM-L6-v2"].pooling == "mean"
    # the model cards: bge and gte use the CLS token, MiniLM averages; getting this wrong gives plausible-looking, wrong vectors


def test_the_default_files_are_fp32_because_int8_was_slower_on_the_cpu_this_was_measured_on():
    assert SPECS["bge-small-en-v1.5"].file == "onnx/model.onnx" and SPECS["all-MiniLM-L6-v2"].file == "onnx/model.onnx"
    assert "int8" in SPECS["gte-modernbert-base-int8"].file  # the one model whose small file is only the quantized one


def test_prefixes_are_documentation_the_server_does_not_apply():
    assert SPECS["bge-small-en-v1.5"].needs_prefix["query"].startswith("Represent this sentence")
