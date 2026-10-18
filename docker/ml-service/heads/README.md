# Classifier heads

A **head** is the small trained part of the routing classifier: a logistic-regression weight vector over the embedding of one specific
model (specs/014). Two files per model, named after the registry entry:

- `head-<model>.npz` — `w` (float32, one weight per embedding dimension) and `b` (float32).
- `head-<model>.json` — provenance and the binding: the model's repo, file, **full commit sha** and dimension; the dataset it was trained
  on; held-out metrics; the chosen threshold; and `"accepted": true|false`.

The service serves a head only if it was trained against **exactly** the model revision it is loaded with, **and** `accepted` is `true`.
A head for another model (or another revision of the same one) is meaningless on these vectors, and a trained-but-unvalidated head must
never influence a turn. The trainer (`make router-head-train`) always writes `accepted: false`; a person sets it after the evaluation
harness shows the head beats the random and prompt-length routers on held-out data.

**No head ships in this repository.** With none, the classifier answers "no advice" and every turn runs on the local model, exactly as
before. Mount your own directory and point `ROUTER_HEAD_DIR` at it, or copy accepted heads here before building the image.

`GET /health/models` shows each classify model's head as `loaded`, `absent`, `pending: model not ready`, or `refused: <reason>` (`not accepted`,
`revision mismatch`, `repo mismatch`, `file mismatch`, `model mismatch`, `dimension mismatch`, `malformed (…)`, or `unreadable (…)`). Heads are read
when the service starts, so mount new ones and restart the container.

**The mounted directory must be readable by the service's user** (uid 1000 in the image: directory mode `755`, files `644`, or owned by uid 1000).
A directory the service cannot read is `refused: unreadable` for every head and routing stays off; the rest of the service (embeddings,
rerank, prompt guard) starts and serves as usual. It used to stop the whole service at start-up (found by CI, where a `0700` temp directory
was mounted), so if `/health/models` says `unreadable`, check the mode and owner of the directory first. `head_version` in the JSON is what `/classify` returns with every score, so a score can always
be traced to the head that produced it.
