"""A subagent's own `[n]` citation markers are removed from its answer; an index in its text is not.

The markers name the subagent's OWN retrieved sources (its `[1]` is a different document from the
parent's `[1]`), and the parent's citation checks would read them as theirs, so they come out
(GRAPH_PATTERNS.md pattern 46). The strip used to remove every `[digits]`, so a literal `[0]`, or the
`[1]` of `items[1]`, went with them.
"""
import pytest

from app.agent.tools.run_subagent import strip_citation_markers


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Support is open 9 to 5 [1].", "Support is open 9 to 5."),
        ("Support is open 9 to 5.[1]", "Support is open 9 to 5."),
        ("[1] Support is open 9 to 5.", " Support is open 9 to 5."),
        ("See the policy [1][3] for details.", "See the policy for details."),
        ("Both agree [2] and [10].", "Both agree and."),
        ("Line one [1]\nLine two [2]", "Line one\nLine two"),
    ],
)
def test_a_citation_marker_is_removed_with_the_space_before_it(text, expected):
    assert strip_citation_markers(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "Use items[0] as the default.",
        "The first element is [0].",
        "Read matrix[1][2] last.",
        "Call f(x)[1] after the lookup.",
        "The zero-padded [01] form is not a marker.",
    ],
)
def test_an_index_or_a_zero_is_left_alone(text):
    assert strip_citation_markers(text) == text
