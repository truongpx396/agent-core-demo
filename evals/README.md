# Model evaluation: data and decisions

This directory is the **data** half of the model-evaluation harness (the code is `scripts/model_eval/`; the design is
`specs/014-model-serving-eval-harness/`). It exists so that the next time someone asks "should we use model X instead?", the answer is a
report produced by running X against what the current model scores on **this repository's own questions**, not a blog post.

```
evals/
├── candidates/      which models can be evaluated (one TOML per kind), and which one is the baseline (`baseline = true`)
├── datasets/        frozen, versioned evaluation sets: <name>/<version>/{manifest.json, *.jsonl} and public.lock.json
├── baselines/       the recorded result of the model in use today: <kind>/<candidate>@<dataset>-<version>.json (+ .items.json)
└── sources/         the authored text the datasets are built from (knowledge-base articles, screening prompts)
```

## Evaluate a model

```bash
make model-eval KIND=embedding CANDIDATE=bge-small-en-v1.5             # one candidate, compared with the baseline
make model-eval-all CANDIDATE=baseline-as-candidate                     # a control, for every kind
make model-eval-verify                                                  # no model, no network: every hash, split, licence, control
make model-eval-data                                                    # fetch the pinned public datasets once (then offline)
```

`make model-eval` writes `var/model_eval/reports/<kind>-<candidate>-<dataset>-<time>.{json,md}` and prints the Markdown. Exit codes:
`0` ok · `2` usage/configuration · `3` a model, dataset or service is missing (**no report is written**) · `4` verification failed ·
`5` a **control** gave the wrong answer: the report is marked `INVALID` and must not be used.

Kinds: `embedding`, `reranker`, `screen` (the prompt-injection screen) and `classifier` (the routing head, below).

## How to read a report

1. **`warnings`.** `PROVISIONAL` means part of the evaluation set has not been read by a person (see below). `INVALID` means a control
   failed. Stop on either. A `WHY:` warning next to `INVALID` on a `classifier` run means the controls failed because of the labelled
   outcomes, not the harness: the strong model scored so little above the local one on average that even a perfect router cannot be told
   from a random one (the first real labelling run: +0.032), so no claim about routing quality can be made until the prompts, the judge
   or the model pair give the strong model a clear win somewhere.
2. **`controls`.** The baseline against itself must come out indistinguishable; a useless model (random vectors, a screen that flags
   nothing) must come out worse. If they do not, the harness is wrong, and a confident-looking number from a wrong harness is the
   worst outcome.
3. **`deltas`.** Act only on `better` or `worse`. **`inconclusive` means "collect more items", not "equal"**; it carries `needed_n`, an
   estimate of how many items would settle it. A few hundred items cannot resolve a one-point difference.
4. **`efficiency`.** Latency belongs to a hardware id. A number measured on a laptop is not the container's number, and the report
   refuses to compare latency across machines.
5. **`tradeoffs`.** For an embedding model this states the vector size per million chunks and that **changing the embedding model means
   re-embedding every collection**.

## The routing classifier (kind `classifier`)

A router answers one question per prompt, "does this prompt need the stronger model?", and what that answer is worth is a fact about **two
specific models and one judge**, so it can only be measured against labelled outcomes that you produce yourself. Nothing is committed and
no router ships: until a person accepts a head on the ML service, `/classify` answers `served_by: "none"` and every turn runs on the local
model.

**There is no head trainer.** One was built (a logistic regression over the embedding) and removed once the first real run showed that the
labels could not support any router (see below and ENGINEERING_PATTERNS.md pattern 69). What stays is everything needed to judge a router
of any kind: the labeller, the outcomes format, the cost-quality metrics, the controls, and a pluggable runner.

```bash
make router-label JUDGE=judge                                    # 1. score both models' answers to the route prompts (minutes per prompt; resumable)
make model-eval KIND=classifier CANDIDATE=random-router          # 2. a control: the floor any router must beat (also: constant)
make model-eval KIND=classifier CANDIDATE=ml-service-classify    # 3. what the ML service's /classify gives, once a head is accepted there
```

