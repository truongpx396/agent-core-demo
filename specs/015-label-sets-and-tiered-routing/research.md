# Research: what was measured, and what it does and does not show

Everything here was run on 2026-10-22 on the maintainer's laptop (Apple M4 Pro, 12 threads) with the models and datasets pinned in the repository.
Nothing below is a claim about routing quality. The only routing outcomes that exist are the 014 ones, and 014 found them unable to separate
a perfect router from a random one.

## R1. How should a label's similarity be computed from its examples? (decision D1)

**Question.** A label has *k* example sentences. A text's similarity to the label can be (a) the cosine to the single nearest example (`max`),
(b) the mean of the cosines to the two nearest (`top2`), or (c) the cosine to the centroid, the renormalised mean of the examples' unit vectors.
Which one should the service use?

**Method.** CLINC150 (`clinc/clinc_oos`, `plus`, pinned in `evals/datasets/public.lock.json`): 150 intents plus out-of-scope sentences. Anchors
are the **first *k* training examples** of each intent (no selection, so nothing is tuned toward the test set). The "none of these" cutoff is
chosen on a validation sample (6 per intent and 150 out-of-scope) as the value that keeps 95% of its in-scope sentences. Everything is
reported on a test sample (10 per intent = 1,500 in-scope, and 300 out-of-scope). bge-small-en-v1.5, fp32 ONNX, no prefix. One run (the sample
seeds are fixed in the script); no confidence intervals, so differences of a point or two are not claims.

| k | method | accuracy (nearest intent) | out-of-scope rejected at the cutoff | in-scope kept |
|---|---|---|---|---|
| 3 | max | 0.749 | 63.7% | 94.7% |
| 3 | top2 | 0.775 | 58.7% | 95.4% |
| 3 | **centroid** | **0.778** | **67.3%** | 95.1% |
| 5 | max | 0.761 | 64.0% | 94.7% |
| 5 | top2 | 0.781 | 67.0% | 94.5% |
| 5 | **centroid** | **0.795** | **69.0%** | 95.0% |
| 10 | max | 0.806 | 71.0% | 94.7% |
| 10 | top2 | 0.834 | 70.3% | 95.3% |
| 10 | **centroid** | **0.863** | **78.0%** | 95.0% |

**Read.** The centroid is the best or tied at every k on both measures, and the gap widens with k (5.7 points over `max` at k=10). What this
does not show: that the centroid is best for a label that is two clusters of meaning (a single centre between two clusters is far from both;
the file format asks authors to split such a label), or for seven coarse topics (CLINC's intents are fine-grained, which is the harder task).
The cutoffs it chose differ by method and by k (0.67-0.75), and the centre of the scale is a property of the model, which is D2.

**Reproduce.** From the repository root with the venv active and the models cached:

```bash
PYTHONPATH=.:docker/ml-service python anchor_aggregate.py bge-small-en-v1.5
```

with `anchor_aggregate.py` being:

```python
"""Scratch: how should a label's similarity be aggregated from its anchor examples? (max vs mean of top-2 vs centroid), on CLINC with k anchors per intent.

Light by design: one model, ~2000 short texts. Anchors are the FIRST k train examples per intent (deterministic, no tuning on test); the cutoff for
"none of these" is chosen on a validation sample at 95% of in-scope items kept; everything is reported on a test sample.
"""
import sys
import time

sys.path[:0] = [".", "docker/ml-service"]
import numpy as np

from ml_service.registry import load_model_specs
from ml_service.settings import Settings
from ml_service.text_model import load_text_model
from pathlib import Path
from scripts.model_eval import datasets as ds
from scripts.model_eval import public

MODEL = sys.argv[1] if len(sys.argv) > 1 else "bge-small-en-v1.5"
spec = next(s for s in load_model_specs(Path("docker/ml-service/models.toml")) if s.name == MODEL)
model = load_text_model(spec, Settings.from_env({}))
entries = ds.load_public_lock()
entry = next(e for e in entries if e.id == "clinc-oos-plus")
data = public.load_clinc(entry)


def embed(texts):
    out = []
    for i in range(0, len(texts), 32):
        out.append(np.asarray(model.text.embed(texts[i:i + 32]).vectors, dtype=np.float64))
    v = np.vstack(out)
    return v / np.maximum(np.linalg.norm(v, axis=1, keepdims=True), 1e-12)


def sample(items, per_intent, oos_total, seed):
    rng = np.random.default_rng(seed)
    by = {}
    for it in items:
        by.setdefault(it.intent, []).append(it)
    chosen = []
    for intent, rows in by.items():
        if intent == data.oos_index:
            idx = rng.permutation(len(rows))[:oos_total]
        else:
            idx = rng.permutation(len(rows))[:per_intent]
        chosen += [rows[i] for i in idx]
    return chosen


val = sample(data.validation, 6, 150, 1)
test = sample(data.test, 10, 300, 2)
t0 = time.perf_counter()
Xv, Xt = embed([i.text for i in val]), embed([i.text for i in test])
print(f"model {MODEL}  val {len(val)} test {len(test)}  embed {time.perf_counter()-t0:.1f}s")

intents = [i for i in range(len(data.names)) if i != data.oos_index]
for k in (3, 5, 10):
    anchors = {i: [a.text for a in data.train if a.intent == i][:k] for i in intents}
    A = {i: embed(texts) for i, texts in anchors.items()}

    def score(X, how):
        cols = []
        for i in intents:
            S = X @ A[i].T  # (n, k)
            if how == "max":
                cols.append(S.max(axis=1))
            elif how == "top2":
                cols.append(np.sort(S, axis=1)[:, -2:].mean(axis=1) if S.shape[1] >= 2 else S.max(axis=1))
            else:
                c = A[i].mean(axis=0)
                c /= np.linalg.norm(c)
                cols.append(X @ c)
        return np.stack(cols, axis=1)

    for how in ("max", "top2", "centroid"):
        sv, st = score(Xv, how), score(Xt, how)
        conf_v, conf_t = sv.max(axis=1), st.max(axis=1)
        oos_v = np.array([i.oos for i in val])
        oos_t = np.array([i.oos for i in test])
        thr = np.quantile(conf_v[~oos_v], 0.05)  # keep 95% of in-scope items
        pred = np.array(intents)[st.argmax(axis=1)]
        truth = np.array([i.intent for i in test])
        acc = float((pred[~oos_t] == truth[~oos_t]).mean())
        oos_recall = float((conf_t[oos_t] < thr).mean())
        keep = float((conf_t[~oos_t] >= thr).mean())
        # margin: top1 - top2
        srt = np.sort(st, axis=1)
        margin = srt[:, -1] - srt[:, -2]
        print(f"k={k:2d} {how:8s} accuracy {acc:.3f} | cutoff {thr:.3f} keeps {keep:.1%} of in-scope, rejects {oos_recall:.1%} of out-of-scope | median margin {np.median(margin[~oos_t]):.3f}")
```

## R2. How good is the shipped `route-topics` set on this repository's own sentences? (FR-010..012)

**Method.** `make labelset-calibrate` picks each model's cutoff on the 56 **dev** sentences (the cutoff that keeps 95% of the 48 in-scope ones;
it kept 93.8% because with 48 sentences the 5th percentile falls between two of them); `make model-eval KIND=classifier
CANDIDATE=<labelset-…> DATASET=route-topics` reports on the 96 **test** sentences (80 in-scope, 16 `none`). The sentences and the 80 examples are
authored by the implementing agent and are **unreviewed**, so both reports are PROVISIONAL. The label set's version in these runs is `6a3fe2f35057`.

