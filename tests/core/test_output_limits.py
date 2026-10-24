"""app/core/output_limits.py: the cap on what a tool hands the model (specs/009 A3)."""
from app.core import metrics
from app.core.output_limits import bound_output
from tests.conftest import metric_value


def test_text_within_the_limit_is_returned_untouched_and_uncounted():
    before = metric_value(metrics.agent_tool_output_truncated_total, dependency="unit")

    assert bound_output("short", 100, dependency="unit") == "short"
    assert bound_output("x" * 100, 100, dependency="unit") == "x" * 100  # exactly the limit fits
    assert metric_value(metrics.agent_tool_output_truncated_total, dependency="unit") == before


def test_longer_text_keeps_its_head_and_its_tail_and_says_what_was_left_out():
    text = "BEGIN" + "." * 10_000 + "END"

    bounded = bound_output(text, 300, dependency="unit")

    assert bounded.startswith("BEGIN")
    assert bounded.endswith("END")  # the failure in a command's output is usually at the end
    assert f"{len(text) - 300} of {len(text)} characters omitted" in bounded
    assert len(bounded) < 300 + 80


def test_the_limit_counts_kept_characters_and_the_head_gets_two_thirds():
    bounded = bound_output("a" * 600 + "b" * 600, 300, dependency="unit")

    head = bounded.split("\n… [")[0]
    tail = bounded.rsplit("] …\n", 1)[1]
    assert head == "a" * 200
    assert tail == "b" * 100


def test_every_cut_is_counted_on_the_dependency_that_produced_it():
    before = metric_value(metrics.agent_tool_output_truncated_total, dependency="unit_counted")

    bound_output("z" * 1000, 100, dependency="unit_counted")
    bound_output("z" * 1000, 100, dependency="unit_counted")

    assert metric_value(metrics.agent_tool_output_truncated_total, dependency="unit_counted") == before + 2


def test_a_limit_of_zero_or_less_means_no_cap():
    assert bound_output("q" * 5000, 0, dependency="unit") == "q" * 5000