1. **Label.** `route-domains` has 32 `dev` and 91 `test` prompts. The labeller runs each through the local alias (`chat`) and the strong
   alias (`chat-strong`), and has the judge alias score each answer 0-1 against a short written rubric. The judge sees the question and one
   answer, never which model wrote it. Output: `datasets/route-domains/v1/outcomes/<strong>.jsonl` (`{id, local, strong, judge}`) and a
   `.meta.json` beside it, both **gitignored**. A score it could not get (timeout, an unusable judge reply twice) is `null`, never `0`.
   Bounded by a per-call timeout and `DEADLINE`; a re-run keeps finished prompts and retries the missing ones. A file scored by one
   judge is never extended by another. A prompt is `needs_stronger` when `strong - local >= 0.2` (the margin).
2. **Read the report.** It gives the decision's `accuracy`, `macro_f1` and `ece` and what the decision is for: `apgr` (the average share of
   the strong-minus-local gap recovered as a function of how many prompts are escalated; a random router scores 0.5), `cpt50` / `cpt90`
   (the smallest share of prompts that must go to the stronger model to recover half / 90% of the gap; **lower is better**) and
   `apgr_vs_length` (against a router that escalates the longest prompts first). **A router is worth turning on only if `apgr` is clearly
   above 0.5 and `apgr_vs_length` has a confidence interval above zero.** The report also times one prompt at a time and warns when p95
   exceeds the 50 ms budget (SC-009).
3. **Plug in a new router.** Anything with a `scores(texts)` method (`runners.classify.ClassifierBackend`) can be handed to
   `runners.classify.run(candidate, settings, backend=...)` and is scored, timed and compared exactly like the controls. The runner is not
   wired to a command for it yet; a router gets a declaration in `candidates/classifier.toml` when it is.
4. **Accept (a person).** For the `/classify` endpoint: a head file with `"accepted": true`, mounted as `ROUTER_HEAD_DIR`
   (`docker/ml-service/heads/README.md`). The app does not ask for a head's score any more (spec 015 routes from rules and labels), so an accepted head is evaluated here but not used by `route_model`.

With **no outcomes** the run ends with `UNMEASURED` (exit 3) and writes no report: routing quality is never a number computed from
nothing. The same happens if the strong model did not beat the local one on the test prompts, because then there is nothing to recover.
There is **no recorded baseline** for this kind (nothing is in use), so every run says so and compares nothing; `constant` and
`random-router` are runnable controls whose numbers are the floor. The derived controls build the best possible router from the same
outcomes and check that the harness tells it from a random and a constant one.

**What the first real run showed.** 123 prompts, `qwen2.5:3b` against `qwen2.5:7b`, judged by the same 7B: the strong model scored only
+0.032 above the local one on the test prompts, on a judge scale that is nearly two-valued. Two heads trained on those labels did no better
than "never escalate", and the tags the prompts carry (domain, difficulty) predicted nothing about where the stronger model wins. The
controls fail on such data and the report says why (`WHY:`). The limit is the labels, not the method: a classifier does well on a standard
intent set (CLINC150, `clinc-oos-plus`, pinned in `datasets/public.lock.json`, scored by `suites.IntentSuite`), but no router can be judged
until the stronger model wins clearly somewhere, with a judge that is not itself that model.

## Label sets (a different question from routing)

A **label set** (`docker/ml-service/labelsets/<name>.json`, specs/015) lists a few choices with example sentences, and the ML service answers
`POST /classify task=label` with the choice a text is nearest to, or "none of these" below a cutoff that is stored per model. That is a
classification task with a known right answer, so it is judged on sentences whose topic is known, not on routing outcomes:

```bash
make labelset-calibrate CANDIDATE=labelset-bge-small-en-v1.5                                  # choose the cutoff on the DEV split; prints the line to paste
make model-eval KIND=classifier CANDIDATE=labelset-bge-small-en-v1.5 DATASET=route-topics     # report on the TEST split
```

The report gives in-scope accuracy and macro-F1 (is the nearest label right?), `oos_recall@thr` (the share of "none of these" sentences the cutoff
rejects) and `in_scope_reject@thr` (the share of real topics it wrongly rejects), the per-topic results and the commonest mix-ups, and the shuffled-
predictions control. It runs the service's own code on the model in-process, so it needs the model cached and `PYTHONPATH` to include
`docker/ml-service` (the Makefile sets it). `ece_in_scope` treats a similarity as a probability and is not meaningful for this matcher.
`evals/datasets/route-topics` is authored, unreviewed and small (96 test sentences), so every report is PROVISIONAL and two good models cannot be
ranked by a point; what it can tell is a matcher that works from one that does not, and which topics get confused (`specs/015/research.md`).

## Add a candidate

