"""/rerank and /prompt-guard behind the real app, lifespan and registry, with fake models (no download).

The wire contracts are the ones the old single-file service had; app/retrieval/embeddings.py::rerank and app/core/moderation.py read
exactly these shapes, so a change here is a change to the app.
"""
import numpy as np
import pytest
from fastapi.testclient import TestClient

from ml_service.app import create_app
from ml_service.guard import GuardService
from ml_service.onnx_text import OnnxText
from ml_service.rerank import RerankService
from ml_service.settings import Settings
from tests.ml_service.conftest import FakeSession, make_tokenizer


def _settings(**overrides) -> Settings:
    base = dict(rerank_model_name="x", guard_model_repo="x", rerank_max_batch_size=8, guard_max_batch_size=8, max_batch_wait_ms=2,
                onnx_intra_op_threads=1, model_retry_base_s=0.02, model_retry_max_s=0.05, models_enabled=())
    return Settings(**{**base, **overrides})


class FakeCrossEncoder:
    def rerank_pairs(self, pairs):
        return [float(len(text)) - 0.5 for _query, text in pairs]  # longer text -> higher score, deterministic


def _guard_logits(ids: np.ndarray) -> np.ndarray:
    # benign ("hello" -> id 2 first) -> logits favour class 0; anything else favours class 1 (malicious)
    return np.array([[5.0, 0.0] if row[0] == 2 else [0.0, 5.0] for row in ids], dtype=np.float32)


def _rerank_loader():
    return RerankService(FakeCrossEncoder(), max_batch_size=8, max_wait_ms=2)


def _guard_loader():
    text = OnnxText(FakeSession(logits_fn=_guard_logits), make_tokenizer(), pooling="none", normalize=False, max_length=8, pad_token="[PAD]")
    return GuardService(text, max_batch_size=8, max_wait_ms=2)


def _client(**loaders) -> TestClient:
    # specs=[]: no text models declared, so the real models.toml (and a download) never enters a hermetic test
    return TestClient(create_app(_settings(), specs=[], loaders={"rerank": _rerank_loader, "prompt-guard": _guard_loader, **loaders}))


def test_rerank_keeps_the_tei_wire_contract_in_input_order():
    with _client() as client:
        response = client.post("/rerank", json={"query": "q", "texts": ["a", "abcd", "ab"], "raw_scores": True})

    assert response.status_code == 200
    assert response.json() == [
        {"index": 0, "score": 0.5, "text": "a"},
        {"index": 1, "score": 3.5, "text": "abcd"},
        {"index": 2, "score": 1.5, "text": "ab"},
    ]  # input order, raw (unbounded) scores; the caller sorts


def test_rerank_of_nothing_is_an_empty_list():
    with _client() as client:
        assert client.post("/rerank", json={"query": "q", "texts": []}).json() == []


def test_prompt_guard_keeps_its_wire_contract_and_label_mapping():
    with _client() as client:
        response = client.post("/prompt-guard", json={"texts": ["hello", "foo bar"]})

    body = response.json()
    assert response.status_code == 200
    assert [(r["index"], r["label"], r["text"]) for r in body] == [(0, "BENIGN", "hello"), (1, "MALICIOUS", "foo bar")]
    assert body[0]["malicious_score"] == pytest.approx(1 / (1 + np.exp(5)), rel=1e-4)  # softmax([5, 0])[1]
    assert body[1]["malicious_score"] > 0.99


def test_prompt_guard_of_nothing_is_an_empty_list():
    with _client() as client:
        assert client.post("/prompt-guard", json={"texts": []}).json() == []


def test_a_model_that_is_not_ready_answers_503_with_the_error_envelope_and_retry_after():
    def broken():
        raise OSError("model file missing")

    with _client(rerank=broken) as client:
        response = client.post("/rerank", json={"query": "q", "texts": ["a"]})
        guard = client.post("/prompt-guard", json={"texts": ["hello"]})

    assert response.status_code == 503
    assert response.json() == {"error": {"code": "model_unavailable", "message": "model 'rerank' is not ready"}}
    assert int(response.headers["Retry-After"]) >= 1
    assert guard.status_code == 200  # the other endpoint is untouched by the first one's failure
