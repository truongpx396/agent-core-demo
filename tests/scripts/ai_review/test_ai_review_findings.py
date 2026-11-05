"""Tests for scripts/ai_review_findings.py: parsing the model's answer into findings and deciding
where each can be attached on a PR's diff. Pure functions, so no fakes are needed.

The diff fixture mirrors what was probed against GitHub's real API on a throwaway PR: two hunks in
one file, a new file, a deleted file, a pure rename, a binary file, and a "no newline" marker. The
anchoring rules asserted here are the ones that probing found GitHub enforcing (a line outside any
hunk is a 422, and a batch is atomic), so a regression here is a 422 that costs a whole review.
"""
import pytest

from scripts.ai_review import findings as f

DIFF = "\n".join(
    [
        "diff --git a/README.md b/README.md",
        "index 111..222 100644",
        "--- a/README.md",
        "+++ b/README.md",
        "@@ -1,7 +1,9 @@",
        " # title",
        " ",  # an empty context line is a single space
        " intro",
        "-old line",
        "+new line",
        "+PROBE ONE",
        "+PROBE TWO",
        " ",
        " | Tool |",
        " |------|",
        "@@ -896,6 +898,8 @@ way):",
        " a",
        " b",
        " c",
        "+PROBE THREE",
        "+PROBE FOUR",
        " d",
        " e",
        " f",
        "diff --git a/probe_new.py b/probe_new.py",
        "new file mode 100644",
        "--- /dev/null",
        "+++ b/probe_new.py",
        "@@ -0,0 +1,3 @@",
        "+a = 1",
        "+b = 2",
        "+c = 3",
        "diff --git a/gone.py b/gone.py",
        "deleted file mode 100644",
        "--- a/gone.py",
        "+++ /dev/null",
        "@@ -1,2 +0,0 @@",
        "-x = 1",
        "-y = 2",
        "diff --git a/old.py b/new.py",
        "similarity index 100%",
        "rename from old.py",
        "rename to new.py",
        "diff --git a/img.png b/img.png",
        "Binary files a/img.png and b/img.png differ",
        "diff --git a/tail.py b/tail.py",
        "--- a/tail.py",
        "+++ b/tail.py",
        "@@ -1,2 +1,2 @@",
        " keep",
        "-old",
        "\\ No newline at end of file",
        "+new",
        "\\ No newline at end of file",
        "",
    ]
)
ADDR = f.addressable_lines(DIFF)


def _finding(path, start, end=None, severity="CONCERN"):
    return f.Finding(severity, f"**[{severity}]** `{path}:{start}` - text", path, start, start if end is None else end)


# --- parse_findings ---------------------------------------------------------------------------


def test_parse_findings_splits_on_severity_tags_and_keeps_the_preamble():
    answer = (
        "Here is the review.\n\n"
        "**[BLOCKER]** `app/a.py:12` - **Bad thing.** More words.\n\n"
        "**[CONCERN]** `app/b.py:3-5` - second. Another sentence.\n\n"
        "**[NIT]** `app/c.py:1` - third."
    )
    preamble, findings = f.parse_findings(answer)
    assert preamble == "Here is the review.\n\n"
    assert [(x.severity, x.path, x.start, x.end) for x in findings] == [
        ("BLOCKER", "app/a.py", 12, 12),
        ("CONCERN", "app/b.py", 3, 5),
        ("NIT", "app/c.py", 1, 1),
    ]
    assert findings[1].text.startswith("**[CONCERN]**") and "Another sentence." in findings[1].text


def test_parse_findings_does_not_start_a_finding_inside_a_code_fence():
    answer = "**[BLOCKER]** `a.py:1` - first\n```python\n**[NIT]** `b.py:2` - inside a suggested fix\n```\n\n**[CONCERN]** `c.py:3` - second"
    _, findings = f.parse_findings(answer)
    assert [x.path for x in findings] == ["a.py", "c.py"]
    assert "inside a suggested fix" in findings[0].text  # the fence stays part of the finding it belongs to


