# Label sets

A **label set** is a reviewed file that names a handful of choices and gives a few example sentences for each. `POST /v1/decide` with a
`choice` question named after the set (`{"state": "...", "questions": {"<name>": {"type": "choice", "instructions": "...", "criteria": {"<label>": "<what it means>", ...}}}}`)
answers which choice the text is nearest to, or `null` when it is near none of them (specs/015, specs/016; the `criteria` keys must be exactly the set's labels). Nothing is trained: the service embeds the examples with the model that serves the request and compares.

The shipped set is `route-topics.json` (what kind of work a message asks for; the app turns each topic into a model tier). To add another, for
example "does this message ask us to send an email?", add `<name>.json` here, or mount your own directory and point `LABELSET_DIR` at it.

## The file

```json
{
  "schema_version": 1,
  "name": "email-intent",
  "description": "free text for the next reader",
  "labels": {
    "send_email": ["Email the customer the new quote.", "Send Priya a note about the delay.", "…at least three, at most 64…"],
    "no_email":   ["What is our refund policy?", "Summarise this thread.", "…"]
  },
  "calibration": {
    "bge-small-en-v1.5": { "min_similarity": 0.709 },
    "all-MiniLM-L6-v2":  { "min_similarity": 0.247 }
  }
}
```

- **The file name is the set's name** (`email-intent.json` has `"name": "email-intent"`). 2..64 labels, 3..64 examples each, 512 in all, each
  example 1..300 characters, and **no example may appear under two labels** (two labels sharing a sentence can never be told apart).
- **One label should be one cluster of meaning.** The service averages a label's examples into one point. A label that is really two topics
  ("billing questions" and "shipping questions") belongs in two labels.
- **Write examples the way users write.** Short, varied, real-sounding. Keep them different from the sentences you will judge the set on.
- **`calibration` has an entry for every model that serves requests** (`docker/ml-service/models.toml`, the `classify` roles). It is the
  similarity below which the answer is "none of these". It differs by model, because the models place sentences on different scales (bge-small's
  cutoff is about 0.7, MiniLM's about 0.25). **A model with no entry does not serve the set**, so the fallback model cannot be handed the
  primary's number by accident.

## Choose the cutoff, do not guess it

```bash
make labelset-calibrate CANDIDATE=labelset-bge-small-en-v1.5 LABELSET=route-topics   # prints the line to paste under "calibration"
make model-eval KIND=classifier CANDIDATE=labelset-bge-small-en-v1.5 DATASET=route-topics   # then judge it on held-out sentences
```

The first picks the cutoff that keeps 95% of the in-scope sentences of the **dev** split (and says what it does to the `none` ones); the second
reports on the **test** split. A new set needs a dataset of sentences with their right answers (`evals/datasets/route-topics/` is the model:
`evals/sources/route-topics/prompts.tsv` and `scripts/model_eval/build/topics.py`).

## What the service tells you

`GET /health/models` lists each set under `labelsets` with its `version` (a hash of the labels, examples and cutoffs, so a changed file is a
changed version) and, per model, `loaded`, `pending: …` or `refused: …` (`no calibration for <model>`, `malformed (…)`, `unreadable (…)`). A set
that cannot be read is refused alone: the other sets, the embeddings, the reranker and the guard start and serve as usual.

**The directory must be readable by the service's user** (uid 1000 in the image: directory `755`, files `644`). Sets are read when the service
starts, so mount new ones and restart the container. A changed model revision is picked up without a restart: the examples are embedded again.

## What a result means

`label` is the nearest label, or `null` below the model's cutoff. `nearest` is the closest label even when the cutoff said no. `similarity` is the
cosine to that label's centre. `runner_up` and `margin` describe the second closest: a small margin means the text sits between two choices. The
service never decides what a label means for the caller (a model tier, an email, a queue); that belongs to the code that asks.
