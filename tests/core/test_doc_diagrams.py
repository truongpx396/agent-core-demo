"""A Mermaid block that does not parse is not drawn at all: GitHub shows "Unable to render rich display" for the whole README diagram.

`ExtMCP -. Streamable HTTP + OAuth 2.1 token .-> Caddy` did that (PR #224): a "." inside the text of a dotted edge breaks Mermaid's
lexer ("Lexical error ... Unrecognized text"). The repo has no Mermaid renderer, so this pins the one rule that bit rather than
parsing the grammar. The quoted form, `-.->|"text"|`, takes any text.
"""
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DOCS = sorted(REPO.glob("*.md"))
MERMAID_BLOCK = re.compile(r"```mermaid\n(.*?)\n```", re.S)
# `A -. text .-> B`: the unquoted label of a dotted edge. `A -.-> B` and `A -.->|"text"| B` have no match.
DOTTED_EDGE_TEXT = re.compile(r"-\.\s+(?P<text>[^\n]*?)\s+\.->")


def _blocks() -> list[tuple[str, str]]:
    return [(doc.name, block) for doc in DOCS for block in MERMAID_BLOCK.findall(doc.read_text())]


def test_there_are_diagrams_for_this_test_to_be_about():
    """A floor against a vacuous pass: a renamed README or a changed fence would make the check below true."""
    assert any(name == "README.md" for name, _ in _blocks())


def test_a_dotted_edge_label_has_no_dot_in_it():
    offending = [
        f"{name}: {match.group(0)}"
        for name, block in _blocks()
        for match in DOTTED_EDGE_TEXT.finditer(block)
        if "." in match.group("text")
    ]

    assert not offending, (
        "a '.' in the text of a dotted edge makes Mermaid fail to render the whole diagram; "
        'write the label as -.->|"text"| instead:\n' + "\n".join(offending)
    )
