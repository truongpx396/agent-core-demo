"""scripts/model_eval/runners/classify.py::TieredRouter: the app's own router, scored like any other (specs/015 FR-029).

The label-set matcher is a fake that returns chosen nearest labels, so what is under test is the backend: that it runs `routing_policy.decide` (the
function `route_model` runs) and turns its tiers into scores a cost-quality curve can rank without depending on the order of the dataset.
"""
import numpy as np
import pytest

from app.core import routing_policy as rp
from scripts.model_eval import __main__ as cli
from scripts.model_eval import metrics
from scripts.model_eval.runners import classify
from scripts.model_eval.runners.base import PrerequisiteMissing, RunSettings

LABELS = ("smalltalk", "lookup", "action", "drafting", "analysis", "code", "reasoning", "high_stakes")
CUTOFF = 0.7
LONG = "x" * rp.DEFAULT_LONG_PROMPT_CHARS


class FakeTopics:
    """Nearest label and similarity chosen per text; anything not listed is nearest to `smalltalk` at 0.9."""

    identity, revision, size_bytes, rss_mb = "fake:model", "r" * 40, 7, 1.0
    labels = LABELS

    def __init__(self, answers=None, min_similarity=CUTOFF):
        self.answers, self.min_similarity = answers or {}, min_similarity

    def nearest(self, texts):
        return [(LABELS.index(self.answers.get(t, ("smalltalk", 0.9))[0]), self.answers.get(t, ("smalltalk", 0.9))[1]) for t in texts]


def router(answers=None, **kw):
    return classify.TieredRouter(FakeTopics(answers), **kw)


def test_it_scores_the_tier_the_apps_own_decide_gives_with_the_label_cutoff_applied():
    answers = {"a": ("reasoning", 0.9), "b": ("analysis", 0.9), "c": ("reasoning", 0.69), "d": ("analysis", CUTOFF)}
    decided = router(answers).decisions(["a", "b", "c", "d", "e"])

    assert [d.tier for d in decided] == ["strong", "medium", "local", "medium", "local"]  # c is below the cutoff (no label); d is exactly at it (kept)
    for text, d in zip("abcde", decided, strict=True):  # and each is exactly what the app computes from the same inputs
        label = answers.get(text, ("smalltalk", 0.9))
        expected = rp.decide(text, label[0] if label[1] >= CUTOFF else None, ceiling="strong", configured=rp.TIERS, long_prompt_chars=rp.DEFAULT_LONG_PROMPT_CHARS)
        assert d == expected


def test_the_rules_reach_the_score_when_the_label_does_not():
    scores = router({LONG: ("reasoning", 0.1)}).scores([LONG])  # the label is below the cutoff; the long-prompt rule asks for the medium tier

    assert 0.5 <= scores[0] < 0.501


def test_scores_keep_the_tiers_in_order_and_medium_counts_as_needing_a_stronger_model():
    answers = {f"{label}-{n}": (label, 0.9) for label in LABELS for n in range(30)}
    texts = list(answers)
    by_tier: dict[str, list[float]] = {"local": [], "medium": [], "strong": []}
    for d, s in zip(router(answers).decisions(texts), router(answers).scores(texts), strict=True):
        by_tier[d.tier].append(s)

    assert all(by_tier[t] for t in by_tier)
    assert max(by_tier["local"]) < 0.001 <= 0.5 <= min(by_tier["medium"]) and max(by_tier["medium"]) < 0.501 < 0.999 <= min(by_tier["strong"]) and max(by_tier["strong"]) < 1.0
    assert min(by_tier["medium"]) >= 0.5, "the suite decides at 0.5: any routed tier is 'needs a stronger model'"


def test_a_score_depends_only_on_the_text_and_the_seed_never_on_the_order_of_the_dataset():
    texts = [f"prompt {n}" for n in range(40)]
    forward = dict(zip(texts, router().scores(texts), strict=True))
    backward = dict(zip(texts[::-1], router().scores(texts[::-1]), strict=True))

    assert forward == backward
    assert len(set(forward.values())) == 40, "prompts in the same tier must not tie, or the curve would rank them by input order"
    other_seed = dict(zip(texts, router(seed=1).scores(texts), strict=True))
    assert other_seed != forward and all(abs(other_seed[t] - forward[t]) < 0.001 for t in texts)


def test_the_curve_of_a_router_that_puts_everything_in_one_tier_is_a_random_tie_break_not_the_datasets_order():
    n = 400
    texts = [f"prompt {i}" for i in range(n)]
    q_local = np.where(np.arange(n) < n // 2, 0.0, 1.0)  # the first half is where the strong model helps ...
    q_strong = np.ones(n)
    scores = router().scores(texts)
    curve = metrics.pgr_curve(scores, q_local, q_strong)

    # ... so a router that breaks its tie by input order would recover the whole gap by escalating half the prompts; a random tie-break recovers half of it.
    assert curve[10] == pytest.approx(0.5, abs=0.1)


def test_the_ceiling_and_the_configured_tiers_are_the_apps():
    answers = {"a": ("reasoning", 0.9)}

    assert router(answers, ceiling="medium").decisions(["a"])[0].tier == "medium"
    assert router(answers, configured=("local", "medium")).decisions(["a"])[0].tier == "medium"
    assert router(answers, configured=("local",)).decisions(["a"])[0].tier == "local"


def test_the_backend_carries_the_identity_of_the_matcher_it_uses():
    backend = router()

    assert backend.identity == "rules-and-labels:fake:model" and backend.revision == "r" * 40 and (backend.size_bytes, backend.rss_mb) == (7, 1.0)


def test_it_is_a_declared_candidate_and_build_backend_builds_it(monkeypatch):
    candidate = cli.resolve_candidate("classifier", "rules-and-labels-bge-small-en-v1.5")
    monkeypatch.setattr(classify.topics, "load_labelset", lambda name: object())
    monkeypatch.setattr(classify.topics, "InProcessTopics", lambda cand, labelset, threads=None: FakeTopics())

    built = classify.build_backend(candidate, RunSettings(runs=1))

    assert isinstance(built, classify.TieredRouter) and candidate.name in classify.TIERED_ROUTERS


def test_a_label_set_with_no_cutoff_for_its_model_cannot_back_a_router(monkeypatch):
    candidate = cli.resolve_candidate("classifier", "rules-and-labels-bge-small-en-v1.5")
    monkeypatch.setattr(classify.topics, "load_labelset", lambda name: object())
    monkeypatch.setattr(classify.topics, "InProcessTopics", lambda cand, labelset, threads=None: FakeTopics(min_similarity=None))

    with pytest.raises(PrerequisiteMissing, match="no calibration.*labelset-calibrate"):
        classify.build_backend(candidate, RunSettings(runs=1))
