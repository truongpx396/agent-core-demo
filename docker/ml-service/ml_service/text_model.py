"""A declared text model, loaded: the ONNX session, its tokenizer and what the endpoints need to serve it (specs/014 FR-021/FR-022).

One `TextModel` per entry of models.toml. It is loaded ONCE and serves every role the entry declares: the embedding endpoint
(`embed.py`) now, and the routing classifier later, from the same session, so the classifier's embeddings and the served embeddings are
the same numbers by construction.

The file is fetched at the registry's pinned commit, never a branch. A branch moves under a running service and silently changes what a
model returns; a head trained against one revision is meaningless on another (specs/014 research.md C1), so the revision a model was
loaded at is checked, not trusted, and reported by `/health/models`.
"""
from dataclasses import dataclass
from pathlib import Path

from ml_service.onnx_text import OnnxText
from ml_service.registry import ModelSpec
from ml_service.settings import Settings


@dataclass
class TextModel:
    spec: ModelSpec
    text: OnnxText
    dim: int
    revision: str  # the commit the weights were really read from

    def info(self) -> dict:
        """What /health/models and /v1/models report about it."""
        return {"revision": self.revision, "dim": self.dim, "licence": self.spec.licence, "file": self.spec.file}


def _revision_of(downloaded: str, expected: str) -> str:
    """The Hub cache stores a file at `.../snapshots/<commit>/...`, so the commit the bytes came from is in the path. Verified, because
    `hf_hub_download(revision=...)` resolving a name to another commit would otherwise go unnoticed."""
    parts = Path(downloaded).parts
    if "snapshots" in parts:
        found = parts[parts.index("snapshots") + 1]
        if found != expected:
            raise RuntimeError(f"asked for revision {expected} but the cache holds {found}")
        return found
    raise RuntimeError(f"cannot tell which revision {downloaded!r} came from; refusing to serve an unverifiable model")


def load_text_model(spec: ModelSpec, settings: Settings) -> TextModel:
    """Blocking: downloads (first run) and loads. All heavy imports are deferred so importing this module stays cheap."""
    import onnxruntime as ort
    from huggingface_hub import hf_hub_download
    from tokenizers import Tokenizer

    weights = hf_hub_download(spec.repo, spec.file, revision=spec.revision)
    tokenizer_file = hf_hub_download(spec.repo, "tokenizer.json", revision=spec.revision)
    revision = _revision_of(weights, spec.revision)
    options = ort.SessionOptions()
    options.intra_op_num_threads = settings.onnx_intra_op_threads
    session = ort.InferenceSession(weights, sess_options=options)
    text = OnnxText(
        session,
        Tokenizer.from_file(tokenizer_file),
        pooling=spec.pooling,  # type: ignore[arg-type]  # validated against POOLINGS when models.toml was parsed
        normalize=spec.normalize,
        max_length=spec.max_length,
        pad_token=spec.pad_token,
        output=spec.output,
    )
    dim = int(text.embed(["dimension probe"]).vectors.shape[1])
    return TextModel(spec, text, dim, revision)