Add a `[[candidate]]` table to `candidates/<kind>.toml`. A name is `[a-z0-9._-]+` (no `@`: it separates the candidate from the dataset in
a baseline's filename). A licence is required. An `inprocess` candidate pins a **full 40-hex commit sha**, never a branch, and names its
pad token (the harness never guesses one). Nothing else changes.

## Add or change a dataset

Datasets are **frozen**: a manifest records the sha256 of every file, and any mismatch stops the run. Do not edit a dataset in place; add
`v2/`. A baseline recorded on `v1` is refused, not warned about, when compared against a `v2` run.

- `dev` is for tuning and is **refused for reporting**; `test` is what is reported. They are disjoint by id and by near-duplicate text.
- `reviewed` is a per-item flag a **person** sets. The report's `reviewed_share` is computed from the items. A baseline recorded while it
  is below 1.0 is `provisional`; re-record after review.
- Repository-own sets contain **no tenant or customer data**. A licence is required: the repository has no `LICENSE` file, so authored
  data is marked `repository-internal` rather than claim one.
- Public sets (`datasets/public.lock.json`) are pinned by commit and per-file hash, carry a licence from a short allow-list, and are never
  redistributed: they download into `var/model_eval/public/` (untracked).

| Public set | Used for | Licence (Hub card, read 2026-10-17) |
|---|---|---|
| `BeIR/scifact`, `BeIR/nfcorpus` (+ `-qrels`) | embedding, reranker | CC BY-SA 4.0 |
| `deepset/prompt-injections` | screen | Apache-2.0 |
| `jackhhao/jailbreak-classification` | screen | Apache-2.0 |

Refused, with the reason on the card: `mteb/msmarco` (research-only terms), `mteb/scifact` and `mteb/fiqa` (no licence stated),
`xTRam1/safe-guard-prompt-injection` (no licence), `allenai/wildjailbreak` (gated), `PolyAI/banking77` (the repo ships a loader *script*,
and running a dataset's script is running downloaded code).

## Rebuilding the repository's own sets

```bash
python -m scripts.model_eval.build.retrieval qwen2.5:7b    # questions from a LOCAL model, filtered; needs Ollama with that model
python -m scripts.model_eval.build.rerank                  # fixed candidate lists (BM25 hard negatives; no model involved)
python -m scripts.model_eval.build.screen                  # from evals/sources/screen/*.tsv
python -m scripts.model_eval.build.route_prompts qwen2.5:7b
```

Questions are model-written and then filtered so a question cannot be a copy of its passage (no contiguous run over 40 characters in
common), cannot duplicate another, and cannot leak across the dev/test split. **They are written `reviewed: false`.** The generation is
as reproducible as a seeded local model is; the committed files are the record.

## Three measurement lessons (why the harness has controls and a hardware id)

These cost real time while this was built, and each is now enforced rather than remembered:

1. **int8 is not automatically faster.** On an ARM laptop CPU, fastembed's int8 bge-small was *slower* than the official fp32 file
   (35.8 vs 17.1 ms per chunk). On x86 it may well be the opposite. The candidate list has both files for the same model; measure on the CPU
   you deploy to.
2. **Use real text.** A 110-token synthetic paragraph understated real 259-token markdown chunks by 2.4x, and the first reported cost per
   chunk was 2.7x too high. The retrieval set is chunked by the app's own chunker.
3. **The same model is not the same cost on different hardware.** nomic as fp32 ONNX on CPU is 3-5x slower than Ollama on Metal; a small
   model is on par. The report keys latency by hardware id and will not compare across ids.

## Known gaps

- The repository-own sets are small (tens of chunks, a hundred or so questions) and unreviewed. They can separate a good model from a
  useless one; they cannot rank two good ones within a point. Review them (set `reviewed: true`), grow them, then re-record the baselines.
- Questions are written by a 7B local model, so they skew toward factual lookups of the passage. Hand-written adversarial questions are
  the most valuable addition.
- Latency is measured on the maintainer's machine; the baselines carry that machine's hardware id until someone records another
  (`make model-eval-baseline ... ADD_HARDWARE=1`).
- **Routing labels are weak.** The local and strong models answer WITHOUT retrieval, so a prompt about a company policy is scored on how
  honestly the model handles not knowing it, not on a grounded answer. A judge that is also the strong model favours its own style (use a
  different judge when one exists). And a head is trained on 32 prompts and reported on 91: it can show whether the method works at all, and
  it cannot show that routing helps real traffic. Grow the route set (a new `v2/`) before relying on a head.
