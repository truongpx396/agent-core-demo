"""Bootstrap intervals and the better / worse / inconclusive verdict (specs/014 FR-005, research.md A4).

A few hundred evaluation items cannot resolve a one-point difference, and a number without its uncertainty invites a confident wrong
decision. So every quality number carries a 95% percentile interval from resampling the ITEMS (the unit of independence: a query's
several relevant passages are not independent), and a comparison uses the PAIRED bootstrap: candidate and baseline are scored on the
same resample, so what is measured is the difference per item, which is far tighter than comparing two independent intervals.
Fixed seed, so the same inputs always give the same interval (SC-002).
"""
import math
from dataclasses import dataclass

import numpy as np

from scripts.model_eval.suites import ItemData, Suite

SEED = 20261017
N_BOOT = 2000
ALPHA = 0.05


def resample_indices(n: int, *, seed: int = SEED, n_boot: int = N_BOOT) -> np.ndarray:
    """(n_boot, n) item positions drawn with replacement."""
    if n < 1:
        raise ValueError("cannot resample an empty evaluation set")
    return np.random.default_rng(seed).integers(0, n, size=(n_boot, n))


def _interval(samples: np.ndarray) -> tuple[float, float]:
    finite = samples[~np.isnan(samples)]
    if len(finite) == 0:
        return float("nan"), float("nan")
    return float(np.percentile(finite, 100 * ALPHA / 2)), float(np.percentile(finite, 100 * (1 - ALPHA / 2)))


@dataclass(frozen=True)
class Measured:
    value: float
    ci95: tuple[float, float]


def measure(suite: Suite, data: ItemData, *, seed: int = SEED, n_boot: int = N_BOOT) -> dict[str, Measured]:
    """Every metric of `suite` on all items, with its bootstrap interval."""
    full = suite.compute(data, np.arange(len(data)))
    boots = [suite.compute(data, idx) for idx in resample_indices(len(data), seed=seed, n_boot=n_boot)]
    return {name: Measured(value, _interval(np.array([b[name] for b in boots]))) for name, value in full.items()}


@dataclass(frozen=True)
class Delta:
    delta: float
    ci95: tuple[float, float]
    verdict: str  # better | worse | inconclusive | no_difference
    needed_n: int | None


def compare(suite: Suite, candidate: ItemData, baseline: ItemData, *, seed: int = SEED, n_boot: int = N_BOOT) -> dict[str, Delta]:
    """Paired comparison of a candidate against a baseline on the items they share."""
    cand, base = candidate.aligned_with(baseline)
    n = len(cand)
    idx_all = np.arange(n)
    point_c, point_b = suite.compute(cand, idx_all), suite.compute(base, idx_all)
    identical = cand.same_values_as(base)
    boots = resample_indices(n, seed=seed, n_boot=n_boot)
    c_boot = [suite.compute(cand, idx) for idx in boots]
    b_boot = [suite.compute(base, idx) for idx in boots]
    out: dict[str, Delta] = {}
    for name in suite.names():
        delta = point_c[name] - point_b[name]
        diffs = np.array([c[name] - b[name] for c, b in zip(c_boot, b_boot, strict=True)])
        lo, hi = _interval(diffs)
        verdict, needed = _verdict(delta, lo, hi, suite.higher_is_better(name), identical, n)
        out[name] = Delta(delta, (lo, hi), verdict, needed)
    return out


def _verdict(delta: float, lo: float, hi: float, higher_is_better: bool, identical: bool, n: int) -> tuple[str, int | None]:
    if identical:
        return "no_difference", None
    if math.isnan(lo) or math.isnan(hi):
        return "inconclusive", None
    if lo > 0 or hi < 0:
        improved = (delta > 0) == higher_is_better
        return ("better" if improved else "worse"), None
    # The interval spans zero. How many items would it take for a difference this size to clear the noise? n * (half-width / |delta|)^2
    # (normal approximation), labelled an estimate: the interval narrows roughly with 1/sqrt(n).
    half_width = (hi - lo) / 2
    needed = None if delta == 0 else math.ceil(n * (half_width / abs(delta)) ** 2)
    return "inconclusive", needed
