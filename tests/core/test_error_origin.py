"""`error_origin` says WHERE an unexpected error was raised, and never what it said.

The logs carry an exception's class and nothing else, because its text can name an internal host, a SQL
fragment or a DSN (pattern 30). That left a bare `IndexError` from a CI run with no way to find its
source (#171). The origin is a file, a line and a function: enough to find it, nothing that can leak.
"""
from app.core.errors import error_origin


def _where_it_goes_wrong(items):
    return items[3]  # raises IndexError


def _wrapper(items):
    return _where_it_goes_wrong(items)


def test_the_origin_is_the_innermost_frame_not_the_caller():
    try:
        _wrapper([])
    except IndexError as exc:
        origin = error_origin(exc)

    assert origin.startswith("test_error_origin.py:")
    assert origin.endswith(" in _where_it_goes_wrong")


def test_the_origin_never_contains_the_message():
    try:
        raise RuntimeError("could not connect to internal-db-host.example:5432 as admin:hunter2")
    except RuntimeError as exc:
        origin = error_origin(exc)

    assert "internal-db-host" not in origin and "hunter2" not in origin


def test_an_exception_that_was_never_raised_has_an_unknown_origin():
    assert error_origin(ValueError("never raised")) == "unknown"
