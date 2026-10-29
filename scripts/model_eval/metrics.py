"""The evaluation metrics, each a few lines of numpy and pinned by a hand-computed example in
tests/scripts/model_eval. In-repo because `mteb` drags in torch (which the ML service images avoid at 2 GB+)
and runs models through its own wrappers, not the app's path, and `pytrec_eval` is a C extension for about
thirty lines of arithmetic. The risk of writing them here is a WRONG implementation, which the hand-computed
expectations and the control candidates (controls.py) catch.

Conventions: retrieval metrics take a ranked list of item ids and a map of relevant id -> integer gain (>= 1),
and return None for a query with no relevant item (the aggregate is over scorable queries). NDCG uses LINEAR
gains, as `trec_eval`/`pytrec_eval`, BEIR and MTEB do. "Positive" means attack / needs-stronger-model (label
1); a higher score means more positive.
"""
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import numpy as np

Floats = Sequence[float] | np.ndarray  # a plain list or a numpy array: callers have both
Ints = Sequence[int] | np.ndarray
Bools = Sequence[bool] | np.ndarray

# ---------------------------------------------------------------------------------------------------------------------------------
# Retrieval and reranking
# ---------------------------------------------------------------------------------------------------------------------------------


def recall_at_k(ranked: Sequence[str], relevant: Mapping[str, int], k: int) -> float | None:
    """Fraction of the query's relevant items found in the top k."""
    if not relevant:
        return None
    hits = sum(1 for item in ranked[:k] if item in relevant)
    return hits / len(relevant)


def mrr_at_k(ranked: Sequence[str], relevant: Mapping[str, int], k: int) -> float | None:
    """Reciprocal rank of the first relevant item in the top k, 0 if none is there."""
    if not relevant:
        return None
    for rank, item in enumerate(ranked[:k], start=1):
        if item in relevant:
            return 1.0 / rank
    return 0.0


def ndcg_at_k(ranked: Sequence[str], relevant: Mapping[str, int], k: int) -> float | None:
    """DCG of the top k over the ideal DCG; gain / log2(rank + 1), linear gains. None when nothing is relevant."""
    gains_sorted = sorted((g for g in relevant.values() if g > 0), reverse=True)
    if not gains_sorted:
        return None
    dcg = sum(relevant.get(item, 0) / math.log2(rank + 1) for rank, item in enumerate(ranked[:k], start=1))
    idcg = sum(g / math.log2(rank + 1) for rank, g in enumerate(gains_sorted[:k], start=1))
    return dcg / idcg


def mean_defined(values: Sequence[float | None]) -> float:
    """Mean over the queries that could be scored (None = no relevant item). Raises if none could."""
    defined = [v for v in values if v is not None]
    if not defined:
        raise ValueError("no query had a relevant item; the evaluation set is unusable")
    return float(np.mean(defined))


# ---------------------------------------------------------------------------------------------------------------------------------
# Screening (binary detection)
# ---------------------------------------------------------------------------------------------------------------------------------


def _split_by_label(scores: Floats, labels: Ints) -> tuple[np.ndarray, np.ndarray]:
    s, y = np.asarray(scores, dtype=float), np.asarray(labels, dtype=int)
    if s.shape != y.shape:
        raise ValueError("scores and labels must have the same length")
    positives, negatives = s[y == 1], s[y == 0]
    if len(positives) == 0 or len(negatives) == 0:
        raise ValueError("need at least one positive and one negative example")
    return positives, negatives


def roc_auc(scores: Floats, labels: Ints) -> float:
    """Area under the ROC curve: the probability a random positive outscores a random negative, a tie counting half (rank-sum with
    tie correction)."""
    positives, negatives = _split_by_label(scores, labels)
    # For each positive: negatives strictly below it, plus half the negatives equal to it.
    neg_sorted = np.sort(negatives)
    below = np.searchsorted(neg_sorted, positives, side="left")
    below_or_equal = np.searchsorted(neg_sorted, positives, side="right")
    wins = below + 0.5 * (below_or_equal - below)
    return float(wins.sum() / (len(positives) * len(negatives)))


def tpr_at_fpr(scores: Floats, labels: Ints, max_fpr: float = 0.01) -> float:
    """The highest detection rate over thresholds whose false-positive rate does NOT exceed `max_fpr`. Conservative: no interpolation
    between thresholds, and "flag nothing" (rate 0) is always available, so a screen that cannot reach the cap scores 0."""
    positives, negatives = _split_by_label(scores, labels)
    best = 0.0
    for threshold in np.unique(np.concatenate([positives, negatives])):
        fpr = float((negatives >= threshold).mean())
        if fpr <= max_fpr:
            best = max(best, float((positives >= threshold).mean()))
    return best


def rate_at_threshold(scores: Floats, threshold: float) -> float:
    """Share of `scores` at or above `threshold` (the false-positive rate when called on benign items, the detection rate on attacks)."""
    s = np.asarray(scores, dtype=float)
    if len(s) == 0:
        raise ValueError("no scores")
    return float((s >= threshold).mean())


def precision_at_threshold(scores: Floats, labels: Ints, threshold: float) -> float | None:
    """Of the items flagged at `threshold`, the share that really are positive; None if nothing was flagged."""
    s, y = np.asarray(scores, dtype=float), np.asarray(labels, dtype=int)
    flagged = s >= threshold
    if not flagged.any():
        return None
    return float((y[flagged] == 1).mean())


# ---------------------------------------------------------------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------------------------------------------------------------


