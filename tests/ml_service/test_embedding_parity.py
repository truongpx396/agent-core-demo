"""The served vectors against an INDEPENDENT implementation of the model card's recipe (specs/014 FR-025, T064).

Marker `integration`: it downloads the real bge-small-en-v1.5 (133 MB, pinned commit) and self-skips, never fails, when the Hugging Face
Hub is unreachable. What this proves and what it does not: `OnnxText` (tokenise, pad, truncate, pool, normalise) is checked against a
second, deliberately naive implementation of the same recipe written here; the ONNX graph itself is the same file in both, so a defect
INSIDE the graph would not show. What it catches is what actually goes wrong in a serving layer: the wrong pooling, padding that changes a
result, a normalisation forgotten, a tokenizer configured differently than the model was trained with.
"""
import numpy as np
import pytest

from ml_service.app import MODELS_TOML
from ml_service.registry import load_model_specs
from ml_service.settings import Settings
from ml_service.text_model import load_text_model

pytestmark = pytest.mark.integration

TEXTS = [
    "How do I reset my password?",
    "To reset your password, open Settings and choose Reset password.",
    "Photosynthesis happens in the chloroplasts of plant cells.",
    "refund",
    "The quarterly pipeline for EMEA slipped by two weeks; flag deals that moved more than 14 days and draft a follow-up for each owner.",
    "a " * 600,  # 600 words + 2 special tokens: longer than the 512-token limit, so it is truncated
    "Support hours are 9am to 5pm on weekdays, excluding public holidays.",
    "SELECT * FROM incidents WHERE severity = 'sev-1' ORDER BY opened_at DESC",
]


@pytest.fixture(scope="module")
def model():
    spec = next(s for s in load_model_specs(MODELS_TOML) if s.name == "bge-small-en-v1.5")
    try:
        return load_text_model(spec, Settings.from_env({"ONNX_INTRA_OP_THREADS": "2"}))
    except Exception as exc:  # noqa: BLE001 - any failure to fetch the pinned model (offline, Hub down) is a skip, never a failure
        pytest.skip(f"the pinned model could not be loaded ({type(exc).__name__}): {exc}")


def _reference(model, texts):
    """The card's recipe, written the simple way: one text at a time, no batching, no padding, CLS token, L2 normalise."""
    import onnxruntime as ort
    from huggingface_hub import hf_hub_download
    from tokenizers import Tokenizer

    spec = model.spec
    tokenizer = Tokenizer.from_file(hf_hub_download(spec.repo, "tokenizer.json", revision=spec.revision))
    tokenizer.no_padding()
    tokenizer.enable_truncation(max_length=spec.max_length)
    session = ort.InferenceSession(hf_hub_download(spec.repo, spec.file, revision=spec.revision))
    names = [i.name for i in session.get_inputs()]
    out = []
    for text in texts:
        enc = tokenizer.encode(text)
        feed = {"input_ids": np.array([enc.ids], dtype=np.int64), "attention_mask": np.array([enc.attention_mask], dtype=np.int64)}
        if "token_type_ids" in names:
            feed["token_type_ids"] = np.zeros_like(feed["input_ids"])
        hidden = session.run(None, feed)[0][0]
        vec = hidden[0]
        out.append(vec / np.linalg.norm(vec))
    return np.array(out, dtype=np.float32)


def test_the_served_vectors_match_an_independent_implementation_of_the_recipe(model):
    served = model.text.embed(TEXTS).vectors
    reference = _reference(model, TEXTS)

    assert served.shape == reference.shape == (len(TEXTS), 384) and model.dim == 384
    assert np.abs(served - reference).max() < 1e-4


def test_padding_in_a_batch_does_not_change_any_vector(model):
    """The classic serving bug: a short text padded next to a long one must embed the same as on its own."""
    together = model.text.embed(TEXTS).vectors
    alone = np.vstack([model.text.embed([t]).vectors for t in TEXTS])

    assert np.abs(together - alone).max() < 1e-4


def test_the_vectors_are_unit_length_and_related_texts_are_closer_than_unrelated_ones(model):
    v = model.text.embed(TEXTS[:3]).vectors
    np.testing.assert_allclose(np.linalg.norm(v, axis=1), 1.0, rtol=1e-5)
    assert float(v[0] @ v[1]) > float(v[0] @ v[2]) + 0.1  # a password question is nearer a password answer than a botany sentence


def test_the_model_reports_the_revision_it_really_read(model):
    assert model.revision == model.spec.revision


def test_the_long_text_was_truncated_and_counted(model):
    encoded = model.text.embed(TEXTS)
    assert encoded.truncated == 1 and max(encoded.token_counts) == 512
