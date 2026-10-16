"""The default suite never builds a real Langfuse client (tests/conftest.py's `no_real_tracing`).

A real client starts threads, tries the network, and registers an `atexit` flush that took about 90 seconds to give up
when Langfuse was not running (a one-file pytest run was slow to exit even when every test passed). This file resets
nothing, so it sees what every ordinary test sees. The tests that exercise the tracing code bring their own fake SDK
(test_tracing_client.py).
"""
from app.core import tracing


def test_an_ordinary_test_gets_no_tracing_client_and_so_builds_no_real_one():
    assert tracing.get_langfuse() is None
