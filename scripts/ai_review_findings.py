"""Turns the AI reviewer's free-text answer into findings that can be anchored on a PR's diff.

Pure functions: no network, no GitHub types, stdlib only. `scripts/ai_review.py` does the I/O.

Every rule here comes from probing GitHub's real review-comment API on a throwaway PR (README,
"AI review"), not from the docs, which are silent on most of it:

- A comment attaches only to a line that is IN the diff (an added or context line inside a hunk).
  Any other line, or a path the PR did not change, is rejected with a 422.
- A batch of comments is ATOMIC: one unplaceable comment fails the whole batch and none are
  created, and the error does not say which one. So placement is decided here, up front, from the
  diff itself, instead of by trial and error against the API.
- GitHub does NOT deduplicate. Posting the same comment twice creates two, so the caller needs a
  stable key per location (`Anchor.key`), carried in a hidden marker in the comment body.
- A multi-line comment is accepted if both of its ends are lines in the diff, even across hunks.
  We are stricter on purpose: see `MAX_RANGE` and `anchor_for`.
"""
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace

# A citation as the prompt asks the model to write it: `path:line`, or `path:start-end`, in code
# formatting. The path alphabet is deliberately narrow (no spaces, brackets or parentheses), so
# nothing the model writes can break out of the markdown link built around it. The lookarounds
# skip a citation the model already wrapped in a link, so we never nest one link in another. The
# digit count is capped because Python refuses `int()` of more than 4,300 digits: a degenerate
# model answer with a huge number must leave that citation as plain text, not raise and cost the
# whole review. (7 digits is far beyond any real file's line count.)
CITATION = re.compile(r"(?<!\[)`([A-Za-z0-9_./@+-]+):L?(\d{1,7})(?:-L?(\d{1,7}))?`(?!\]\()")

# A multi-line comment longer than this highlights more than it points at, so a longer range is
# attached to its first line instead. Not a GitHub limit.
MAX_RANGE = 15
# Bound on how far into a long range we look for its first line that is in the diff: a model can
# cite `file:1-9999999`, and scanning that is a pointless amount of work.
_SCAN = 200

# One line can be wrong in several ways at once (a query that is both injectable and unscoped), and
# a real model then cites the same line for each finding. They get separate threads, so each can be
# replied to and resolved on its own, up to this many; past it a line is a pile-up and the rest
# stay in the summary. Found by running the real model: the first version allowed one per line and
# mislabelled the second finding as "not on a changed line".
MAX_PER_LINE = 3

_SEVERITY = r"\*\*\[(BLOCKER|CONCERN|NIT)\]\*\*"
_MARKER = re.compile(r"<!-- ai-review:inline (\S+:\d+(?:~\d+)?) -->")


@dataclass(frozen=True)
class Finding:
    """One finding as the model wrote it, with the first location it cites (if any)."""

    severity: str  # BLOCKER | CONCERN | NIT
    text: str  # verbatim, including its leading severity tag and citation
    path: str | None = None
    start: int | None = None
    end: int | None = None

    @property
    def headline(self) -> str:
        """A one-line gist for the summary comment: the first sentence after the citation."""
        body = re.sub(rf"^{_SEVERITY}\s*", "", self.text)
        body = CITATION.sub("", body, count=1).lstrip()
        body = re.sub(r"^[-–—:]+\s*", "", body)
        plain = re.sub(r"[*_`]", "", body)  # before splitting: `**Bad thing.** More` has no space after its period
        gist = re.split(r"(?<=[.!?])\s|\n", plain, maxsplit=1)[0].strip()
        return gist if len(gist) <= 110 else gist[:109].rstrip() + "…"


@dataclass(frozen=True)
class Anchor:
    """Where a comment goes: `line` is the last line, `start_line` the first of a multi-line range."""

    path: str
    line: int
    start_line: int | None = None
    ordinal: int = 1  # which of the findings sharing this line it is

    @property
    def key(self) -> str:
        """Stable identity of a comment slot, for deduplicating across re-runs: the first finding on
        a line is `path:line`, the second `path:line~2`."""
        base = f"{self.path}:{self.line}"
        return base if self.ordinal == 1 else f"{base}~{self.ordinal}"


@dataclass(frozen=True)
class Placement:
    finding: Finding
    anchor: Anchor | None  # None: nothing on the diff to attach it to; it stays in the summary only


