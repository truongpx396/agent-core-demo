"""Environment-driven settings for the ML service, read once at app creation.

Every name that existed before the package split keeps its name and default, so an existing deployment's environment keeps working.
"""
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

DEFAULT_RERANK_MODEL = "Xenova/ms-marco-MiniLM-L-6-v2"
# gravitee-io's ONNX port of meta-llama/Llama-Prompt-Guard-2-22M. NOT the original meta-llama/ repo: that one is gated under the
# Llama 4 license (terms plus an HF token), which blocks an anonymous Docker build; the port is downloadable unauthenticated.
# 22M, not the 86M sibling: Meta's own guidance is that resource-constrained deployments "will likely prefer the 22M model despite a
# slightly lower attack-prevention rate". Override GUARD_MODEL_REPO for the 86M port if that tradeoff is ever wanted.
DEFAULT_GUARD_REPO = "gravitee-io/Llama-Prompt-Guard-2-22M-onnx"
GUARD_MODEL_FILE = "model.quant.onnx"
GUARD_MAX_SEQ_LEN = 512  # this model's own max_position_embeddings (config.json)
GUARD_LABELS = {0: "BENIGN", 1: "MALICIOUS"}  # this model's own config.json id2label
# Where `head-<model>.npz/json` are read: beside models.toml in the image (/app/heads), overridable so an operator can mount their own.
DEFAULT_HEAD_DIR = Path(__file__).resolve().parent.parent / "heads"
# Where label sets (`<name>.json`, specs/015) are read: beside models.toml in the image (/app/labelsets), overridable so an operator can mount their own.
DEFAULT_LABELSET_DIR = Path(__file__).resolve().parent.parent / "labelsets"


@dataclass(frozen=True)
class Settings:
    rerank_model_name: str
    guard_model_repo: str
    rerank_max_batch_size: int
    guard_max_batch_size: int
    # The adaptive-batching latency tax: the first item to land on an empty queue still waits up to this long for siblings.
    max_batch_wait_ms: float
    # Each batcher dispatches one batch at a time, so one ONNX pass per model is ever running and it may take every core. The
    # batchers of different models do run concurrently with each other (accepted, small models).
    onnx_intra_op_threads: int
    # The load-retry backoff: the first retry waits `model_retry_base_s`, then double, up to `model_retry_max_s`. Every wait has a
    # ceiling (Principle V). The base is a setting only so a test can run the whole machine in milliseconds.
    model_retry_base_s: float
    model_retry_max_s: float
    # Comma list of registry model names to load; empty means "the registry's own `enabled` flags".
    models_enabled: tuple[str, ...]
    # The embedding endpoint's request ceilings (a request over either is REJECTED, never truncated: a caller must be told its input
    # did not fit) and how many texts one model pass takes. Every wait and every size has a ceiling (Principle V).
    embed_max_items: int = 256
    embed_max_chars: int = 8000
    embed_batch_size: int = 32
    # The classifier's bounds. The deadline is the time one model may take before the request moves to the next one (shorter than the app's
    # own timeout so the app's is a backstop); a request over the character limit is rejected, never truncated.
    classify_max_chars: int = 4000
    classify_deadline_ms: float = 250.0
    router_head_dir: Path = DEFAULT_HEAD_DIR
    labelset_dir: Path = DEFAULT_LABELSET_DIR

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "Settings":
        env = os.environ if environ is None else environ
        enabled = tuple(name.strip() for name in env.get("ML_MODELS_ENABLED", "").split(",") if name.strip())
        return cls(
            rerank_model_name=env.get("RERANK_MODEL_NAME", DEFAULT_RERANK_MODEL),
            guard_model_repo=env.get("GUARD_MODEL_REPO", DEFAULT_GUARD_REPO),
            rerank_max_batch_size=int(env.get("RERANK_MAX_BATCH_SIZE", "256")),
            guard_max_batch_size=int(env.get("GUARD_MAX_BATCH_SIZE", "256")),
            max_batch_wait_ms=float(env.get("MAX_BATCH_WAIT_MS", "8")),
            onnx_intra_op_threads=int(env.get("ONNX_INTRA_OP_THREADS", str(os.cpu_count() or 4))),
            model_retry_base_s=float(env.get("ML_MODEL_RETRY_BASE_S", "5")),
            model_retry_max_s=float(env.get("ML_MODEL_RETRY_MAX_S", "300")),
            models_enabled=enabled,
            embed_max_items=int(env.get("ML_EMBED_MAX_ITEMS", "256")),
            embed_max_chars=int(env.get("ML_EMBED_MAX_CHARS", "8000")),
            embed_batch_size=int(env.get("ML_EMBED_BATCH_SIZE", "32")),
            classify_max_chars=int(env.get("ML_CLASSIFY_MAX_CHARS", "4000")),
            classify_deadline_ms=float(env.get("ML_CLASSIFY_DEADLINE_MS", "250")),
            router_head_dir=Path(env["ROUTER_HEAD_DIR"]) if env.get("ROUTER_HEAD_DIR") else DEFAULT_HEAD_DIR,
            labelset_dir=Path(env["LABELSET_DIR"]) if env.get("LABELSET_DIR") else DEFAULT_LABELSET_DIR,
        )
