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


class ClassifierSuite(Suite):
    """A routing classifier on prompts whose outcomes are known: does it send the right prompts to the stronger model?

    Arrays per item: `score` (the head's probability that the prompt needs the stronger model), `label` (1 iff the strong model scored at least
    the margin above the local one, `routing.label`), `q_local` and `q_strong` (the judge's 0-1 score of each model's answer), `words` (the
    prompt's length in words, for the length router it must beat).

    `accuracy`, `macro_f1` and `ece` judge the yes/no decision at the threshold. The routing metrics judge what the decision is FOR: escalate the
    top share `p` of prompts by score and see how much of the gap between the local and the strong model comes back (RouteLLM's performance gap
    recovered, PGR). `apgr` averages that over the fixed grid, `cpt50`/`cpt90` are the smallest share of prompts that must go to the stronger
    model to recover half / 90% of the gap (LOWER is better: the cheaper the better), and `apgr_vs_length` is the head's APGR minus the APGR of
    a router that escalates the longest prompts first, recomputed on every resample, so "beats the length router" has a confidence interval.
    A random router recovers exactly the share it escalates (APGR 0.5), so "beats random" is `apgr` above 0.5 and needs no second curve.
    """

    kind = "classifier"
    metrics = (
        MetricSpec("accuracy"),
        MetricSpec("macro_f1"),
        MetricSpec("ece", higher_is_better=False),
        MetricSpec("apgr"),
        MetricSpec("cpt50", higher_is_better=False),
        MetricSpec("cpt90", higher_is_better=False),
        MetricSpec("apgr_vs_length"),
    )

    def __init__(self, threshold: float = 0.5) -> None:
        self.threshold = threshold

    def compute(self, data: ItemData, idx: np.ndarray) -> dict[str, float]:
        score = np.asarray(data.arrays["score"])[idx]
        label = np.asarray(data.arrays["label"])[idx].astype(int)
        q_local, q_strong = np.asarray(data.arrays["q_local"])[idx], np.asarray(data.arrays["q_strong"])[idx]
        words = np.asarray(data.arrays["words"])[idx]
        predicted = (score >= self.threshold).astype(int)
        nan = float("nan")
        ece, _bins = m.expected_calibration_error(np.maximum(score, 1.0 - score), predicted == label)
        out = {
            "accuracy": m.accuracy(label.tolist(), predicted.tolist()),
            "macro_f1": m.macro_f1(label.tolist(), predicted.tolist()).value,
            "ece": ece,
            "apgr": nan, "cpt50": nan, "cpt90": nan, "apgr_vs_length": nan,
        }
        try:  # a resample on which the strong model happens to be no better has no gap to recover; the percentile skips NaN
            curve = m.pgr_curve(score, q_local, q_strong)
            length_curve = m.pgr_curve(words, q_local, q_strong)
        except ValueError:
            return out
        out["apgr"] = m.apgr(curve)
        out["apgr_vs_length"] = out["apgr"] - m.apgr(length_curve)
        for name, fraction in (("cpt50", 0.5), ("cpt90", 0.9)):
            share = m.cpt(curve, fraction)
            out[name] = nan if share is None else share  # never reaching the fraction is "no value", not "zero"
        return out


INTENT_DATASETS = ("clinc-oos-plus",)  # public sets scored by IntentSuite instead of the routing suite


def fast_macro_f1(label: np.ndarray, predicted: np.ndarray) -> float:
    """`metrics.macro_f1` for many classes, from bin counts. That reference is a pure-Python loop over classes and items, which is exactly right
    for clarity and far too slow for 150 classes under 2,000 bootstrap resamples; a test pins this to it on random data. Same definition: the
    mean F1 over the classes that occur in `label`, F1 = 2tp / (true + predicted)."""
    k = int(max(label.max(), predicted.max())) + 1
    true_n = np.bincount(label, minlength=k)
    pred_n = np.bincount(predicted, minlength=k)
    tp = np.bincount(label[label == predicted], minlength=k)
    denominator = true_n + pred_n
    f1 = np.divide(2.0 * tp, denominator, out=np.zeros(k), where=denominator > 0)
    return float(f1[true_n > 0].mean())


class IntentSuite(Suite):
    """The method-sanity check (specs/014 T072): an embedding model plus a linear head trained on a standard intent set, scored on its test split.

    It says nothing about routing. It answers "does this embedding pipeline plus this way of training a head produce a sensible classifier on a
    task with a known answer?", which can be checked with no stronger model and no labelling. CLINC's out-of-scope examples are reported SEPARATELY
    from the in-scope ones: a request that matches no intent is the case a router meets every day.

    Arrays per item: `pred` (predicted intent), `label` (true intent; -1 for an out-of-scope item), `confidence` (the head's top probability),
    `oos` (1 for an out-of-scope item) and `oos_threshold` (the same value in every row; the confidence below which the head says "none of
    these", chosen on the validation split, never on the test split).
    """

    kind = "classifier"
    metrics = (
        MetricSpec("accuracy_in_scope"),
        MetricSpec("macro_f1_in_scope"),
        MetricSpec("ece_in_scope", higher_is_better=False),
        MetricSpec("oos_recall@thr"),
        MetricSpec("in_scope_reject@thr", higher_is_better=False),
    )

    def compute(self, data: ItemData, idx: np.ndarray) -> dict[str, float]:
        oos = np.asarray(data.arrays["oos"])[idx].astype(bool)
        inside = ~oos
        nan = float("nan")
        if not inside.any():
            return dict.fromkeys(self.names(), nan)
        pred = np.asarray(data.arrays["pred"])[idx].astype(int)[inside]
        label = np.asarray(data.arrays["label"])[idx].astype(int)[inside]
        confidence = np.asarray(data.arrays["confidence"])[idx]
        threshold = float(np.asarray(data.arrays["oos_threshold"])[0])
        ece, _bins = m.expected_calibration_error(confidence[inside], pred == label)
        return {
            "accuracy_in_scope": float((pred == label).mean()),
            "macro_f1_in_scope": fast_macro_f1(label, pred),
            "ece_in_scope": ece,
            "oos_recall@thr": float((confidence[oos] < threshold).mean()) if oos.any() else nan,
            "in_scope_reject@thr": float((confidence[inside] < threshold).mean()),
        }


def suite_for(kind: str, *, screen_threshold: float = 0.5, dataset: str | None = None) -> Suite:
    if kind == "embedding":
        return EMBEDDING_SUITE
    if kind == "reranker":
        return RERANK_SUITE
    if kind == "screen":
        return ScreenSuite(screen_threshold)
    if kind == "classifier":
        return IntentSuite() if dataset in INTENT_DATASETS else ClassifierSuite()
    raise ValueError(f"unknown kind {kind!r}")