def parse_findings(answer: str) -> tuple[str, list[Finding]]:
    """Splits an answer into (preamble, findings). No findings means an unparseable answer (or the
    model's "No issues found"), and the caller should then post the answer as it is.

    A finding starts at a line beginning with its `**[SEVERITY]**` tag, outside a code fence (a
    suggested fix may contain anything). Its location is the first citation in its lead
    paragraph; one further down is a mention of something else, not where the finding is.
    """
    starts: list[tuple[int, str]] = []  # (offset, severity) of each finding's first line
    offset = 0
    in_fence = False
    for line in answer.splitlines(keepends=True):
        tag = re.match(_SEVERITY, line)
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        elif tag and not in_fence:
            starts.append((offset, tag.group(1)))
        offset += len(line)
    if not starts:
        return answer, []
    findings = []
    ends = [begin for begin, _ in starts[1:]] + [len(answer)]
    for (begin, severity), end in zip(starts, ends, strict=True):
        text = answer[begin:end].strip()
        lead = re.split(r"\n\s*\n|```", text, maxsplit=1)[0]
        cite = CITATION.search(lead)
        path = start = stop = None
        if cite:
            path, start = cite.group(1), int(cite.group(2))
            stop = int(cite.group(3) or start)
        findings.append(Finding(severity, text, path, start, stop))
    return answer[: starts[0][0]], findings


def addressable_lines(diff: str) -> dict[str, frozenset[int]]:
    """For each changed file, the new-side line numbers a review comment can attach to: every added
    or context line inside a hunk. A deleted line has no new-side number, and a file with no
    hunks (binary, pure rename) has no lines at all."""
    result: dict[str, set[int]] = {}
    path: str | None = None
    new = 0
    in_hunk = False
    for raw in diff.split("\n"):
        if raw.startswith("diff --git "):
            header = re.match(r"diff --git a/.+? b/(.+)$", raw)
            path = header.group(1) if header else None
            in_hunk = False
            if path:
                result.setdefault(path, set())
            continue
        hunk = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", raw)
        if hunk:
            new, in_hunk = int(hunk.group(1)), True
        elif in_hunk and path and raw[:1] in ("+", " "):
            result[path].add(new)
            new += 1
    return {name: frozenset(lines) for name, lines in result.items()}


def anchor_for(finding: Finding, addressable: Mapping[str, frozenset[int]]) -> Anchor | None:
    """The spot on the diff for a finding, or None.

    A cited range becomes a multi-line comment only if EVERY line of it is in the diff and it is at
    most `MAX_RANGE` long. Otherwise the comment goes on the first line of the range that is in the
    diff, so a range that spills past a hunk still lands, just narrower. Stricter than GitHub, which
    accepts any range whose two ends resolve, even across hunks, and would then highlight lines the
    PR never touched.
    """
    if finding.path is None or finding.start is None:
        return None
    lines = addressable.get(finding.path)
    if not lines:
        return None
    first, last = finding.start, finding.end if finding.end is not None else finding.start
    low, high = min(first, last), max(first, last)
    if high - low <= MAX_RANGE and all(n in lines for n in range(low, high + 1)):
        return Anchor(finding.path, high, low if high > low else None)
    spot = next((n for n in range(low, min(high, low + _SCAN) + 1) if n in lines), None)
    return None if spot is None else Anchor(finding.path, spot)


def place_findings(findings: list[Finding], addressable: Mapping[str, frozenset[int]]) -> list[Placement]:
    """Anchors each finding. Findings that land on the same line each get their own comment (the
    n-th gets ordinal n), up to `MAX_PER_LINE`; later ones stay summary-only."""
    on_line: dict[tuple[str, int], int] = {}
    placements = []
    for finding in findings:
        anchor = anchor_for(finding, addressable)
        if anchor is not None:
            spot = (anchor.path, anchor.line)
            count = on_line.get(spot, 0) + 1
            if count > MAX_PER_LINE:
                anchor = None
            else:
                on_line[spot] = count
                anchor = replace(anchor, ordinal=count)
        placements.append(Placement(finding, anchor))
    return placements


def marker(anchor: Anchor) -> str:
    """Hidden HTML comment that identifies a location in a comment body, for dedupe on re-runs."""
    return f"<!-- ai-review:inline {anchor.key} -->"


def marker_key(body: str) -> str | None:
    found = _MARKER.search(body)
    return found.group(1) if found else None
