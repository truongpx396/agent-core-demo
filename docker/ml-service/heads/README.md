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
