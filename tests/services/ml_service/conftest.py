"""Shared fakes for the ML service's hermetic tests: a real `tokenizers.Tokenizer` built from a tiny vocabulary (no download) and a fake
ONNX session. Nothing here loads a model or opens a socket."""
from types import SimpleNamespace

import numpy as np
import pytest
from tokenizers import Tokenizer, models, pre_tokenizers

pytest.importorskip("prometheus_client", reason="ml_service.metrics needs prometheus-client (requirements-dev.txt / the CI test job)")

VOCAB = {"[PAD]": 0, "[UNK]": 1, "hello": 2, "world": 3, "foo": 4, "bar": 5, "baz": 6}


def make_tokenizer(vocab: dict[str, int] | None = None) -> Tokenizer:
    tok = Tokenizer(models.WordLevel(vocab or VOCAB, unk_token="[UNK]"))
    tok.pre_tokenizer = pre_tokenizers.Whitespace()
    return tok


class FakeSession:
    """Looks like an onnxruntime session. Its hidden state for a token with id `i` is `[i, 2i, 3i, 4i]`, so every pooling mode has a
    value that can be worked out by hand; `logits_fn`, if given, replaces the output (for the guard)."""

    def __init__(self, inputs=("input_ids", "attention_mask"), outputs=("last_hidden_state",), logits_fn=None):
        self.input_names = list(inputs)
        self.output_names = list(outputs)
        self.logits_fn = logits_fn
        self.calls: list[dict[str, np.ndarray]] = []

    def get_inputs(self):
        return [SimpleNamespace(name=n) for n in self.input_names]

    def get_outputs(self):
        return [SimpleNamespace(name=n) for n in self.output_names]

    def run(self, _output_names, feed):
        self.calls.append(feed)
        ids = feed["input_ids"].astype(np.float32)
        if self.logits_fn is not None:
            return [self.logits_fn(feed["input_ids"])]
        hidden = np.stack([ids, 2 * ids, 3 * ids, 4 * ids], axis=-1)
        return [hidden for _ in self.output_names]
