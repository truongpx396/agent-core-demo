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
CANDIDATE=<labelset-…> DATASET=route-topics` reports on the 96 **test** sentences (80 in-scope, 16 `none`). The sentences and the examples are
authored by the implementing agent and are **unreviewed**, so both reports are PROVISIONAL. Two versions of the set were measured, because the
first look at real prompts (R4) led to ten more `lookup` examples: `6a3fe2f35057` (80 examples) and the shipped `5b42dff1816d` (90). The
cutoffs came out the same for both (0.709 and 0.247).

| test split, n=96 | bge-small, first (80) | bge-small, **shipped (90)** | MiniLM, first (80) | MiniLM, **shipped (90)** |
|---|---|---|---|---|
| in-scope accuracy (nearest label), 95% CI | 0.8125 [0.724, 0.895] | **0.8250 [0.740, 0.903]** | 0.8625 [0.784, 0.936] | **0.8875 [0.810, 0.951]** |
| in-scope macro-F1 | 0.810 | 0.822 | 0.862 | 0.889 |
| `none` sentences rejected at the cutoff (n=16) | 56% [31%, 80%] | 56% [31%, 80%] | 50% [26%, 75%] | 50% [26%, 75%] |
| in-scope sentences wrongly rejected | 22.5% | 23.8% | 16.3% | 15.0% |
| query latency p50 / p95, one text, in-process | 2.0 / 3.0 ms | same | 0.9 / 1.1 ms | same |
| controls (identity, shuffled predictions) | pass | pass | pass | pass |

Per topic (nearest label correct, of 10), shipped set, bge / MiniLM: `smalltalk` 10 / 9, `code` 10 / 8, `high_stakes` 10 / 10, `drafting` 8 / 10,
`analysis` 8 / 10, `reasoning` 7 / 8, `lookup` 8 / 8, `action` 5 / 8. The mix-ups are mostly among `lookup`, `action` and `drafting` ("find out" and
"do" and "write" share verbs). The ten extra `lookup` examples moved `lookup` from 6 to 8 on both models and `action` from 6 to 5 on bge: all within the
noise of ten sentences per topic.

**Read.** The matcher separates the topics well enough to be a useful signal and not well enough to be trusted alone, which is how the router
uses it (one of two signals, abstaining below the cutoff). The two models' accuracies are within each other's confidence interval, so this says
nothing about which should be primary; the order stays the one `models.toml` already has. About half of the `none` sentences (7 of 16 on bge, 8 of
16 on MiniLM: "go ahead", "yes please", "2026-11-04", "ref 88213", "...") still receive a label. They land on `smalltalk`, `lookup`, `drafting`, `code`,
`analysis` and `action`, and **none lands on `reasoning` or `high_stakes`** (first version; not re-counted for the shipped one). Under the tier table in
`contracts/tiered-router.md`, one of the 16 (`...` read as `code` on bge, `2026-11-04` read as `analysis` on MiniLM) would reach the medium tier and none the
strong tier. The 24% of in-scope sentences wrongly rejected on test, against 6% on dev, is the cost of choosing a cutoff on 48 sentences; a larger dev
split would tighten it. None of this was tuned on the test split.

**Limits.** 96 test sentences cannot separate two good models by a point, and 16 `none` sentences give a 25-point-wide interval. The sentences
are the author's idea of how users write, not real traffic, and are in English only.

## R3. Would the margin to the runner-up help? (not used; recorded so the next person does not have to rediscover it)

The result carries `margin` (nearest minus second nearest). Descriptively, over all 152 route-topics sentences, on the FIRST version of the set (nothing here was
chosen on it): the margin is low when the nearest label is wrong or the sentence belongs to no topic. **R4 shows the same does not hold on real prompts.**

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

## R4. What the router does with this repository's own prompts (the finding that changed the shipped set)

`route-domains` holds 123 prompts the repository's agents are really asked (32 dev, 91 test; mostly knowledge-base lookups, none of them tenant data).
They have no topic labels, but they are the closest thing to real traffic, and what a router does with them is its cost. The tier spread was computed
with the app's own `decide` (rules + label, bge-small, all three tiers configured, no ceiling); the rules fired on **none** of the 123 (they are short,
plain questions), so every escalation came from the label.

**First look (80 examples), all 123 prompts: 13 routed up (9 medium, 4 strong), 11%.** Reading them, nearly all are ordinary lookups the label set
misread: "What's the minimum order quantity for your services?" as `reasoning` (strong), "How often do you offer volume discounts?" and "Do you have any
case studies or success stories?" as `analysis` (medium), "Could you check if there's a runbook for the database failure?" as `code`. The fallback model
(MiniLM) routed 19 of 123 up (15%). The margin does not help here, unlike on the authored sentences (R3): the routed-up prompts had a median margin of
0.029 against 0.028 for the ones correctly labelled local, so a margin cutoff removes right and wrong labels in equal measure. The cause is coverage: ten
`lookup` examples do not span how this app's users ask about pricing, discounts, case studies, runbooks and procedures.

**What was done, and under what hygiene.** The errors were read on the route-domains **dev** split only (6 of 32 routed up). Ten `lookup` examples were
written from the *kinds* of question they show, not copied (a test now fails if an example is a near-duplicate of any route-domains prompt), the cutoffs
were re-chosen on the route-topics dev split (unchanged), and two things were measured: route-topics **test** for regression (R2: none) and
route-domains **test**, which had not informed any example.

| routed up by the label signal (bge-small) | before (80 examples) | after (90 examples) |
|---|---|---|
| route-domains dev (32), in-sample: these informed the examples | 6 (19%) | 1 (3%) |
| route-domains **test** (91), held out | **7 (8%)** | **5 (5%)** |

**Read.** One round of "read what shadow shows, add examples, recalibrate" cut the held-out false escalation from 8% to 5%; it is a small effect on 91
prompts (7 to 5) and should not be read as more than that. What is left on test is one `high_stakes` ("What's the procedure to report a security
vulnerability?", an escalation the policy might even want) and four `analysis` ("How does your pricing compare to competitors for similar services?",
"Do you offer any special deals for new clients?", "How does your pricing scale with the number of users?", "Do you have any case studies ... I can
review?"): sales questions that sit next to "compare the plans" in meaning. Fixing those further on this test split would be tuning on it. The workflow is
the deliverable, not the number: an operator running `ROUTER_MODE=shadow` reads `agent_model_route_tier_total` and the `model_route` log lines (label,
similarity decile, never the text), finds the misread kinds of question, adds examples, runs `make labelset-calibrate`, and repeats.

**What this does and does not say about cost.** On a workload like this one, at roughly 5-11% of turns routed up by a signal that is often wrong, the
router would add cost for little or no benefit unless the escalated turns really are answered better, which nothing measured here shows (the only routing
outcomes that exist could not tell a perfect router from a random one; the harness run of this router on them gives an average gap recovered of 0.42
against 0.50 for random, interval -0.43 to 1.22, and is INVALID for that reason). `ROUTER_MAX_TIER=medium` bounds the strong share, and the default stays off.
