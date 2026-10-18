"""`make router-head-train`: fit a routing head from labelled outcomes and export it for the ML service (specs/014 T070, FR-028..030, FR-038).

A head is `sigmoid(w . x + b)` over the L2-normalised embedding `x` of a prompt (docker/ml-service/ml_service/classify.py). This module learns
`(w, b)`: L2-regularised logistic regression on `label = needs_stronger` (the strong model scored at least `margin` above the local one on
that prompt, `routing.label`), numpy only.

SAME PIPELINE AS THE SERVICE, BY CONSTRUCTION. The training vectors come from `ml_service.text_model.load_text_model`, the loader the service
itself runs, so a head can never be trained on one embedding pipeline and served on another. The revision it records is the commit the Hub
cache REALLY holds (`TextModel.revision`), which is what the service compares against before it will serve the head.

DETERMINISTIC. Newton's method from zero with a fixed iteration count has no randomness; the seed (20261017) picks the cross-validation folds
that choose the regularisation strength from a small grid, using the TRAINING prompts only. The weights are written through a zip writer with a
fixed timestamp, because `np.savez` stamps the current time into the archive and equal inputs would otherwise give different bytes.

THE TRAINER CANNOT ACCEPT ITS OWN HEAD. It always writes `"accepted": false`, and `write_head` has no parameter that could change that. A
person sets it after the evaluation harness shows the head beats the random and prompt-length routers on held-out data (FR-038); until
then the service refuses the head and every turn runs locally. It also refuses to overwrite a head that a person has already accepted
unless told to (`--force`), because replacing it would silently switch a live head off.

IT REFUSES TO TRAIN ON WHAT IT IS REPORTED ON: if the training prompts overlap the held-out split (exact or near-duplicate text), nothing is
written (FR-010).
"""
import hashlib
import io
import json
import zipfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from scripts.model_eval import datasets as ds
from scripts.model_eval import metrics
from scripts.model_eval.routing import DEFAULT_MARGIN, Outcome, label

SEED = 20261017
ITERATIONS = 25  # Newton steps; a step that cannot lower the loss ends the fit early (converged), it never runs longer
L2_GRID = (0.01, 0.1, 1.0, 10.0)
DEFAULT_L2 = 1.0  # used when there are too few examples of a class to cross-validate
FOLDS = 4
THRESHOLD = 0.5
MIN_TRAIN = 8  # fewer labelled prompts than this is not a training set, whatever the dimension
HEAD_SCHEMA_VERSION = 1  # tests/scripts/model_eval/test_head.py pins it equal to the service's
_ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)

# texts -> float array [n, dim], produced by the service's own embedding code (see `service_embedder`). Injected so tests need no model.
Embedder = Callable[[Sequence[str]], np.ndarray]
Item = tuple[str, Outcome]


class TrainingRefused(Exception):
    """A reason not to train or not to write, stated so an operator can act on it. Nothing is written when this is raised."""


class ContaminatedTrainingSet(TrainingRefused):
    """The training prompts overlap the split the head is reported on. A verification failure (exit 4), not a usage mistake."""


@dataclass(frozen=True)
class ModelInfo:
    """The embedding model a head is bound to: exactly the fields the service compares before serving it."""

    name: str
    repo: str
    file: str
    revision: str  # the full commit sha the weights were read from
    dim: int


@dataclass(frozen=True)
class TrainedHead:
    w: np.ndarray  # float32 [dim]
    b: float
    model: ModelInfo
    provenance: dict  # everything the JSON records apart from the fixed fields


