"""The one tokenise -> run -> pool -> normalise path every text model in this service uses.

The prompt-injection guard, the embedding endpoint and the routing classifier all feed text to an ONNX graph through this module, and
`scripts/model_eval/head.py` imports it to embed training text. That is deliberate: a classifier head must never be trained on one
pipeline and served on another (different pooling, different truncation, a quantized file instead of the fp32 one), because the head
is a plain vector and silently gives wrong answers on vectors from a different pipeline (specs/014 research.md C1).

Rules this module enforces, each from a real hazard:
  * The pad id comes from the tokenizer's own pad token, or from a token the registry names. It is NEVER guessed: the old guard code
    hard-coded `pad_id=0`, which happens to be right for that model and wrong for others.
  * Truncation and padding are set explicitly from the model's declared `max_length`. A model's own tokenizer.json is not trusted
    to do it: all-MiniLM-L6-v2's ships fixed-length-128 padding AND truncation at 128, which would pad every short query to 128
    tokens (slow) and cut long passages (wrong) if left alone. Padding here is to the longest item in the batch.
  * `token_type_ids` is fed only if the graph names it (BERT-style models want it, ModernBERT-style ones do not). Any other graph
    input is refused at construction, never silently zero-filled.
"""
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

import numpy as np
from tokenizers import Tokenizer

Pooling = Literal["cls", "mean", "last", "none"]
POOLINGS: tuple[str, ...] = ("cls", "mean", "last", "none")
_KNOWN_INPUTS = {"input_ids", "attention_mask", "token_type_ids"}


class Session(Protocol):
    """The part of `onnxruntime.InferenceSession` this module uses (so tests can pass a fake)."""

    def get_inputs(self) -> Sequence[Any]: ...
    def get_outputs(self) -> Sequence[Any]: ...
    def run(self, output_names: Any, input_feed: dict[str, np.ndarray]) -> list[np.ndarray]: ...


def resolve_pad(tokenizer: Tokenizer, declared: str | None) -> tuple[int, str]:
    """The (id, token) used to pad. A declared token must exist in the vocabulary; otherwise the tokenizer must carry its own padding
    configuration. Never falls back to a guessed id."""
    if declared is not None:
        pad_id = tokenizer.token_to_id(declared)
        if pad_id is None:
            raise ValueError(f"pad token {declared!r} is not in the tokenizer's vocabulary")
        return int(pad_id), declared
    padding = tokenizer.padding
    if padding and padding.get("pad_token") is not None and padding.get("pad_id") is not None:
        return int(padding["pad_id"]), str(padding["pad_token"])
    raise ValueError("no pad_token declared and the tokenizer defines none; refusing to guess a pad id")


def prepare_tokenizer(tokenizer: Tokenizer, *, max_length: int, pad_token: str | None) -> Tokenizer:
    """Set truncation and batch-longest padding explicitly (see the module docstring). Returns the same tokenizer."""
    if max_length < 1:
        raise ValueError("max_length must be at least 1")
    pad_id, token = resolve_pad(tokenizer, pad_token)
    tokenizer.enable_padding(pad_id=pad_id, pad_token=token)  # no `length`: pad to the longest item in the batch
    tokenizer.enable_truncation(max_length=max_length)
    return tokenizer


@dataclass(frozen=True)
class Encoded:
    vectors: np.ndarray  # (n, dim) float32, L2-normalised when the model says so
    token_counts: list[int]  # tokens actually fed to the model per item, after truncation
    truncated: int  # how many items were cut to max_length


class OnnxText:
    """One ONNX text model plus its tokenizer. Not thread-safe by itself; the service calls it from one batcher thread at a time."""

    def __init__(
        self,
        session: Session,
        tokenizer: Tokenizer,
        *,
        pooling: Pooling,
        normalize: bool,
        max_length: int,
        pad_token: str | None,
        output: str | None = None,
    ) -> None:
        if pooling not in POOLINGS:
            raise ValueError(f"unknown pooling {pooling!r}; expected one of {POOLINGS}")
        self._session = session
        # An untruncated, unpadded copy, taken BEFORE the tokenizer is prepared. `Encoding.overflowing` is how a tokenizer reports a
        # cut, but it is empty for some tokenizers (verified: not populated without a special-token post-processor), so truncation is
        # counted by re-encoding only the items that reached the limit instead of trusting a field that is not always filled in.
        self._probe = Tokenizer.from_str(tokenizer.to_str())
        self._probe.no_truncation()
        self._probe.no_padding()
        self._max_length = max_length
        self._tokenizer = prepare_tokenizer(tokenizer, max_length=max_length, pad_token=pad_token)
        self._pooling: Pooling = pooling
        self._normalize = normalize
        self._input_names = [i.name for i in session.get_inputs()]
        unsupported = [n for n in self._input_names if n not in _KNOWN_INPUTS]
        if unsupported:
            raise ValueError(f"the graph needs inputs this service cannot supply: {unsupported}")
        output_names = [o.name for o in session.get_outputs()]
        if output is not None and output not in output_names:
            raise ValueError(f"the graph has no output named {output!r}; it has {output_names}")
        self._output_index = output_names.index(output) if output is not None else 0

    def _run(self, texts: Sequence[str]) -> tuple[np.ndarray, np.ndarray, list[int], int]:
        encodings = self._tokenizer.encode_batch(list(texts))
        ids = np.array([e.ids for e in encodings], dtype=np.int64)
        mask = np.array([e.attention_mask for e in encodings], dtype=np.int64)
        feed: dict[str, np.ndarray] = {"input_ids": ids, "attention_mask": mask}
        if "token_type_ids" in self._input_names:
            feed["token_type_ids"] = np.zeros_like(ids)
        feed = {name: value for name, value in feed.items() if name in self._input_names}
        output = np.asarray(self._session.run(None, feed)[self._output_index])
        truncated = sum(
            1
            for e, text in zip(encodings, texts, strict=True)
            if len(e.ids) >= self._max_length and len(self._probe.encode(text).ids) > self._max_length
        )
        return output, mask, [int(m.sum()) for m in mask], truncated

    def logits(self, texts: Sequence[str]) -> np.ndarray:
        """Raw first-output values, for a sequence-classification graph (the guard)."""
        if not texts:
            return np.zeros((0, 0), dtype=np.float32)
        output, _mask, _counts, _truncated = self._run(texts)
        return output

    def embed(self, texts: Sequence[str]) -> Encoded:
        """Pooled (and optionally L2-normalised) vectors, float32, in input order."""
        if not texts:
            return Encoded(np.zeros((0, 0), dtype=np.float32), [], 0)
        output, mask, counts, truncated = self._run(texts)
        if self._pooling == "none":
            vectors = output
        elif self._pooling == "cls":
            vectors = output[:, 0]
        elif self._pooling == "mean":
            weights = mask[..., None].astype(output.dtype)
            vectors = (output * weights).sum(axis=1) / np.maximum(weights.sum(axis=1), 1e-9)
        else:  # last non-pad token; padding is on the right
            vectors = output[np.arange(output.shape[0]), mask.sum(axis=1) - 1]
        vectors = np.asarray(vectors, dtype=np.float32)
        if self._normalize:
            norms = np.linalg.norm(vectors, axis=1, keepdims=True)
            vectors = vectors / np.maximum(norms, 1e-12)
        return Encoded(vectors, counts, truncated)
