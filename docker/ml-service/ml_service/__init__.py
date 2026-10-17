"""The ML service's package (specs/014-model-serving-eval-harness, tasks T008-T014).

`docker/ml-service/main.py` used to be one 300-line file with no importable logic. Adding embeddings, a classifier, per-model health
and metrics would have doubled it and mixed four responsibilities, and the head trainer (scripts/model_eval/head.py) must run the
SAME tokenise/pool code the service serves with, or a head can be trained on one pipeline and served on another. So the logic lives
here, importable by tests and scripts, and `main.py` is a one-line shim (`from ml_service.app import app`) so the image's
`uvicorn main:app` entry point is unchanged.

Modules, one concern each: `batching` (the micro-batcher), `onnx_text` (tokenise, run, pool), `registry` (declared models and their
load state), `rerank` and `guard` (the two endpoints this service always had), `health`, `metrics`, `settings`, `app` (wiring).
"""
