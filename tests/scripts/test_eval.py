"""Tests for scripts/eval.py's aggregation logic — run_case's repetition
averaging/pass-rate and grounded_claims_ratio's answer-quality gate
(GRAPH_PATTERNS.md pattern 40). Uses a fake graph (no real LLM/Qdrant),
matching the rest of this suite's hermetic discipline — scripts/eval.py
itself is meant to run against the real stack (see its own module
docstring), but the AGGREGATION math is pure and worth testing in
isolation.
"""
import pytest
from langchain_core.messages import AIMessage

from scripts.eval import CaseResult, GoldenCase, grounded_claims_ratio, run_case


class _FakeState:
    def __init__(self):
        self.next = ()  # never paused


class _FakeGraph:
    """A graph whose .invoke() returns a pre-scripted sequence of result
    dicts, one per call — enough to drive run_case's repetition loop
    without a real LLM."""

    def __init__(self, results):
        self._results = iter(results)

    def invoke(self, input_, config):
        return next(self._results)

    def get_state(self, config):
        return _FakeState()


def _result(answer, total_tokens=10, total_cost_usd=0.0, used=0, ungrounded=0):
    return {
        "messages": [AIMessage(content=answer)],
        "iterations": 1,
        "total_tokens": total_tokens,
        "total_cost_usd": total_cost_usd,
        "used_citations": [{"marker": f"[{i + 1}]"} for i in range(used)],
        "ungrounded_claims_count": ungrounded,
    }


def _case_result(**overrides):
    defaults = dict(
        id="c1", input="x", pass_rate=1.0, repetitions=1, passed=True,
        checks=[], latency_s=0.1, iterations=1, tool_calls=[], answer="x",
        total_tokens=0, total_cost_usd=0.0, used_citations_count=0, ungrounded_claims_count=0,
    )
    defaults.update(overrides)
    return CaseResult(**defaults)


class TestRunCaseAggregation:
    def test_all_repetitions_passing_gives_100_percent_pass_rate(self):
        case = GoldenCase(id="c1", input="hi", expect_keywords=["hello"])
        graph = _FakeGraph([_result("hello there") for _ in range(5)])

        result = run_case(graph, case, repetitions=5)

        assert result.pass_rate == 1.0
        assert result.passed is True

    def test_below_threshold_pass_rate_fails_the_case(self):
        case = GoldenCase(id="c1", input="hi", expect_keywords=["hello"])
        # 2 of 5 pass -> 40%, below REPETITION_PASS_THRESHOLD (80%)
        graph = _FakeGraph(
            [
                _result("hello there"),
                _result("hello there"),
                _result("nope"),
                _result("nope"),
                _result("nope"),
            ]
        )

        result = run_case(graph, case, repetitions=5)

        assert result.pass_rate == 0.4
        assert result.passed is False

    def test_at_threshold_pass_rate_passes_the_case(self):
        case = GoldenCase(id="c1", input="hi", expect_keywords=["hello"])
        # 4 of 5 -> exactly 80%, the threshold itself.
        graph = _FakeGraph(
            [
                _result("hello there"),
                _result("hello there"),
                _result("hello there"),
                _result("hello there"),
                _result("nope"),
            ]
        )

        result = run_case(graph, case, repetitions=5)

        assert result.pass_rate == 0.8
        assert result.passed is True

    def test_tokens_and_cost_are_summed_across_repetitions_not_averaged(self):
        case = GoldenCase(id="c1", input="hi")
        graph = _FakeGraph(
            [
                _result("ok", total_tokens=100, total_cost_usd=0.01),
                _result("ok", total_tokens=200, total_cost_usd=0.02),
            ]
        )

        result = run_case(graph, case, repetitions=2)

        assert result.total_tokens == 300
        assert result.total_cost_usd == pytest.approx(0.03)

    def test_grounding_counts_are_summed_across_repetitions(self):
        case = GoldenCase(id="c1", input="hi")
        graph = _FakeGraph(
            [
                _result("ok", used=2, ungrounded=1),
                _result("ok", used=1, ungrounded=0),
            ]
        )

        result = run_case(graph, case, repetitions=2)

        assert result.used_citations_count == 3
        assert result.ungrounded_claims_count == 1

    def test_checks_and_answer_come_from_the_last_repetition(self):
        case = GoldenCase(id="c1", input="hi", expect_keywords=["hello"])
        graph = _FakeGraph([_result("hello there"), _result("the final one")])

        result = run_case(graph, case, repetitions=2)

        assert result.answer == "the final one"


class TestGroundedClaimsRatio:
    def test_no_citations_anywhere_is_vacuously_1(self):
        results = [_case_result(used_citations_count=0, ungrounded_claims_count=0)]
        assert grounded_claims_ratio(results) == 1.0

    def test_all_grounded_is_1(self):
        results = [_case_result(used_citations_count=5, ungrounded_claims_count=0)]
        assert grounded_claims_ratio(results) == 1.0

    def test_mixed_grounding_computes_the_right_ratio(self):
        results = [_case_result(used_citations_count=9, ungrounded_claims_count=1)]
        assert grounded_claims_ratio(results) == 0.9

    def test_aggregates_across_multiple_cases(self):
        results = [
            _case_result(id="c1", used_citations_count=8, ungrounded_claims_count=0),
            _case_result(id="c2", used_citations_count=0, ungrounded_claims_count=2),
        ]
        assert grounded_claims_ratio(results) == 0.8
