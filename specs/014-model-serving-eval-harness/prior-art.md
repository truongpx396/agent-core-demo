# Prior art: how model benchmarks are built

Research for FR-001 to FR-014 (the harness), written 2026-10-17 before planning. It records what was found, how sure each
finding is, and what `/speckit-plan` still has to verify. Nothing here is a requirement; the requirements are in `spec.md`.

**Confidence labels.** *Sourced* = read on a page this session (link given). *Practice* = well-known convention stated from
general knowledge, **not** confirmed by a source in this session — plan must verify before relying on it.

## 1. Embedding / retrieval models

| Finding | Confidence | Source |
|---|---|---|
| Retrieval is headlined by NDCG@10 (BEIR / MTEB retrieval). | Practice; consistent with the model cards read (gte-modernbert-base, nomic, arctic-embed, granite all quote NDCG@10-style retrieval averages) | model cards |
| Publisher scores come from different tables and benchmark versions (MTEB English v1, English v2, multilingual, RTEB) and are **not** comparable across cards. | Sourced | granite-embedding-small-english-r2 card; EmbeddingGemma paper; secondary roundups disagree with each other |
| For a RAG pipeline, build a **frozen golden set** of a few hundred realistic questions, each mapped to the passage(s) that answer it, and re-run it on every change so each change is a measurable before/after. | Sourced (practitioner guides) | [Qdrant: Measuring Retrieval Relevance](https://qdrant.tech/documentation/search-evaluation/retrieval-relevance/), [NVIDIA: evaluating RAG with synthetic data](https://developer.nvidia.com/blog/evaluating-and-enhancing-rag-pipeline-performance-using-synthetic-data/) |
| Synthetic questions: have a model write a question answerable *only* from one chunk's fact; add adversarial questions that resemble several chunks but are answered by one; filter by answerability and ease. | Sourced | same |
| Pick the metric by stage: recall@k for first-pass retrieval, MRR for binary labels, NDCG@k when labels are graded (and for reranking). **Match k to what the pipeline passes on** — reporting @100 for a UI that shows 5 flatters the model. | Sourced | same |
| Leakage = any setup that makes offline metrics look better than production reality. | Sourced | same |

**Adopted.** Frozen repo-own golden set (FR-009); recall at the app's own list sizes, 20 before rerank and 5 final
(FR-003); NDCG@10 and MRR; generated-then-human-reviewed questions with recorded provenance (FR-009); disjoint dev/test and
duplicate detection (FR-010). **Our position (not from a source):** a public leaderboard is a sanity check, never a
substitute for the repo-own set.

## 2. Rerankers

| Finding | Confidence | Source |
|---|---|---|
| BEIR protocol for cross-encoders: retrieve a fixed first-stage list (top-100 by BM25), rerank it, report NDCG@10. Other papers use other K (UPR reranks 1,000), so **K must be stated** or gains are not comparable. | Sourced | [HYRR](https://arxiv.org/pdf/2212.10528) (quoting Thakur et al., 2021) |
| MS MARCO passage ranking leads with MRR@10; another study finds MRR noisier than NDCG/MAP, so NDCG@10 is the steadier lead metric. | Sourced | [Scaling Laws for Cross-Encoder Reranking](https://arxiv.org/pdf/2603.04816); Sentence-Transformers docs |
| Cost scales with candidate count: one preprint reports ~43.5 ms/query (MiniLM) vs ~409.6 ms (BGE) at 128 passages on an A100. Report latency at a stated K and hardware. | Sourced (single preprint) | same |
| Vendor latency / BEIR figures from blogs are unverified. | Sourced | roundup pages |

**Adopted.** Fixed candidate lists of a stated size, independent of whatever the current embedding model retrieves
(Story 1, Scenario 4); NDCG@10 + MRR@10; latency per query at that K.

## 3. Routers (choose the cheap or the strong model)

| Finding | Confidence | Source |
|---|---|---|
| RouterBench plots each router on a **cost–quality plane**, summarises it as AIQ (normalised area under the curve), and ships **pre-generated responses and quality scores for every model** so routers are evaluated *without running inference*. | Sourced | [RouterBench](https://arxiv.org/html/2403.12031v2) |
| RouteLLM (binary strong/weak) reports **APGR** (average performance gap recovered across cost budgets) and **CPT(x%)** (the least share of calls to the strong model that recovers x% of the gap), against a **random router** baseline. | Sourced | [RouteLLM paper summaries](https://shekhargulati.com/2024/07/09/routellm-paper/), [LMSYS blog](https://lmsys.org/blog/2024-07-01-routellm/) |
| BERT-scale classifiers add roughly 10–30 ms per decision. | Sourced (one practitioner estimate) | [LLM routing in production](https://tianpan.co/blog/2025-10-19-llm-routing-production) |

**Adopted.** Record each routing prompt's outcome for *both* models once, then evaluate any head offline from the record
(no inference per candidate) — RouterBench's central idea; cost–quality curve with a CPT-style reading ("share
escalated to recover x% of the gap") and a random router as control (Story 1, Scenario 6; FR-003, FR-038); added control: a
prompt-length router, because length is the cheapest signal a trained head must beat. **Gap:** there is no off-the-shelf
"is this too hard for a 3B model" label set that matches this stack; labels come from this repo's own prompts (FR-030,
FR-039).

## 4. Prompt-injection / input screening

| Finding | Confidence | Source |
|---|---|---|
| Prompt Guard 2's card reports **AUC** and **recall at 1% false-positive rate** on a *private* held-out set, plus latency (22M: AUC .995, 88.7% recall at 1% FPR, 19.3 ms; 86M: .998, 97.5%). | Sourced from a mirror of the card | [Llama Prompt Guard 2 86M mirror](https://huggingface.co/project-free-llama/Llama-Prompt-Guard-2-86M); [22M mirror](https://huggingface.co/hipocap/Llama-Prompt-Guard-2-22M) |
| Figures conflict between sources (LlamaFirewall paper gives the 86M model AUC .98; Groq pages swap the 22M/86M numbers). | Sourced | [LlamaFirewall](https://arxiv.org/pdf/2505.03574), Groq docs |
| PINT (Check Point) is a neutral benchmark of 4,314 inputs (3,016 English, 1,298 non-English) with one aggregate score; its leaderboard puts Prompt Guard 2 (86M) near 78.8%. Vendor private-set numbers and PINT scores are **not comparable**. | Sourced | [PINT benchmark](https://github.com/CheckPointSW/pint-benchmark) |

**Adopted.** Detection rate at a fixed 1% FPR plus ROC-AUC (FR-003), and — what vendor cards omit and this app needs —
the false-positive rate on benign prompts *in this app's domains* at the app's operating threshold (Story 1, Scenario 5). **Lesson:**
never trust a published figure for a screen; record our own number on our own sets (FR-006).

## 5. Classifiers generally

| Finding | Confidence | Source |
|---|---|---|
| BANKING77 (13,083 queries, 77 intents) and CLINC150 (22,500 in-scope across 150 intents plus 1,200 out-of-scope) are standard intent sets; CLINC150's out-of-scope examples exist to test out-of-domain behavior. Both appear as CC BY 4.0. | Sourced for the numbers; **licence unconfirmed at the source repositories** | [UCI CLINC150](https://archive.ics.uci.edu/dataset/570/clinc150), legacy HF BANKING77 loader |
| Macro-F1, expected calibration error and selective prediction (abstain below a confidence) are the usual measures for a production classifier. | Practice — **no source read this session** | — |
| Few-shot fine-tuning is unstable; SetFit-style methods help at ~8 examples per class, but the gap shrinks by ~64. A comparison of frozen embeddings + logistic regression against fine-tuning was **not found**. | Sourced (replication paper, forum) | [arXiv 2306.08058](https://arxiv.org/pdf/2306.08058) |

**Adopted.** Macro-F1 and calibration error beside accuracy (FR-003); the head is judged on a held-out split, and the
open question (does a frozen-embedding head match a fine-tuned one on our data?) is answered by running it through the
harness, not argued here.

## 6. Cross-cutting practice adopted

- **Report uncertainty.** A few hundred examples cannot resolve a one-point difference; every delta is *better / worse /
  inconclusive* (FR-005). *Practice* (bootstrap confidence intervals); the bootstrap itself is plan work.
- **Controls validate the harness.** The baseline against itself and a degenerate model must come out as expected
  (FR-008) — the cheapest way to catch a broken metric.
- **Measure through the production path** (the gateway alias, the service endpoint), not only the library, because the
  real bugs here have been in the path (FR-013) — the prefixes were never applied precisely because nothing measured the
  path.
- **Latency belongs to hardware.** A number without a hardware profile is not comparable (FR-002, FR-006).
- **Freeze and version datasets**; a dataset that changes under the same name invalidates every baseline (edge cases).

## 7. Questions for `/speckit-plan` to settle

1. Licence of each public candidate set at its **source** (BEIR subsets, a prompt-injection set, BANKING77, CLINC150, PINT's
   public example data); anything unconfirmed is excluded until it is.
2. Whether to depend on the `mteb` library for the public retrieval sets. It is the reference implementation of the
   protocol, but it pulls a heavy framework; the harness is a development tool, not part of the service images, yet it
   must still not be added to `requirements.txt` without a stated reason.
3. How to generate the repo-own question sets: model-written then person-reviewed, and which model writes them (the
   constitution allows a cloud model for evaluation roles only).
4. The exact bootstrap procedure and the smallest set size at which a one-point NDCG difference becomes resolvable.
5. How the retrieval degrade path behaves today, to reuse it for an embedding-identity mismatch (FR-019).
6. How cost governance treats an unpriced alias (FR-036), and whether a per-turn model choice needs a checkpointed state
   change (FR-034) and therefore a schema-version bump.
