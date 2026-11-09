"""Every text file the repo reads or writes names its encoding.

`Path.read_text()`, `Path.write_text(text)` and `open(path)` use the machine's locale encoding when none is given (cp1252 on Windows, and
whatever `LANG` says elsewhere), not UTF-8. The sources, the fixtures and this repo's own expected strings are full of `→`, `—` and `’`, so
code that reads them without an encoding passes on one machine and raises `UnicodeDecodeError` (or writes mojibake) on another. Ruff's
`PLW1514` only flags a receiver it can prove is a `Path`; an AST walk found 35 calls in shipped code it had missed (fixed in #251) and 158 in the
tests (fixed with this test). This reads every call, whatever the receiver.

Binary opens (`"rb"`, `"wb"`) have no encoding to name and are not checked.
"""
import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
ROOTS = ("app", "scripts", "tests", "packages", "services", "deploy", "garak", "loadtest")
SKIP_DIRS = {"node_modules", ".venv", "venv", "__pycache__", ".mypy_cache"}


def _python_files():
    for root in ROOTS:
        for path in (REPO / root).rglob("*.py"):
            if not SKIP_DIRS.intersection(path.parts):
                yield path


def _names_no_encoding(call: ast.Call) -> bool:
    """True for a `.read_text` / `.write_text` / builtin text-mode `open` call that leaves the encoding to the locale."""
    func = call.func
    if any(k.arg is None for k in call.keywords):  # `**options`: cannot tell, and then the caller is naming it somewhere
        return False
    if any(k.arg == "encoding" for k in call.keywords):
        return False
    if isinstance(func, ast.Attribute) and func.attr in {"read_text", "write_text"}:
        positional_encoding = 1 if func.attr == "read_text" else 2  # read_text(encoding), write_text(data, encoding)
        return len(call.args) < positional_encoding
    if isinstance(func, ast.Name) and func.id == "open":
        mode = next((k.value for k in call.keywords if k.arg == "mode"), call.args[1] if len(call.args) > 1 else None)
        binary = isinstance(mode, ast.Constant) and isinstance(mode.value, str) and "b" in mode.value
        return not binary and len(call.args) < 4  # open(file, mode, buffering, encoding)
    return False


def test_no_text_file_is_read_or_written_in_the_locale_encoding():
    offenders = []
    for path in _python_files():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and _names_no_encoding(node):
                offenders.append(f"{path.relative_to(REPO)}:{node.lineno}")
    assert not offenders, "text files opened without `encoding=\"utf-8\"`:\n" + "\n".join(sorted(offenders))