| | bge-small-en-v1.5 (primary) | all-MiniLM-L6-v2 (fallback) |
|---|---|---|
| cutoff chosen on dev | 0.709 | 0.247 |
| in-scope accuracy (nearest label), 95% CI | 0.8125 [0.724, 0.895] | 0.8625 [0.784, 0.936] |
| in-scope macro-F1 | 0.810 | 0.862 |
| `none` sentences rejected at the cutoff (n=16) | 56% [31%, 80%] | 50% [26%, 75%] |
| in-scope sentences wrongly rejected at the cutoff | 22.5% | 16.3% |
| query latency p50 / p95, one text, in-process | 2.0 / 3.0 ms | 0.9 / 1.1 ms |
| controls (identity, shuffled predictions) | both pass | both pass |

Weak spots, from the per-topic lines in the reports (nearest label correct, of 10 per topic, bge / MiniLM): `smalltalk` 10 / 9, `code` 10 / 8,
`high_stakes` 10 / 10, `drafting` 8 / 10, `analysis` 8 / 10, `reasoning` 7 / 8, `lookup` 6 / 6, `action` 6 / 8. The mix-ups are mostly among `lookup`,
`action` and `drafting` ("find out" and "do" and "write" share verbs), plus `lookup` taken for `code` twice on bge.

**Read.** The matcher separates the topics well enough to be a useful signal and not well enough to be trusted alone, which is how the router
uses it (one of two signals, abstaining below the cutoff). The two models' accuracies are within each other's confidence interval, so this says
nothing about which should be primary; the order stays the one `models.toml` already has. About half of the `none` sentences (7 of 16 on bge, 8 of
16 on MiniLM: "go ahead", "yes please", "2026-11-04", "ref 88213", "...") still receive a label. They land on `smalltalk`, `lookup`, `drafting`, `code`,
`analysis` and `action`, and **none lands on `reasoning` or `high_stakes`**. Under the tier table in `contracts/tiered-router.md`, one of the 16
(`...` read as `code` on bge, `2026-11-04` read as `analysis` on MiniLM) would reach the medium tier and none the strong tier. The 22% of in-scope
sentences wrongly rejected on test, against 6% on dev, is the cost of choosing a cutoff on 48 sentences; a larger dev split would tighten it. None of
this was tuned on the test split.

## R3. Would the margin to the runner-up help? (not used; recorded so the next person does not have to rediscover it)

The result carries `margin` (nearest minus second nearest). Descriptively, over all 152 sentences (nothing here was chosen on it): the margin is
low exactly when the nearest label is wrong or the sentence belongs to no topic.

| | bge-small | MiniLM |
|---|---|---|
| median margin, nearest label correct (110 / 113 sentences) | 0.068 | 0.135 |
| median margin, nearest label wrong (18 / 15) | 0.010 | 0.034 |
| median margin, `none` sentences (24) | 0.026 | 0.041 |
| AUC for "correct in-scope" against "none", by similarity / by margin | 0.815 / 0.781 | 0.905 / 0.870 |

So a margin cutoff would catch wrong labels that a similarity cutoff cannot. It is **not built**: the scale differs by model (a 0.03 margin is
"ambiguous" on one and "average" on the other), so it needs a second per-model calibrated number, and choosing two numbers on 48 dev sentences
would overfit. The service already returns the margin, so the change is a `min_margin` beside `min_similarity` in `calibration` once there is a
larger labelled dev set.

**Limits.** 96 test sentences cannot separate two good models by a point, and 16 `none` sentences give a 25-point-wide interval. The sentences
are the author's idea of how users write, not real traffic, and are in English only.