def test_parse_findings_takes_the_location_from_the_lead_paragraph_only():
    answer = "**[CONCERN]** this one cites nothing up front\n\nLater it mentions `other.py:9` in passing."
    _, findings = f.parse_findings(answer)
    assert findings[0].path is None and findings[0].start is None  # a later citation is a mention, not the location
    _, findings = f.parse_findings("**[CONCERN]** The bug is here: `a.py:L7-L9` - see it.")
    assert (findings[0].path, findings[0].start, findings[0].end) == ("a.py", 7, 9)  # anywhere in the lead, L prefix ok


@pytest.mark.parametrize("answer", ["No issues found in the diff.", "", "Some prose with no tags at all."])
def test_parse_findings_returns_the_answer_untouched_when_there_are_no_findings(answer):
    assert f.parse_findings(answer) == (answer, [])


def test_parse_findings_survives_a_degenerate_line_number():
    _, findings = f.parse_findings("**[NIT]** `a.py:" + "9" * 5000 + "` - absurd")
    assert findings[0].path is None  # not a citation (digit cap), and above all no exception


@pytest.mark.parametrize(
    "text, gist",
    [
        ("**[BLOCKER]** `a.py:1` - **Bad thing.** More words here.", "Bad thing."),
        ("**[NIT]** `a.py:1` — no bold, em dash. Second sentence.", "no bold, em dash."),
        ("**[CONCERN]** `a.py:1` - first line without a period\nsecond line", "first line without a period"),
        ("**[NIT]** `a.py:1` - " + "word " * 60, ("word " * 60)[:109].rstrip() + "…"),
        ("**[NIT]** `a.py:1`", ""),
    ],
)
def test_headline_is_the_first_sentence_after_the_citation_without_markdown(text, gist):
    assert f.Finding("NIT", text).headline == gist


# --- addressable_lines ------------------------------------------------------------------------


def test_addressable_lines_are_the_added_and_context_lines_of_each_hunk():
    assert ADDR["README.md"] == frozenset(range(1, 10)) | frozenset(range(898, 906))  # two hunks, nothing between
    assert ADDR["probe_new.py"] == frozenset({1, 2, 3})
    assert ADDR["tail.py"] == frozenset({1, 2})  # the "no newline" markers are not lines


def test_addressable_lines_has_nothing_for_files_without_new_side_lines():
    assert ADDR["gone.py"] == frozenset()  # a deleted line has no new-side number
    assert ADDR["new.py"] == frozenset()  # a pure rename has no hunk; the key is the NEW name
    assert ADDR["img.png"] == frozenset()
    assert "old.py" not in ADDR


def test_addressable_lines_counts_an_added_line_that_looks_like_a_header():
    diff = "diff --git a/x.md b/x.md\n--- a/x.md\n+++ b/x.md\n@@ -1 +1,2 @@\n keep\n+++ b/not-a-header\n"
    assert f.addressable_lines(diff)["x.md"] == frozenset({1, 2})  # inside a hunk it is content, not a file header


# --- anchor_for -------------------------------------------------------------------------------


def test_anchor_for_places_a_line_that_is_in_the_diff_including_a_context_line():
    assert f.anchor_for(_finding("README.md", 5), ADDR) == f.Anchor("README.md", 5)
    assert f.anchor_for(_finding("README.md", 2), ADDR) == f.Anchor("README.md", 2)  # unchanged, but inside a hunk


@pytest.mark.parametrize("path, line", [("README.md", 500), ("README.md", 10), ("README.md", 0), ("Makefile", 1), ("gone.py", 1), ("new.py", 1)])
def test_anchor_for_refuses_anything_github_would_answer_with_a_422(path, line):
    assert f.anchor_for(_finding(path, line), ADDR) is None  # a line outside every hunk, or a path not in the diff


def test_anchor_for_makes_a_multi_line_comment_only_when_every_line_is_in_the_diff():
    assert f.anchor_for(_finding("probe_new.py", 1, 3), ADDR) == f.Anchor("probe_new.py", 3, 1)
    assert f.anchor_for(_finding("probe_new.py", 3, 1), ADDR) == f.Anchor("probe_new.py", 3, 1)  # reversed
    assert f.anchor_for(_finding("probe_new.py", 2, 2), ADDR) == f.Anchor("probe_new.py", 2)  # a one-line range is one line


