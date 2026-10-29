"""The model-evaluation harness (specs/014-model-serving-eval-harness, User Story 1). A maintainer declares a
candidate model, runs one command for its kind (embedding, reranker, prompt-injection screen, routing
classifier) and gets a report putting it next to the recorded baseline of the model in use: quality on this
repo's own domains and on public sets, speed, size and licence, and for every quality number whether the
difference is real or inside the noise of a small set. A model decision is made by running candidates, and the
tool is itself proven by controls (the baseline against itself must show "no difference"; a deliberately
useless model must come out worse).

Run it as `python -m scripts.model_eval <command>` (see __main__.py and specs/014 contracts/model-eval.md). A
manual tier like `make eval`: it needs models and, for the gateway path, services. The pure parts (metrics,
statistics, report format, baseline comparison, dataset checks) are hermetic and run in `make test`.
"""
