"""The model-evaluation harness (specs/014-model-serving-eval-harness, User Story 1).

A maintainer declares a candidate model, runs one command for its kind (embedding, reranker, prompt-injection screen, routing
classifier), and gets a report that puts it next to the recorded baseline of the model in use today: quality on this repository's own
domains and on public reference sets, speed, size and licence, and for every quality number whether the difference from the baseline
is real or inside the noise of a small evaluation set. It exists so a model decision is made by running candidates, not by a blog
post, and so the tool that decides is itself proven by control candidates (the baseline against itself must come out "no difference";
a deliberately useless model must come out worse).

Run it as `python -m scripts.model_eval <command>` (see __main__.py and specs/014 contracts/model-eval.md). A manual tier, like
`make eval`: it needs models and, for the gateway path, running services. Its pure parts (metrics, statistics, report format,
baseline comparison, dataset checks) are hermetic and run in `make test`.
"""