def test_anchor_for_falls_back_to_the_first_line_in_the_diff_when_a_range_spills_out():
    assert f.anchor_for(_finding("README.md", 7, 20), ADDR) == f.Anchor("README.md", 7)  # starts in, ends outside
    assert f.anchor_for(_finding("README.md", 5, 901), ADDR) == f.Anchor("README.md", 5)  # spans two hunks: GitHub would accept it
    assert f.anchor_for(_finding("README.md", 12, 20), ADDR) is None  # none of it is in the diff


def test_anchor_for_caps_a_long_range_at_its_first_line():
    long_diff = "diff --git a/big.py b/big.py\n@@ -0,0 +1,40 @@\n" + "\n".join(f"+l{i}" for i in range(40)) + "\n"
    big = f.addressable_lines(long_diff)
    assert f.anchor_for(_finding("big.py", 1, f.MAX_RANGE + 1), big) == f.Anchor("big.py", f.MAX_RANGE + 1, 1)  # at the cap
    assert f.anchor_for(_finding("big.py", 1, f.MAX_RANGE + 2), big) == f.Anchor("big.py", 1)  # over it: first line only


class _CountingLines(frozenset):
    """A set of lines that counts how many membership checks anchor_for makes against it."""

    checks = 0

    def __contains__(self, item):
        _CountingLines.checks += 1
        return super().__contains__(item)


def test_anchor_for_inspects_a_bounded_number_of_lines_for_an_absurd_range():
    # A model can cite `file:50-9999999`. Counting checks (not timing) keeps this deterministic.
    lines = {"probe_new.py": _CountingLines({1, 2, 3})}
    _CountingLines.checks = 0
    assert f.anchor_for(_finding("probe_new.py", 50, 9_999_999), lines) is None
    assert _CountingLines.checks <= 250  # a bounded scan, not ten million checks
    _CountingLines.checks = 0
    assert f.anchor_for(_finding("probe_new.py", 1, 9_999_999), lines) == f.Anchor("probe_new.py", 1)
    assert _CountingLines.checks <= 250


def test_anchor_for_ignores_a_finding_with_no_location():
    assert f.anchor_for(f.Finding("NIT", "**[NIT]** nothing cited"), ADDR) is None


# --- place_findings / marker -------------------------------------------------------------------


def test_place_findings_keeps_order_and_gives_each_finding_on_one_line_its_own_comment_slot():
    placed = f.place_findings([_finding("probe_new.py", 2), _finding("README.md", 500), _finding("probe_new.py", 2)], ADDR)
    assert [p.anchor for p in placed] == [f.Anchor("probe_new.py", 2), None, f.Anchor("probe_new.py", 2, ordinal=2)]
    assert [p.anchor.key for p in placed if p.anchor] == ["probe_new.py:2", "probe_new.py:2~2"]  # distinct, stable keys for re-runs


def test_place_findings_stops_at_the_cap_and_a_range_ending_on_the_same_line_shares_its_slots():
    findings = [_finding("probe_new.py", 2)] * (f.MAX_PER_LINE + 2)
    anchors = [p.anchor for p in f.place_findings(findings, ADDR)]
    assert [a.ordinal for a in anchors[: f.MAX_PER_LINE] if a] == list(range(1, f.MAX_PER_LINE + 1))
    assert anchors[f.MAX_PER_LINE:] == [None, None]  # past the cap a line is a pile-up: the rest stay in the summary
    mixed = f.place_findings([_finding("probe_new.py", 3), _finding("probe_new.py", 1, 3)], ADDR)
    assert [p.anchor.key for p in mixed if p.anchor] == ["probe_new.py:3", "probe_new.py:3~2"]  # same END line, so same line


def test_marker_round_trips_and_ignores_text_that_is_not_ours():
    anchor = f.Anchor("app/a.py", 12, 10)
    assert f.marker_key(f"body\n\n{f.marker(anchor)}") == "app/a.py:12"
    assert f.marker_key(f.marker(f.Anchor("app/a.py", 12, ordinal=2))) == "app/a.py:12~2"
    assert f.marker_key("no marker here") is None
    assert f.marker_key("<!-- ai-review:inline not-a-key -->") is None
