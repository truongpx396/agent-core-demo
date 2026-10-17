"""The shared tokenise -> run -> pool path (docker/ml-service/ml_service/onnx_text.py).

Every expected vector below is worked out by hand from `FakeSession`'s hidden state: a token with id `i` has the vector
[i, 2i, 3i, 4i]. "hello world" is ids [2, 3]; "foo" is [4] padded with the pad id.
"""
import numpy as np
import pytest

from ml_service.onnx_text import OnnxText, prepare_tokenizer, resolve_pad
from tests.ml_service.conftest import FakeSession, make_tokenizer


def _model(pooling="cls", normalize=False, session=None, max_length=8, pad_token="[PAD]", output=None):
    session = session or FakeSession()
    return OnnxText(session, make_tokenizer(), pooling=pooling, normalize=normalize, max_length=max_length, pad_token=pad_token, output=output), session


# ---- pad token: never guessed -----------------------------------------------------------------------------------------------
def test_a_declared_pad_token_is_looked_up_in_the_vocabulary():
    assert resolve_pad(make_tokenizer(), "[PAD]") == (0, "[PAD]")


def test_a_pad_token_missing_from_the_vocabulary_is_refused():
    with pytest.raises(ValueError, match="not in the tokenizer's vocabulary"):
        resolve_pad(make_tokenizer(), "<nope>")


def test_with_no_pad_token_declared_and_none_configured_it_refuses_instead_of_guessing_zero():
    with pytest.raises(ValueError, match="refusing to guess a pad id"):
        resolve_pad(make_tokenizer(), None)


def test_a_tokenizer_that_carries_its_own_padding_is_used_when_none_is_declared():
    tok = make_tokenizer()
    tok.enable_padding(pad_id=0, pad_token="[PAD]")
    assert resolve_pad(tok, None) == (0, "[PAD]")


def test_the_pad_id_is_the_vocabulary_id_even_when_it_is_not_zero():
    tok = make_tokenizer({"[UNK]": 0, "hello": 1, "world": 2, "<pad>": 7})
    model = OnnxText(FakeSession(), tok, pooling="cls", normalize=False, max_length=8, pad_token="<pad>")

    model.embed(["hello world", "hello"])

    assert model._session.calls[0]["input_ids"].tolist() == [[1, 2], [1, 7]]  # noqa: SLF001 - the padded feed is the assertion


# ---- the MiniLM trap: a tokenizer.json that bakes in fixed-length padding ---------------------------------------------------
def test_explicit_padding_replaces_a_fixed_length_one_shipped_in_the_tokenizer_file():
    tok = make_tokenizer()
    tok.enable_padding(pad_id=0, pad_token="[PAD]", length=10)  # what all-MiniLM-L6-v2's tokenizer.json does (to 128)
    prepare_tokenizer(tok, max_length=8, pad_token="[PAD]")
    session = FakeSession()
    OnnxText(session, tok, pooling="cls", normalize=False, max_length=8, pad_token="[PAD]").embed(["hello", "hello world"])

    assert session.calls[0]["input_ids"].shape == (2, 2)  # padded to the longest item, not to 10


# ---- pooling ----------------------------------------------------------------------------------------------------------------
def test_cls_pooling_takes_the_first_token():
    model, _ = _model("cls")
    got = model.embed(["hello world", "foo"]).vectors
    np.testing.assert_allclose(got, [[2, 4, 6, 8], [4, 8, 12, 16]])


def test_mean_pooling_averages_real_tokens_only_never_the_padding():
    model, _ = _model("mean")
    got = model.embed(["hello world", "foo"]).vectors
    # "hello world": ids 2 and 3 -> mean 2.5; "foo": id 4 and one pad (id 0, masked out) -> 4, not 2
    np.testing.assert_allclose(got, [[2.5, 5, 7.5, 10], [4, 8, 12, 16]])


def test_last_pooling_takes_the_last_real_token_not_the_padding():
    model, _ = _model("last")
    got = model.embed(["hello world", "foo"]).vectors
    np.testing.assert_allclose(got, [[3, 6, 9, 12], [4, 8, 12, 16]])


