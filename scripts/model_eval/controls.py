"""Controls: the check that the harness itself is right (specs/014 FR-008, SC-003).

A comparison tool that cannot tell a model from itself, or a good model from a useless one, is worse than none, because it produces
confident numbers. So every report carries controls computed with the SAME comparison code:
  * identity - the baseline's own per-item data against itself must be `no_difference`;
  * degenerate - a screen/model that does nothing useful must be `worse` on the metrics that mean "it works".
Two layers. The DERIVED controls here are cheap and run in every report (they test the metrics, bootstrap and verdict plumbing on the
real dataset's size). The CONTROL CANDIDATES (random vectors, always-benign ...) are run through the real runner by name
(`--candidate random-vectors`) and prove the runner too; tests/integration/test_model_eval_controls_real_data.py runs them on the real
data. A report whose controls fail is `status: invalid`, exit code 5, and must not be used.
"""
from dataclasses import dataclass

import numpy as np

from scripts.model_eval.stats import SEED, Delta, compare
from scripts.model_eval.suites import ItemData, Suite

# Control candidates by kind, and what each must come out as against the baseline.
CONTROL_CANDIDATES: dict[str, dict[str, str]] = {
    "embedding": {"baseline-as-candidate": "no_difference", "random-vectors": "worse"},
    "reranker": {"baseline-as-candidate": "no_difference", "random-scores": "worse"},
    "screen": {"baseline-as-candidate": "no_difference", "always-benign": "worse", "random-scores": "worse"},
    "classifier": {"baseline-as-candidate": "no_difference", "constant": "worse", "random-router": "worse"},
}

# `no_difference` means "not distinguishable from the baseline": a real model run twice on a non-deterministic backend can land
# inside the noise, which is `inconclusive`, and that is a pass. Better or worse is the failure.
ALLOWED = {"no_difference": {"no_difference", "inconclusive"}, "worse": {"worse"}}

# The metrics each degenerate screen is expected to be WORSE on. They differ, and the difference is the lesson of the first real run: the
# first version judged both on the detection side (AUC, TPR at 1% FPR, recall at the threshold), and `make model-eval-verify` failed on the
# recorded baseline because a screen that scores at random flags about half of everything at the 0.5 threshold, so its RECALL (about 0.5)
# is as good as a real screen's (0.47 on the committed baseline). Recall at a threshold says nothing about whether a random scorer is
# useless; its ranking (AUC) and its false alarms (FPR) do. Conversely an always-benign screen has a PERFECT false-positive rate, so its FPR
# metrics are legitimately better and say nothing about whether the harness works; it is judged on what it cannot do (detect). TPR at 1% FPR
# is judged for neither: with a few dozen benign items the cap allows ZERO false positives, so a random scorer can reach a non-zero TPR by
# luck and the verdict depends on the seed.
_JUDGED_ON: dict[tuple[str, str], tuple[str, ...]] = {
    ("screen", "random-scores"): ("roc_auc", "ml_fpr_domain@thr", "app_fpr_domain@thr"),
    ("screen", "always-benign"): ("roc_auc", "tpr@1%fpr", "ml_recall@thr", "app_recall@thr"),
}


@dataclass(frozen=True)
class ControlResult:
    name: str
    expect: str
    observed: str
    ok: bool

    def as_dict(self) -> dict:
        return {"name": self.name, "expect": self.expect, "observed": self.observed, "ok": self.ok}


def judge(name: str, expect: str, deltas: dict[str, Delta], only: tuple[str, ...] | None = None) -> ControlResult:
    """Did a control come out as expected on the metrics it is judged on? `observed` is the first verdict outside what was allowed,
    else the verdict seen."""
    chosen = {k: v for k, v in deltas.items() if only is None or k in only}
    verdicts = [d.verdict for d in chosen.values()]
    deviating = next((v for v in verdicts if v not in ALLOWED[expect]), None)
    observed = deviating or (verdicts[0] if verdicts else "inconclusive")
    return ControlResult(name, expect, observed, deviating is None and bool(verdicts))


def _copy(data: ItemData) -> ItemData:
    return ItemData(list(data.ids), {k: np.array(v, copy=True) for k, v in data.arrays.items()})


def derived_controls(kind: str, suite: Suite, baseline: ItemData, *, seed: int = SEED) -> list[ControlResult]:
    results = [judge("identity", "no_difference", compare(suite, _copy(baseline), baseline))]
    rng = np.random.default_rng(seed)
    n = len(baseline)
    if kind == "screen":
        broken = _copy(baseline)
        broken.arrays["score"] = rng.random(n)  # a screen that scores at random
        results.append(judge("random-scores", "worse", compare(suite, broken, baseline), _JUDGED_ON[("screen", "random-scores")]))
        benign = _copy(baseline)
        benign.arrays["score"] = np.zeros(n)  # a screen that never flags anything
        benign.arrays["pattern_hit"] = np.zeros(n)
        results.append(judge("always-benign", "worse", compare(suite, benign, baseline), _JUDGED_ON[("screen", "always-benign")]))
    else:
        broken = _copy(baseline)
        broken.arrays = {k: np.zeros(n) for k in baseline.arrays}  # a model that retrieves/ranks nothing relevant
        results.append(judge("retrieves-nothing", "worse", compare(suite, broken, baseline)))
    return results


def judge_candidate_control(name: str, kind: str, deltas: dict[str, Delta]) -> ControlResult | None:
    """If the candidate being run IS a control (by name), check it against the expectation registered for it."""
    expect = CONTROL_CANDIDATES.get(kind, {}).get(name)
    if expect is None:
        return None
    return judge(name, expect, deltas, _JUDGED_ON.get((kind, name)) if expect == "worse" else None)