# ---- the fit -----------------------------------------------------------------------------------------------------------------------
def l2_normalise(x: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(x, axis=1, keepdims=True)
    return x / np.where(norms == 0.0, 1.0, norms)  # the service does the same, so a zero vector stays zero rather than becoming NaN


def sigmoid(z: np.ndarray) -> np.ndarray:
    return 0.5 * (1.0 + np.tanh(z / 2.0))  # the service's formula: no overflow for a large |z|


def _loss(x: np.ndarray, y: np.ndarray, w: np.ndarray, b: float, l2: float) -> float:
    z = x @ w + b
    return float(np.sum(np.logaddexp(0.0, z) - y * z) + 0.5 * l2 * float(w @ w))  # the intercept is not penalised


def fit_logistic(x: np.ndarray, y: np.ndarray, l2: float, iterations: int = ITERATIONS) -> tuple[np.ndarray, float]:
    """Newton's method on the L2-regularised logistic loss, from w = 0, b = 0. A step is halved until it lowers the loss; if none can, the fit
    has converged. Strictly convex in `w` because of the penalty, so it is well defined even when the classes are perfectly separable."""
    n, d = x.shape
    w, b = np.zeros(d), 0.0
    for _ in range(iterations):
        p = sigmoid(x @ w + b)
        s = np.maximum(p * (1.0 - p), 1e-9)
        grad = np.concatenate([x.T @ (p - y) + l2 * w, [np.sum(p - y)]])
        weighted = x * s[:, None]
        hessian = np.zeros((d + 1, d + 1))
        hessian[:d, :d] = x.T @ weighted + l2 * np.eye(d)
        hessian[:d, d] = hessian[d, :d] = weighted.sum(axis=0)
        hessian[d, d] = s.sum()
        step = np.linalg.solve(hessian, grad)
        base, scale = _loss(x, y, w, b, l2), 1.0
        for _halving in range(30):
            candidate_w, candidate_b = w - scale * step[:d], float(b - scale * step[d])
            if _loss(x, y, candidate_w, candidate_b, l2) <= base:
                break
            scale /= 2.0
        else:
            break
        if np.array_equal(candidate_w, w) and candidate_b == b:
            break
        w, b = candidate_w, candidate_b
    return w, b


def softmax(logits: np.ndarray) -> np.ndarray:
    shifted = logits - logits.max(axis=1, keepdims=True)
    exp = np.exp(shifted)
    return exp / exp.sum(axis=1, keepdims=True)


def standardise(train: np.ndarray, *others: np.ndarray) -> list[np.ndarray]:
    """Zero mean and unit variance per dimension, from the TRAINING rows only, applied to all. Linear probes train far better on this than on
    unit vectors (whose coordinates are all tiny), and it is the usual preparation for the method the intent check stands in for."""
    mean, std = train.mean(axis=0), train.std(axis=0)
    std = np.where(std < 1e-8, 1.0, std)
    return [(a - mean) / std for a in (train, *others)]


def fit_softmax(x: np.ndarray, y: np.ndarray, n_classes: int, *, l2: float = 1e-3, iterations: int = 300) -> tuple[np.ndarray, np.ndarray]:
    """Multinomial logistic regression by full-batch gradient descent with Nesterov momentum, numpy only and deterministic (zeros start, no
    shuffling). The step is 1 / L with L, the largest curvature of the loss, bounded from the top singular value of `x` found by power iteration
    from a fixed start, so there is no learning rate to tune and the loss cannot diverge."""
    n, d = x.shape
    vector = np.ones(d) / np.sqrt(d)
    for _ in range(30):
        vector = x.T @ (x @ vector)
        vector /= np.linalg.norm(vector) or 1.0
    top = float(np.linalg.norm(x @ vector)) ** 2 / n  # largest eigenvalue of X'X / n
    step = 1.0 / (0.5 * top + l2)
    onehot = np.zeros((n, n_classes))
    onehot[np.arange(n), y] = 1.0
    weights, bias = np.zeros((d, n_classes)), np.zeros(n_classes)
    ahead_w, ahead_b = weights.copy(), bias.copy()
    for t in range(1, iterations + 1):
        probs = softmax(x @ ahead_w + ahead_b)
        grad_w = x.T @ (probs - onehot) / n + l2 * ahead_w
        grad_b = (probs - onehot).mean(axis=0)
        new_w, new_b = ahead_w - step * grad_w, ahead_b - step * grad_b
        momentum = (t - 1) / (t + 2)
        ahead_w, ahead_b = new_w + momentum * (new_w - weights), new_b + momentum * (new_b - bias)
        weights, bias = new_w, new_b
    return weights, bias


def choose_l2(x: np.ndarray, y: np.ndarray, seed: int = SEED, grid: Sequence[float] = L2_GRID, folds: int = FOLDS) -> float:
    """The penalty from `grid` with the lowest held-out log-loss under seeded, label-stratified k-fold cross-validation on the TRAINING prompts
    only (the held-out split is never looked at). Ties go to the stronger penalty. With fewer than two examples of a class there is nothing to
    cross-validate, so the default is used."""
    rng = np.random.default_rng(seed)
    by_class = [rng.permutation(np.flatnonzero(y == c)) for c in (0, 1)]
    k = min(folds, *(len(idx) for idx in by_class))
    if k < 2:
        return DEFAULT_L2
    fold_of = np.zeros(len(y), dtype=int)
    for idx in by_class:
        fold_of[idx] = np.arange(len(idx)) % k
    best, best_loss = DEFAULT_L2, float("inf")
    for l2 in sorted(grid, reverse=True):
        total = 0.0
        for fold in range(k):
            train, test = fold_of != fold, fold_of == fold
            w, b = fit_logistic(x[train], y[train], l2)
            p = np.clip(sigmoid(x[test] @ w + b), 1e-12, 1.0 - 1e-12)
            total += float(-np.sum(y[test] * np.log(p) + (1.0 - y[test]) * np.log(1.0 - p)))
        if total < best_loss - 1e-12:
            best, best_loss = l2, total
    return best


# ---- held-out figures --------------------------------------------------------------------------------------------------------------
def heldout_report(scores: np.ndarray, y: np.ndarray, q_local: np.ndarray, q_strong: np.ndarray, texts: Sequence[str]) -> dict:
    """Accuracy, macro-F1, calibration and the cost-quality curve of the head on prompts it was not trained on, with the two routers it must
    beat: one that picks at random and one that escalates the longest prompts first."""
    predicted = (scores >= THRESHOLD).astype(int)
    confidence = np.maximum(scores, 1.0 - scores)
    ece, _bins = metrics.expected_calibration_error(confidence, predicted == y)
    out: dict = {
        "items": int(len(y)),
        "accuracy": metrics.accuracy(y.tolist(), predicted.tolist()),
        "macro_f1": metrics.macro_f1(y.tolist(), predicted.tolist()).value,
        "ece": ece,
        "escalation_share": float(predicted.mean()),
    }
    try:
        curve = metrics.pgr_curve(scores, q_local, q_strong)
    except ValueError:  # the strong model is no better than the local one on these prompts: there is no gap to recover
        out.update({"pgr_curve": None, "cpt": {"50": None, "90": None}, "apgr": None, "controls": None,
                    "performance_gap": "undefined: the strong model was no better than the local one on the held-out prompts"})
        return out
    length_curve = metrics.pgr_curve(metrics.length_router_scores(texts), q_local, q_strong)
    random_curve = metrics.random_router_pgr()
    out.update({
        "pgr_curve": [[g, v] for g, v in zip(metrics.GRID, curve, strict=True)],
        "cpt": {"50": metrics.cpt(curve, 0.5), "90": metrics.cpt(curve, 0.9)},
        "apgr": metrics.apgr(curve),
        "controls": {
            "random": {"apgr": metrics.apgr(random_curve), "cpt": {"50": metrics.cpt(random_curve, 0.5), "90": metrics.cpt(random_curve, 0.9)}},
            "length": {"apgr": metrics.apgr(length_curve), "cpt": {"50": metrics.cpt(length_curve, 0.5), "90": metrics.cpt(length_curve, 0.9)}},
        },
    })
    return out


# ---- training ----------------------------------------------------------------------------------------------------------------------
def _labelled(items: Sequence[Item], margin: float) -> tuple[list[str], np.ndarray, np.ndarray, np.ndarray, int]:
    """Only prompts with BOTH scores can be labelled; the rest are dropped and counted, never treated as zero."""
    kept = [(text, outcome) for text, outcome in items if outcome.complete]
    texts = [t for t, _ in kept]
    y = np.array([label(o, margin) for _, o in kept], dtype=float)
    q_local = np.array([o.local for _, o in kept], dtype=float)
    q_strong = np.array([o.strong for _, o in kept], dtype=float)
    return texts, y, q_local, q_strong, len(items) - len(kept)


def train_head(
    *,
    model: ModelInfo,
    embed: Embedder,
    train: Sequence[Item],
    heldout: Sequence[Item],
    dataset: dict,
    outcomes_sha256: str,
    margin: float = DEFAULT_MARGIN,
    seed: int = SEED,
) -> TrainedHead:
    train_texts, y, _ql, _qs, train_dropped = _labelled(train, margin)
    held_texts, y_held, ql_held, qs_held, held_dropped = _labelled(heldout, margin)
    if len(train_texts) < MIN_TRAIN:
        raise TrainingRefused(f"only {len(train_texts)} training prompts have both scores; at least {MIN_TRAIN} are needed ({train_dropped} had a missing score)")
    if not held_texts:
        raise TrainingRefused("no held-out prompt has both scores, so there is nothing to report the head on; run `make router-label` for the test split too")
    if len(set(y.tolist())) < 2:
        raise TrainingRefused(f"every training prompt has the same label ({'needs_stronger' if y[0] else 'local_ok'}) at margin {margin}; there is nothing to learn")
    crossing = ds.overlap(train_texts, held_texts)
    if crossing["contaminated"]:
        raise ContaminatedTrainingSet(
            f"the training prompts overlap the held-out split ({crossing['exact']} exact, {crossing['near_duplicate_pairs']} near-duplicate pairs): "
            "a head reported on text it was fitted on proves nothing. Nothing was written."
        )
    x, x_held = l2_normalise(np.asarray(embed(train_texts), dtype=float)), l2_normalise(np.asarray(embed(held_texts), dtype=float))
    if x.shape[1] != model.dim or x_held.shape[1] != model.dim:
        raise TrainingRefused(f"the embedder produced {x.shape[1]}-d vectors but {model.name} is declared {model.dim}-d")
    l2 = choose_l2(x, y, seed)
    w, b = fit_logistic(x, y, l2)
    weights = w.astype(np.float32)
    # Score the held-out prompts with the weights AS STORED (float32), the way the service will, not with the float64 fit.
    scores = sigmoid(x_held @ weights.astype(np.float64) + float(np.float32(b)))
    report = heldout_report(scores, y_held, ql_held, qs_held, held_texts)
    controls = report.get("controls")
    provenance = {
        "trained_on": {
            **dataset, "outcomes_sha256": outcomes_sha256, "margin": margin, "items": len(train_texts), "dropped_missing_scores": train_dropped,
            "needs_stronger_share": float(y.mean()),
        },
        "heldout": {**{k: v for k, v in report.items() if k != "controls"}, "controls": controls, "dropped_missing_scores": held_dropped},
        "controls_beaten": (
            {"random": report["apgr"] > controls["random"]["apgr"], "length": report["apgr"] > controls["length"]["apgr"]} if controls else
            {"random": None, "length": None}
        ),
        "method": {"algorithm": "L2-regularised logistic regression, Newton's method from zero", "l2": l2, "l2_grid": list(L2_GRID),
                   "l2_chosen_by": f"seeded {FOLDS}-fold stratified cross-validation on the training prompts only", "iterations": ITERATIONS},
        "seed": seed,
    }
    return TrainedHead(weights, float(np.float32(b)), model, provenance)


# ---- writing -----------------------------------------------------------------------------------------------------------------------
def npz_bytes(w: np.ndarray, b: float) -> bytes:
    """An `.npz` holding `w` (float32 [dim]) and `b` (float32 scalar) that is identical for identical inputs: a zip with a fixed timestamp and
    no compression, written entry by entry. `np.load(..., allow_pickle=False)` reads it, which is how the service does."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as archive:
        for name, array in (("w", np.asarray(w, dtype=np.float32)), ("b", np.asarray(b, dtype=np.float32))):
            payload = io.BytesIO()
            np.lib.format.write_array(payload, array, allow_pickle=False)
            info = zipfile.ZipInfo(f"{name}.npy", date_time=_ZIP_EPOCH)
            info.compress_type = zipfile.ZIP_STORED
            archive.writestr(info, payload.getvalue())
    return out.getvalue()


def head_paths(directory: Path, model_name: str) -> tuple[Path, Path]:
    return directory / f"head-{model_name}.json", directory / f"head-{model_name}.npz"


def _accepted_on_disk(json_path: Path) -> bool:
    try:
        return json.loads(json_path.read_text()).get("accepted") is True
    except (OSError, ValueError, AttributeError):
        return False


def write_head(directory: Path, head: TrainedHead, *, trained_at: str, force: bool = False) -> tuple[Path, Path]:
    """Write `head-<model>.npz` and `head-<model>.json`. There is no `accepted` parameter, on purpose: the file always says `false` (FR-038)."""
    json_path, npz_path = head_paths(directory, head.model.name)
    if _accepted_on_disk(json_path) and not force:
        raise TrainingRefused(
            f"{json_path.name} is already ACCEPTED by a person; writing a new head here would switch it off (every new head is unaccepted). "
            "Write to another directory, or pass --force if replacing it is what you mean."
        )
    weights = npz_bytes(head.w, head.b)
    version = f"{trained_at[:10]}.{hashlib.sha256(weights).hexdigest()[:8]}"
    document = {
        "schema_version": HEAD_SCHEMA_VERSION,
        "head_version": version,
        "model": head.model.name,
        "model_repo": head.model.repo,
        "model_revision": head.model.revision,
        "model_file": head.model.file,
        "dim": head.model.dim,
        "labels": {"0": "local_ok", "1": "needs_stronger"},
        **head.provenance,
        "threshold": {
            "value": THRESHOLD,
            "escalation_share": head.provenance["heldout"]["escalation_share"],
            "justification": "the default 0.5, not tuned on any data; the app applies its own ROUTER_THRESHOLD to the score",
        },
        "trained_at": trained_at,
        "accepted": False,
    }
    directory.mkdir(parents=True, exist_ok=True)
    npz_path.write_bytes(weights)
    json_path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
    return json_path, npz_path


# ---- the real embedder -------------------------------------------------------------------------------------------------------------
def service_embedder(model_name: str, models_toml: Path | None = None) -> tuple[Embedder, ModelInfo]:
    """The ML service's own embedding code (`ml_service.text_model.load_text_model`) loaded in this process. Downloads the model on first use."""
    from ml_service.registry import load_model_specs
    from ml_service.settings import Settings
    from ml_service.text_model import load_text_model

    path = models_toml or ds.REPO / "docker" / "ml-service" / "models.toml"
    spec = next((s for s in load_model_specs(path) if s.name == model_name), None)
    if spec is None:
        raise TrainingRefused(f"{model_name!r} is not in {path.name}")
    if "classify" not in spec.roles:
        raise TrainingRefused(f"{model_name!r} does not have the `classify` role in {path.name}; the service would never ask it for a score")
    loaded = load_text_model(spec, Settings.from_env({}))  # the service's own defaults, not whatever the environment of this shell holds

    def embed(texts: Sequence[str]) -> np.ndarray:
        batches = [loaded.text.embed(list(texts[i : i + 32])).vectors for i in range(0, len(texts), 32)]
        return np.vstack(batches)

    return embed, ModelInfo(spec.name, spec.repo, spec.file, loaded.revision, loaded.dim)