def accuracy(y_true: Sequence[object], y_pred: Sequence[object]) -> float:
    if len(y_true) != len(y_pred) or len(y_true) == 0:
        raise ValueError("y_true and y_pred must be non-empty and the same length")
    return float(np.mean([t == p for t, p in zip(y_true, y_pred, strict=True)]))


@dataclass(frozen=True)
class MacroF1:
    value: float
    classes_scored: int  # classes with at least one true example
    classes_only_predicted: int  # predicted somewhere but never true: they have no support, so they are excluded AND counted


def macro_f1(y_true: Sequence[object], y_pred: Sequence[object]) -> MacroF1:
    """Unweighted mean of per-class F1 over the classes that occur in `y_true`. A class that is only ever predicted has no support
    (its recall is undefined) and is excluded from the mean, but counted so the exclusion is visible."""
    if len(y_true) != len(y_pred) or len(y_true) == 0:
        raise ValueError("y_true and y_pred must be non-empty and the same length")
    true_classes = set(y_true)
    f1s = []
    for cls in true_classes:
        tp = sum(1 for t, p in zip(y_true, y_pred, strict=True) if t == cls and p == cls)
        fp = sum(1 for t, p in zip(y_true, y_pred, strict=True) if t != cls and p == cls)
        fn = sum(1 for t, p in zip(y_true, y_pred, strict=True) if t == cls and p != cls)
        denom = 2 * tp + fp + fn
        f1s.append(2 * tp / denom if denom else 0.0)
    return MacroF1(float(np.mean(f1s)), len(true_classes), len(set(y_pred) - true_classes))


def expected_calibration_error(confidences: Floats, correct: Bools, n_bins: int = 15) -> tuple[float, int]:
    """Weighted mean gap between confidence and accuracy over `n_bins` equal-width bins on [0, 1]. Returns (ece, non-empty bins)."""
    conf, ok = np.asarray(confidences, dtype=float), np.asarray(correct, dtype=float)
    if conf.shape != ok.shape or len(conf) == 0:
        raise ValueError("confidences and correct must be non-empty and the same length")
    edges = np.linspace(0.0, 1.0, n_bins + 1)
    # digitize with the last edge inclusive, so confidence 1.0 lands in the top bin rather than outside
    bin_of = np.clip(np.digitize(conf, edges[1:-1], right=False), 0, n_bins - 1)
    ece, used = 0.0, 0
    for b in range(n_bins):
        mask = bin_of == b
        if mask.any():
            used += 1
            ece += mask.mean() * abs(ok[mask].mean() - conf[mask].mean())
    return float(ece), used


# ---------------------------------------------------------------------------------------------------------------------------------
# Routing: the cost-quality curve (RouteLLM's PGR / CPT / APGR)
# ---------------------------------------------------------------------------------------------------------------------------------

GRID = tuple(round(0.05 * i, 2) for i in range(21))  # 0.0, 0.05, ... 1.0 : the fixed grid APGR averages over


def pgr_curve(
    scores: Floats, q_local: Floats, q_strong: Floats, grid: Floats = GRID
) -> list[float]:
    """Performance gap recovered. Escalate the top-`p` share of prompts by `scores` (ties broken by input order); quality is then the
    mean of the strong model's outcome on the escalated prompts and the local model's on the rest.
    PGR(p) = (Q(p) - Q_local) / (Q_strong - Q_local), so 0 means "no better than never escalating" and 1 "as good as always escalating".
    """
    s, ql, qs = np.asarray(scores, dtype=float), np.asarray(q_local, dtype=float), np.asarray(q_strong, dtype=float)
    if not (s.shape == ql.shape == qs.shape) or len(s) == 0:
        raise ValueError("scores, q_local and q_strong must be non-empty and the same length")
    gap = float(qs.mean() - ql.mean())
    if gap == 0.0:
        raise ValueError("the strong model is no better than the local one on this set: the performance gap is undefined")
    order = np.argsort(-s, kind="stable")
    out = []
    for p in grid:
        n = int(round(p * len(s)))
        chosen = np.zeros(len(s), dtype=bool)
        chosen[order[:n]] = True
        quality = float(np.where(chosen, qs, ql).mean())
        out.append((quality - float(ql.mean())) / gap)
    return out


def cpt(pgr: Floats, x: float, grid: Floats = GRID) -> float | None:
    """Call-performance threshold: the smallest escalated share that recovers at least a fraction `x` of the gap; None if it never does."""
    for p, value in zip(grid, pgr, strict=True):
        if value >= x:
            return p
    return None


def apgr(pgr: Floats) -> float:
    """Average performance gap recovered over the fixed grid."""
    return float(np.mean(pgr))


def random_router_pgr(grid: Floats = GRID) -> list[float]:
    """Expectation for a router that picks at random: escalating a share p recovers share p of the gap."""
    return list(grid)


def sampled_random_router_pgr(
    q_local: Floats, q_strong: Floats, seed: int = 20261017, trials: int = 200, grid: Floats = GRID
) -> list[float]:
    """The same, measured on this set by averaging `trials` random orderings (so a lumpy set shows its real random baseline)."""
    rng = np.random.default_rng(seed)
    n = len(q_local)
    curves = [pgr_curve(rng.random(n), q_local, q_strong, grid) for _ in range(trials)]
    return [float(v) for v in np.mean(curves, axis=0)]


def length_router_scores(texts: Sequence[str]) -> list[float]:
    """Longer prompt -> higher score: the cheapest signal a trained router has to beat. Length is whitespace-separated words."""
    return [float(len(t.split())) for t in texts]
