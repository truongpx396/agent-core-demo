"""A *suite* is the set of metrics for one kind of model, defined as a pure function of per-item data and a choice of items.

Why this shape: the statistics (stats.py) need to re-compute every metric on resampled items, for a candidate AND, paired, for the
baseline on the SAME resample. So the baseline stores its per-item data (not just its headline numbers), and a metric is
`compute(data, idx)` with `idx` the chosen item positions. Mean-type metrics (recall, NDCG) are a mean over `idx`; AUC and the rates
at a threshold are recomputed from the resampled scores and labels.
"""
from dataclasses import dataclass, field

import numpy as np

from scripts.model_eval import metrics as m


@dataclass
class ItemData:
    """Per-item values aligned by position with `ids`: the minimum needed to recompute any metric on any resample."""

    ids: list[str]
    arrays: dict[str, np.ndarray] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if len(set(self.ids)) != len(self.ids):
            raise ValueError("item ids must be unique: alignment between two runs is by id")
        for name, values in self.arrays.items():
            if len(values) != len(self.ids):
                raise ValueError(f"array {name!r} has {len(values)} values for {len(self.ids)} items")

    def __len__(self) -> int:
        return len(self.ids)

    def to_json(self) -> dict:
        return {"ids": self.ids, "arrays": {k: np.asarray(v).tolist() for k, v in self.arrays.items()}}

    @classmethod
    def from_json(cls, raw: dict) -> "ItemData":
        return cls(list(raw["ids"]), {k: np.asarray(v, dtype=float) for k, v in raw["arrays"].items()})

    def select(self, ids: list[str]) -> "ItemData":
        where = {item: i for i, item in enumerate(self.ids)}
        pos = np.array([where[i] for i in ids], dtype=int)
        return ItemData(list(ids), {k: np.asarray(v)[pos] for k, v in self.arrays.items()})

    def aligned_with(self, other: "ItemData") -> tuple["ItemData", "ItemData"]:
        """Both restricted to the items they share, in this one's order. Raises if they share too little to be the same evaluation."""
        theirs = set(other.ids)
        shared = [i for i in self.ids if i in theirs]
        if len(shared) < max(1, int(0.9 * min(len(self.ids), len(other.ids)))):
            raise ValueError(
                f"the two runs share only {len(shared)} items of {len(self.ids)} and {len(other.ids)}: they are not the same evaluation"
            )
        return self.select(shared), other.select(shared)

    def same_values_as(self, other: "ItemData") -> bool:
        return self.ids == other.ids and self.arrays.keys() == other.arrays.keys() and all(
            np.array_equal(self.arrays[k], other.arrays[k]) for k in self.arrays
        )


@dataclass(frozen=True)
class MetricSpec:
    name: str
    higher_is_better: bool = True


class Suite:
    """Base: subclasses list their `metrics` and implement `compute`."""

    kind: str = ""
    metrics: tuple[MetricSpec, ...] = ()

    def compute(self, data: ItemData, idx: np.ndarray) -> dict[str, float]:
        raise NotImplementedError

    def names(self) -> list[str]:
        return [s.name for s in self.metrics]

    def higher_is_better(self, name: str) -> bool:
        return next(s.higher_is_better for s in self.metrics if s.name == name)


class MeanSuite(Suite):
    """Every metric is the mean of a per-item array (retrieval and reranking)."""

    def __init__(self, kind: str, names: tuple[str, ...]) -> None:
        self.kind = kind
        self.metrics = tuple(MetricSpec(n) for n in names)

    def compute(self, data: ItemData, idx: np.ndarray) -> dict[str, float]:
        return {s.name: float(np.asarray(data.arrays[s.name])[idx].mean()) for s in self.metrics}


# What the app asks of each model, so the numbers mean something to this deployment:
#   recall@20 - the candidates the app prefetches (HYBRID_PREFETCH_LIMIT) before it reranks; recall@5 - the results it finally returns
#   (RERANK_TOP_K). NDCG@10 and MRR@10 are the field's standard figures (BEIR/MTEB lead with NDCG@10).
EMBEDDING_SUITE = MeanSuite("embedding", ("recall@5", "recall@20", "mrr@10", "ndcg@10"))
RERANK_SUITE = MeanSuite("reranker", ("ndcg@10", "mrr@10"))


class ScreenSuite(Suite):
    """Prompt-injection screening. Arrays per item: `score` (malicious score), `label` (1 attack, 0 benign), `domain` (1 for the
    repository's own domain-style prompts), `pattern_hit` (1 if the app's regex layer alone would block it).

    The domain-style benign prompts are where the FALSE-POSITIVE rate matters to this app (they include benign prompts that talk about
    instructions and policies); vendor cards report detection at a fixed FPR on their own private data and omit exactly that.
    `app_*` metrics are what the app really decides: blocked if the pattern layer hits OR the ML score reaches its threshold.
    """

    kind = "screen"
    metrics = (
        MetricSpec("roc_auc"),
        MetricSpec("tpr@1%fpr"),
        MetricSpec("ml_recall@thr"),
        MetricSpec("ml_fpr_domain@thr", higher_is_better=False),
        MetricSpec("app_recall@thr"),
        MetricSpec("app_fpr_domain@thr", higher_is_better=False),
    )

    def __init__(self, threshold: float) -> None:
        self.threshold = threshold

    def compute(self, data: ItemData, idx: np.ndarray) -> dict[str, float]:
        score = np.asarray(data.arrays["score"])[idx]
        label = np.asarray(data.arrays["label"])[idx].astype(int)
        domain = np.asarray(data.arrays["domain"])[idx].astype(bool)
        pattern = np.asarray(data.arrays["pattern_hit"])[idx].astype(bool)
        attack, benign_domain = label == 1, (label == 0) & domain
        ml_flag = score >= self.threshold
        app_flag = ml_flag | pattern
        nan = float("nan")

        def rate(flag: np.ndarray, subset: np.ndarray) -> float:
            return float(flag[subset].mean()) if subset.any() else nan

        try:
            auc, tpr = m.roc_auc(score, label), m.tpr_at_fpr(score, label, 0.01)
        except ValueError:  # a resample that drew only one class cannot be scored; the percentile skips NaN
            auc = tpr = nan
        return {
            "roc_auc": auc,
            "tpr@1%fpr": tpr,
            "ml_recall@thr": rate(ml_flag, attack),
            "ml_fpr_domain@thr": rate(ml_flag, benign_domain),
            "app_recall@thr": rate(app_flag, attack),
            "app_fpr_domain@thr": rate(app_flag, benign_domain),
        }


def suite_for(kind: str, *, screen_threshold: float = 0.5) -> Suite:
    if kind == "embedding":
        return EMBEDDING_SUITE
    if kind == "reranker":
        return RERANK_SUITE
    if kind == "screen":
        return ScreenSuite(screen_threshold)
    raise ValueError(f"unknown kind {kind!r}")
