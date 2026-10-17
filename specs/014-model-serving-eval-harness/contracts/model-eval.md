# Contract: the model-evaluation harness

A development tool, **not** part of any service image (nothing under `scripts/` is copied into `docker/ml-service`). Manual tier
like `make eval`; its pure parts are hermetic and run in `make test` (FR-012).

## Commands

All through `python -m scripts.model_eval <command>`; the Makefile wraps them. Targets are `model-eval*` — never `eval*`, which is
the golden-dataset run.

| Make target | Command | What it does |
|---|---|---|
| `make model-eval KIND=embedding CANDIDATE=<name>` | `run` | evaluate one declared candidate on that kind's datasets; write a report; compare to the recorded baseline |
| `make model-eval-all CANDIDATE=<name>` | `run --all-kinds` | the same for every kind the candidate declares |
| `make model-eval-baseline KIND=… CANDIDATE=…` | `baseline` | evaluate and **record** a baseline (refuses if one exists unless `FORCE=1`; prints `PROVISIONAL` when the dataset is not fully reviewed) |
| `make model-eval-data` | `fetch` | download and verify the public datasets in `public.lock.json` into `var/model_eval/public/` |
| `make model-eval-verify` | `verify` | recompute every committed dataset/baseline hash; check disjoint splits, licences, controls' expectations (no model needed) |
| `make router-label` | `route-label` | run the route prompts through the local and strong aliases and a judge; write `outcomes/<strong>.jsonl` (needs live services; requires `--judge`) |
| `make router-head-train` | `head train` | train and export a head for a classifier model from labelled outcomes; never sets `accepted` |

Make passes `VIA=gateway|service|inprocess` as `--path` (never name a make variable `PATH`: it would replace the shell's).
Flags common to `run`/`baseline`: `--path gateway|service|inprocess` (default: the candidate's declared path), `--splits test`
(`dev` is for tuning and is refused for reporting), `--runs N` (repeat for latency, default 3), `--seed`, `--out DIR`
(default `var/model_eval/reports/`), `--no-compare`.

**Exit codes.** `0` ok · `1` an unexpected error · `2` usage/configuration error (unknown candidate, missing licence) · `3` a
prerequisite is missing — a model, dataset or service not available; **no report is written** (FR-011) · `4` verification failed
(hash mismatch, split overlap, control verdict wrong, licence not allowed) · `5` ran, but at least one *control* gave the wrong verdict
(the report is written and marked `INVALID`; a result from a harness whose controls fail is not to be used).

## Report (JSON; the schema is `contracts/model-eval.report.schema.json`, validated by a hermetic test)

```json
{
  "schema_version": 1, "kind": "embedding", "status": "ok" | "provisional" | "invalid",
  "candidate": {"name": "…", "identity": "…", "revision": "…", "licence": "…", "size_bytes": 0, "dim": 384, "path": "service"},
  "baseline":  {"name": "…", "file": "evals/baselines/…", "hw_used": "hw-3fa1c2d4", "latency_comparable": true},
  "dataset": {"name": "retrieval-domains", "version": "v1", "split": "test", "items": 120, "reviewed_share": 0.0,
              "public": [{"id": "beir-scifact", "revision": "…", "licence": "CC-BY-SA-4.0"}]},
  "hardware": {"id": "hw-3fa1c2d4", "cpu": "Apple M4 Pro", "cores": 12, "threads": 12, "onnxruntime": "1.29.0", "os": "Darwin"},
  "repo_revision": "c4fcc64", "ran_at": "…",
  "quality": {"ndcg@10": {"value": 0.0, "ci95": [0.0, 0.0]}},
  "efficiency": {"query_ms": {"p50": 0, "p95": 0}, "ms_per_chunk": 0, "rss_mb": 0, "run_seconds": 0},
  "deltas": {"ndcg@10": {"delta": 0.0, "ci95": [0.0, 0.0], "verdict": "better|worse|inconclusive", "needed_n": null}},
  "controls": [{"name": "baseline-as-candidate", "expect": "no_difference", "observed": "no_difference", "ok": true}],
  "tradeoffs": ["quality: …", "speed: …", "size: …", "licence: …", "embedding: dim 384 → 1.5 GB per million chunks; changing model requires re-embedding every collection"],
  "warnings": ["PROVISIONAL: 0% of evaluation items reviewed"]
}
```

**Rules.**
- A delta's `verdict` comes from the **paired** bootstrap (`research.md` A4); `inconclusive` carries `needed_n`.
- `latency_comparable: false` (different hardware id) suppresses latency deltas and adds a warning; quality deltas stand.
- `efficiency.run_seconds` is the wall-clock of the whole run (SC-001: ≤ 600 s for one kind on the repo-own sets); a longer run adds a warning, it does not fail.
- The classifier report adds a warning when `/classify` p95 exceeds 50 ms (SC-009's budget).
- A report never contains item text, only ids and aggregate numbers (Principle V: no content in telemetry; reports are written to the
  untracked `var/` tree but may be pasted into a PR).
- `status: invalid` if any control failed; `provisional` if any evaluation item was unreviewed.
- Rendering is a view of the JSON: a fixed Markdown table per section (quality, efficiency, trade-offs, warnings); the JSON is the
  record.

## Reading order for a decision

1. `warnings` (PROVISIONAL / INVALID / contaminated / unreviewed). 2. `controls` all ok. 3. `deltas` — act only on `better`/`worse`;
`inconclusive` means *collect more items*, not *equal*. 4. `efficiency` against the container's CPU budget, not the laptop's.
5. `tradeoffs` — dimension and re-embed cost for an embedding model; licence for all.

## What it does not do

It changes no setting, writes no collection, calls no tenant's data (FR-014, FR-039). Its only network use is `fetch` (Hub downloads)
and, for `gateway`/`service` paths, the services the maintainer has running.