def test_normalisation_makes_unit_vectors_and_output_is_float32():
    model, _ = _model("cls", normalize=True)
    encoded = model.embed(["hello world", "foo"])

    np.testing.assert_allclose(np.linalg.norm(encoded.vectors, axis=1), [1.0, 1.0], rtol=1e-6)
    assert encoded.vectors.dtype == np.float32


def test_a_named_output_is_used_and_pooling_none_returns_it_unchanged():
    session = FakeSession(outputs=("last_hidden_state", "sentence_embedding"))
    model = OnnxText(session, make_tokenizer(), pooling="none", normalize=False, max_length=8, pad_token="[PAD]", output="sentence_embedding")
    # The fake returns the same hidden state for every output; pooling "none" must hand back (batch, seq, dim) untouched.
    assert model.embed(["hello world"]).vectors.shape == (1, 2, 4)


def test_an_output_the_graph_does_not_have_is_refused_at_construction():
    with pytest.raises(ValueError, match="no output named"):
        OnnxText(FakeSession(), make_tokenizer(), pooling="none", normalize=False, max_length=8, pad_token="[PAD]", output="nope")


def test_an_unknown_pooling_is_refused():
    with pytest.raises(ValueError, match="unknown pooling"):
        OnnxText(FakeSession(), make_tokenizer(), pooling="max", normalize=False, max_length=8, pad_token="[PAD]")  # type: ignore[arg-type]


# ---- graph inputs -----------------------------------------------------------------------------------------------------------
def test_token_type_ids_are_fed_only_when_the_graph_names_them():
    with_types = FakeSession(inputs=("input_ids", "attention_mask", "token_type_ids"))
    without = FakeSession(inputs=("input_ids", "attention_mask"))
    OnnxText(with_types, make_tokenizer(), pooling="cls", normalize=False, max_length=8, pad_token="[PAD]").embed(["hello"])
    OnnxText(without, make_tokenizer(), pooling="cls", normalize=False, max_length=8, pad_token="[PAD]").embed(["hello"])

    assert set(with_types.calls[0]) == {"input_ids", "attention_mask", "token_type_ids"}
    assert not with_types.calls[0]["token_type_ids"].any()
    assert set(without.calls[0]) == {"input_ids", "attention_mask"}


def test_an_input_the_service_cannot_supply_is_refused_not_zero_filled():
    with pytest.raises(ValueError, match="cannot supply"):
        OnnxText(FakeSession(inputs=("input_ids", "attention_mask", "position_ids")), make_tokenizer(), pooling="cls", normalize=False, max_length=8, pad_token="[PAD]")


# ---- truncation and counting ------------------------------------------------------------------------------------------------
def test_truncation_applies_at_max_length_and_is_counted_per_item():
    model, _ = _model("cls", max_length=2)
    encoded = model.embed(["hello world foo bar", "hello"])

    assert encoded.token_counts == [2, 1]  # tokens actually fed after truncation
    assert encoded.truncated == 1  # only the long item was cut


def test_the_token_count_excludes_padding():
    model, _ = _model("cls")
    assert model.embed(["hello world", "foo"]).token_counts == [2, 1]


def test_an_empty_batch_returns_empty_without_calling_the_model():
    model, session = _model("cls")
    assert model.embed([]).vectors.shape == (0, 0)
    assert model.logits([]).shape == (0, 0)
    assert session.calls == []


def test_logits_returns_the_raw_first_output():
    session = FakeSession(logits_fn=lambda ids: np.array([[0.0, 1.0]] * len(ids), dtype=np.float32))
    model, _ = _model("none", session=session)
    np.testing.assert_allclose(model.logits(["hello", "foo"]), [[0.0, 1.0], [0.0, 1.0]])


def test_an_item_that_exactly_fills_max_length_is_not_counted_as_truncated():
    model, _ = _model("cls", max_length=2)
    encoded = model.embed(["hello world", "hello world foo"])  # 2 tokens: fits exactly; 3 tokens: cut

    assert encoded.token_counts == [2, 2]
    assert encoded.truncated == 1
